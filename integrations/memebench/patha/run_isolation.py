"""Experiment account boundaries; no changes to the core's ACL history policy.

S5 intentionally reads tenant history. Each RQ1 invocation must therefore own
an unused account, and its selective snapshot must contain only that prepared
chain and graph. Nothing is filtered by relevance, question, or scoring labels.
"""

from contexthub.models.knowledge import ContractError


async def require_empty_run_account(repository, account_id):
    """Reject previous or failed runs before constructing any model client."""
    async with repository.session(account_id) as db:
        occupied = await db.fetchval(
            """SELECT EXISTS(SELECT 1 FROM contexts WHERE account_id=$1)
            OR EXISTS(SELECT 1 FROM knowledge_artifacts WHERE account_id=$1)
            OR EXISTS(SELECT 1 FROM execution_attempts WHERE account_id=$1)""",
            account_id,
        )
        if occupied:
            raise ContractError("rq1_run_account_not_empty")


async def require_closed_history(connection, handoff, graph):
    """Check the same repeatable-read state exported by capture, including ACL-
    hidden/deleted contexts and old versions. All original filler is retained.

    This is a quiescent experiment boundary, not a concurrent tenant reservation.
    The caller must exclusively own the account through capture and each copy's
    maintenance. A second graph created after the entry check rejects here.
    """
    expected = {(cv.context_id, cv.version) for cv in (*handoff.sources, *graph.nodes)}
    rows = await connection.fetch(
        """SELECT c.id,v.version FROM contexts c
        LEFT JOIN context_versions v ON v.context_id=c.id
        WHERE c.account_id=$1""",
        handoff.account_id,
    )
    if {(row["id"], row["version"]) for row in rows} != expected:
        raise ContractError("snapshot_foreign_history")
    # A second arm can reuse the same source/carrier IDs. Its discovery seals
    # still reveal the extra graph; source identity alone cannot prove isolation.
    expected_seals = {
        "prepared_input": {ref.id for ref in handoff.prepared},
        "discovery_report": {ref.id for ref in handoff.reports},
        "graph_handoff": {handoff.id},
    }
    rows = await connection.fetch(
        """SELECT kind,id FROM knowledge_artifacts
        WHERE account_id=$1 AND kind=ANY($2::text[])""",
        handoff.account_id,
        list(expected_seals),
    )
    for kind, ids in expected_seals.items():
        if {row["id"] for row in rows if row["kind"] == kind} != ids:
            raise ContractError("snapshot_foreign_discovery")
