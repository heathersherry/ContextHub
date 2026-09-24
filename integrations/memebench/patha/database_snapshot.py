"""Explicit offline database copies, preserving IDs and the entire SQL state.

No defaults, .env, deletion, connection termination, or model construction.
Only newly provisioned, dedicated localhost databases are accepted. This is
snapshot plumbing, not graph reconstruction or a formal experiment manifest.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    Hash,
    Name,
    SealRef,
    canonical_hash,
)
from .run_isolation import require_closed_history


class DatabaseIdentity(Contract):
    system_identifier: Name
    data_directory: Name
    port: int = Field(ge=1024, le=65535)
    database: Name
    ordinary_role: Name
    schema_revision: Name


class DatabaseSnapshot(Contract):
    identity: DatabaseIdentity
    run_id: Name
    account_id: Name
    agent_id: Name
    replicate_id: int = Field(strict=True, ge=0, le=0)
    strategy: str = Field(pattern="^selective$")
    handoff: SealRef
    graph: SealRef
    configuration_sha256: Hash
    dump_sha256: Hash
    table_hashes: dict[str, Hash]
    tool_version: Name


def local_dsn(dsn, identity, *, database=None):
    url = urlsplit(dsn)
    name = database or identity.database
    if (
        url.scheme not in ("postgres", "postgresql")
        or url.hostname != "127.0.0.1"
        or url.port != identity.port
        or url.path != "/" + name
        or url.password
        or url.query
        or url.fragment
        or not url.username
        or not re.fullmatch(r"contexthub_s8_[a-z0-9_]+", name)
    ):
        raise ContractError("dedicated_local_database_required")
    return url.username


async def check_instance(connection, identity, *, database=None, ordinary=False):
    if await connection.fetchval("SHOW port") != str(identity.port):
        raise ContractError("snapshot_port_mismatch")
    if await connection.fetchval("SELECT current_database()") != (
        database or identity.database
    ):
        raise ContractError("snapshot_database_mismatch")
    if await connection.fetchval("SHOW server_encoding") != "UTF8":
        raise ContractError("snapshot_encoding_mismatch")
    if ordinary:
        role = await connection.fetchrow(
            "SELECT rolname,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )
        if (
            role["rolname"] != identity.ordinary_role
            or role["rolsuper"]
            or role["rolbypassrls"]
        ):
            raise ContractError("snapshot_ordinary_role_required")
    else:
        system = await connection.fetchval(
            "SELECT system_identifier::text FROM pg_control_system()"
        )
        if (
            system != identity.system_identifier
            or await connection.fetchval("SHOW data_directory")
            != identity.data_directory
        ):
            raise ContractError("snapshot_instance_mismatch")


async def table_hashes(connection):
    # Run in the exported transaction. JSONB text preserves database values;
    # row order is canonicalized, with no clock/UUID/content fields discarded.
    names = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    )
    result = {}
    for row in names:
        name = row["tablename"]
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise ContractError("snapshot_table_identifier_invalid")
        values = await connection.fetch(
            f'SELECT to_jsonb(t)::text value FROM public."{name}" t'
        )
        result[name] = canonical_hash(sorted(v["value"] for v in values))
    return result


async def process(binary, args, *, log):
    command = [str(binary), *args]
    child = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await child.communicate()
    Path(log).write_bytes(stdout + b"\n" + stderr)
    if child.returncode:
        raise ContractError("snapshot_tool_failed")
    return stdout.decode().strip()


async def capture(
    *, admin_dsn, identity, directory, pg_bin, run_id, handoff, configuration_sha256
):
    """Capture a consistent full database. Admin is used for snapshot export only."""
    import asyncpg

    local_dsn(admin_dsn, identity)
    if handoff.strategy != "selective" or handoff.status != "complete":
        raise ContractError("selective_main_handoff_required")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    connection = await asyncpg.connect(admin_dsn)
    try:
        await check_instance(connection, identity)
        if (
            await connection.fetchval("SELECT version_num FROM alembic_version")
            != identity.schema_revision
        ):
            raise ContractError("snapshot_schema_mismatch")
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            # All checks and hashes use the same snapshot as pg_dump.
            pending = await connection.fetchval(
                """SELECT count(*) FROM knowledge_artifacts o
                WHERE o.account_id=$1 AND o.kind='public_operation' AND NOT EXISTS
                (SELECT 1 FROM knowledge_artifacts c WHERE c.kind='public_completion'
                 AND c.account_id=o.account_id AND c.payload->>'operation_id'=o.id::text)""",
                handoff.account_id,
            )
            if pending:
                raise ContractError("snapshot_unfinished_operations")
            for ref in (handoff.reference(), handoff.graph):
                row = await connection.fetchrow(
                    "SELECT sha256,payload::text body FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
                    ref.account_id,
                    ref.id,
                )
                if (
                    row is None
                    or row["sha256"] != ref.sha256
                    or canonical_hash(json.loads(row["body"])) != ref.sha256
                ):
                    raise ContractError("snapshot_handoff_hash_mismatch")
                if ref == handoff.graph:
                    from contexthub.models.knowledge import GraphSnapshot

                    graph = GraphSnapshot.model_validate_json(row["body"])
            owner = await connection.fetchval(
                "SELECT payload->>'configuration_sha256' FROM knowledge_artifacts WHERE account_id=$1 AND id=$2 AND kind='public_operation'",
                handoff.account_id,
                handoff.execution_id,
            )
            if owner != configuration_sha256:
                raise ContractError("snapshot_configuration_mismatch")
            await require_closed_history(connection, handoff, graph)
            for cv in (*handoff.sources, *graph.nodes):
                current = await connection.fetchrow(
                    """SELECT version,status,validity_status,
                    EXISTS(SELECT 1 FROM context_invalidations i WHERE i.context_id=c.id AND resolved_at IS NULL) causes
                    FROM contexts c WHERE account_id=$1 AND id=$2""",
                    handoff.account_id,
                    cv.context_id,
                )
                if (
                    current is None
                    or current["version"] != cv.version
                    or current["status"] != "active"
                    or current["validity_status"] != "fresh"
                    or current["causes"]
                ):
                    raise ContractError("snapshot_not_before_state")
            exported = await connection.fetchval("SELECT pg_export_snapshot()")
            hashes = await table_hashes(connection)
            version = await process(
                Path(pg_bin) / "pg_dump",
                ["--version"],
                log=directory / "pg-version.log",
            )
            await process(
                Path(pg_bin) / "pg_dump",
                [
                    "--format=custom",
                    "--snapshot=" + exported,
                    "--file=" + str(directory / "database.dump"),
                    "--dbname=" + admin_dsn,
                ],
                log=directory / "pg-dump.log",
            )
        snapshot = DatabaseSnapshot(
            identity=identity,
            run_id=run_id,
            account_id=handoff.account_id,
            agent_id=handoff.agent_id,
            replicate_id=0,
            strategy="selective",
            handoff=handoff.reference(),
            graph=handoff.graph,
            configuration_sha256=configuration_sha256,
            dump_sha256=hashlib.sha256(
                (directory / "database.dump").read_bytes()
            ).hexdigest(),
            table_hashes=hashes,
            tool_version=version,
        )
        (directory / "snapshot.json").write_text(snapshot.model_dump_json(indent=2))
        return snapshot
    finally:
        await connection.close()


async def restore(
    *, admin_dsn, identity, snapshot, directory, target_database, pg_bin, output
):
    """Create a NEW database and restore intact owners/ACLs; never drop or clean.

    Any failed restore is retained for inspection. No retry into a used database.
    """
    import asyncpg

    admin = local_dsn(admin_dsn, identity)
    target_dsn = f"postgresql://{admin}@127.0.0.1:{identity.port}/{target_database}"
    local_dsn(target_dsn, identity, database=target_database)
    if target_database == identity.database or snapshot.identity != identity:
        raise ContractError("snapshot_target_identity_mismatch")
    dump = Path(directory) / "database.dump"
    if hashlib.sha256(dump.read_bytes()).hexdigest() != snapshot.dump_sha256:
        raise ContractError("snapshot_dump_hash_mismatch")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    connection = await asyncpg.connect(admin_dsn)
    try:
        await check_instance(connection, identity)
        if await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname=$1", target_database
        ):
            raise ContractError("snapshot_target_exists")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", identity.ordinary_role):
            raise ContractError("snapshot_role_identifier_invalid")
        await connection.execute(
            f'CREATE DATABASE "{target_database}" OWNER "{identity.ordinary_role}" ENCODING \'UTF8\' TEMPLATE template0'
        )
    finally:
        await connection.close()
    await process(
        Path(pg_bin) / "pg_restore",
        ["--exit-on-error", "--dbname=" + target_dsn, str(dump)],
        log=output / "pg-restore.log",
    )
    connection = await asyncpg.connect(target_dsn)
    try:
        await check_instance(connection, identity, database=target_database)
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            actual = await table_hashes(connection)
        if actual != snapshot.table_hashes:
            raise ContractError("snapshot_restored_state_mismatch")
    finally:
        await connection.close()
    ordinary = f"postgresql://{identity.ordinary_role}@127.0.0.1:{identity.port}/{target_database}"
    connection = await asyncpg.connect(ordinary)
    try:
        await check_instance(
            connection, identity, database=target_database, ordinary=True
        )
        async with connection.transaction(readonly=True):
            await connection.execute(
                "SELECT set_config('app.account_id',$1,true)", snapshot.account_id
            )
            # Verify actual ordinary-role artifact access, not just admin row equality.
            for ref in (snapshot.handoff, snapshot.graph):
                row = await connection.fetchrow(
                    "SELECT sha256 FROM knowledge_artifacts WHERE account_id=$1 AND id=$2",
                    snapshot.account_id,
                    ref.id,
                )
                if row is None or row["sha256"] != ref.sha256:
                    raise ContractError("snapshot_ordinary_artifact_access_failed")
    finally:
        await connection.close()
    (output / "restored.json").write_text(
        json.dumps(
            dict(
                database=target_database,
                snapshot_sha256=canonical_hash(snapshot),
                table_hashes=actual,
            ),
            indent=2,
        )
    )
    return ordinary
