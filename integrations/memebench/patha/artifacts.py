"""Append-only experiment exports. Core artifacts remain the source of truth."""

import hashlib
import json
from pathlib import Path

from contexthub.models.knowledge import ContractError
from .schemas import canonical_json_bytes


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ArtifactStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)

    def put(self, name, value):
        if Path(name).name != name or not name.endswith(".json"):
            raise ContractError("artifact_name_invalid")
        payload = (
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        )
        data = canonical_json_bytes(payload)
        with (self.directory / name).open("xb") as handle:
            handle.write(data)
        return hashlib.sha256(data).hexdigest()


async def export_account_state(repository, account_id, *, agent_id):
    """Export tenant business state in one read-only repeatable-read transaction.

    Answer attempts live in a separate account/ledger and cannot modify this
    state. No clocks, IDs, stale flags, causes or proofs are normalized away.
    """
    async with repository._pool.acquire() as connection:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            await connection.execute(
                "SELECT set_config('app.account_id',$1,true)", account_id
            )
            statements = {
                "contexts": "SELECT to_jsonb(c)::text AS value FROM contexts c WHERE account_id=$1",
                "context_versions": "SELECT to_jsonb(v)::text AS value FROM context_versions v JOIN contexts c ON c.id=v.context_id WHERE c.account_id=$1",
                "invalidations": "SELECT to_jsonb(i)::text AS value FROM context_invalidations i JOIN contexts c ON c.id=i.context_id WHERE c.account_id=$1",
                "artifacts": "SELECT to_jsonb(a)::text AS value FROM knowledge_artifacts a WHERE account_id=$1",
                "events": "SELECT to_jsonb(e)::text AS value FROM change_events e WHERE account_id=$1",
                "attempts": "SELECT to_jsonb(a)::text AS value FROM execution_attempts a WHERE account_id=$1",
                "work_items": "SELECT to_jsonb(w)::text AS value FROM maintenance_work_items w WHERE account_id=$1",
                "proofs": "SELECT to_jsonb(p)::text AS value FROM context_refresh_proofs p WHERE account_id=$1",
                "relations": "SELECT to_jsonb(r)::text AS value FROM context_relations r JOIN contexts c ON c.id=r.context_id WHERE c.account_id=$1",
                "dependencies": "SELECT to_jsonb(d)::text AS value FROM dependencies d JOIN contexts c ON c.id=d.dependent_id WHERE c.account_id=$1",
                "policies": "SELECT to_jsonb(p)::text AS value FROM access_policies p WHERE account_id=$1",
                "teams": "SELECT to_jsonb(t)::text AS value FROM teams t WHERE account_id=$1",
                "memberships": "SELECT to_jsonb(m)::text AS value FROM team_memberships m JOIN teams t ON t.id=m.team_id WHERE t.account_id=$1",
                "propagation_trace": "SELECT to_jsonb(t)::text AS value FROM propagation_trace t JOIN change_events e ON e.event_id=t.event_id WHERE e.account_id=$1",
            }
            result = {}
            for name, sql in statements.items():
                result[name] = [
                    json.loads(v)
                    for v in sorted(
                        r["value"] for r in await connection.fetch(sql, account_id)
                    )
                ]
            from contexthub.services.acl_service import ACLService
            from contexthub.models.request import RequestContext

            acl = ACLService()
            ctx = RequestContext(account_id=account_id, agent_id=agent_id)
            result["answer_contexts"] = []
            for row in result["contexts"]:
                access = await acl.check_read_access(connection, row["uri"], ctx)
                if access.allowed and not access.field_masks:
                    result["answer_contexts"].append(row)
            return result
