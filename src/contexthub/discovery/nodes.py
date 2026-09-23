"""Minimal proposition carrier in the existing versioned context store."""

from uuid import uuid5
from contexthub.models.knowledge import ContextVersion, ContractError
from contexthub.services.artifact_service import require_account


async def materialize_propositions(db, artifact, *, proposition_ids=None):
    """One immutable carrier per artifact/ordinal, never per source/entity label.

    Inherits source scope/owner. Mixed visibility is rejected, not broadened.
    Existing context INSERT version trigger supplies context_versions v1.
    This is an internal tenant-scoped operation; API authorization stays at S7.
    """
    await require_account(db, artifact.account_id)
    mappings = []
    for proposition in artifact.propositions:
        if proposition_ids is not None and proposition.id not in proposition_ids:
            continue
        scopes = []
        for ref in proposition.evidence:
            row = await db.fetchrow(
                """SELECT c.scope,c.owner_space FROM contexts c
                JOIN knowledge_artifacts a ON (a.payload->'source'->>'context_id')::uuid=c.id
                WHERE a.account_id=$1 AND a.id=$2 AND c.account_id=$1""",
                artifact.account_id,
                ref.source.id,
            )
            if row is None:
                raise ContractError("source_context_missing")
            scopes.append((row["scope"], row["owner_space"]))
        if len(set(scopes)) != 1:
            raise ContractError("mixed_source_visibility")
        scope, owner = scopes[0]
        node_id = uuid5(artifact.id, "proposition-context:" + str(proposition.id))
        await db.execute(
            """INSERT INTO contexts(id,account_id,uri,context_type,scope,owner_space,l2_content)
            VALUES($1,$2,$3,'memory',$4,$5,$6) ON CONFLICT(id) DO NOTHING""",
            node_id,
            artifact.account_id,
            "ctx://proposition/" + str(node_id),
            scope,
            owner,
            proposition.text,
        )
        row = await db.fetchrow(
            """SELECT v.l2_content FROM context_versions v JOIN contexts c ON c.id=v.context_id
            WHERE c.account_id=$1 AND v.context_id=$2 AND v.version=$3""",
            artifact.account_id,
            node_id,
            1,
        )
        if row is None or row["l2_content"] != proposition.text:
            raise ContractError("proposition_carrier_mismatch")
        mappings.append((proposition.id, ContextVersion(context_id=node_id, version=1)))
    return tuple(mappings)
