"""Session transaction reducer, independent of storage and semantic inference.

This is the new protocol's state boundary, not a replacement for the SQL
executor. Semantic adapters must supply grounded edits, dependency decisions
and verifier receipts. Merely reading a source never creates a confirmed edge.
"""

from typing import Literal

from pydantic import Field

from contexthub.models.knowledge import Contract, ContractError, canonical_hash


class Node(Contract):
    id: str
    version: int = Field(strict=True, ge=1)
    basis_revision: int = Field(strict=True, ge=0, default=0)
    text: str
    kind: Literal["assertion", "rule", "derived", "uncertain"]
    status: Literal["fresh", "stale", "unresolved", "deleted"] = "unresolved"
    # Immutable source evidence references, resolved by the semantic adapter.
    evidence: tuple[str, ...] = Field(min_length=1)


class Edge(Contract):
    upstream: str
    downstream: str
    status: Literal["confirmed", "pending"]
    evidence: tuple[str, ...] = Field(min_length=1)
    judgment: Literal["memory", "raw", "deterministic"] = "deterministic"


class Edit(Contract):
    id: str
    expected_version: int = Field(strict=True, ge=0)
    text: str
    kind: Literal["assertion", "rule", "derived", "uncertain"]
    evidence: tuple[str, ...] = Field(min_length=1)
    deleted: bool = False
    basis_only: bool = False


class Delta(Contract):
    basis_sha256: str
    edits: tuple[Edit, ...]
    # Replace incoming edges only for explicitly searched targets. Old edges
    # remain in the invalidation union even when absent from this new result.
    searched: tuple[str, ...]
    edges: tuple[Edge, ...]
    complete: bool
    invalidated: tuple[str, ...] = ()


class Verification(Contract):
    basis_sha256: str
    target: str
    target_version: int = Field(strict=True, ge=1)
    upstream_versions: tuple[tuple[str, int], ...]
    supported: bool
    applicable: bool
    conflicts_resolved: bool
    coverage_complete: bool
    evidence: tuple[str, ...] = Field(min_length=1)
    receipt_id: str


class SessionState(Contract):
    revision: int = Field(strict=True, ge=0, default=0)
    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    pending_targets: tuple[str, ...] = ()


def descendants(seeds, edges):
    found = set(seeds)
    while True:
        extra = {e.downstream for e in edges if e.upstream in found} - found
        if not extra:
            return found
        found.update(extra)


def apply_delta(state: SessionState, delta: Delta):
    """Pure atomic transition: retain the old graph while invalidating its union.

    Versions and status are assigned here, never accepted from model output.
    Evidence grounding/semantic correctness remains the adapter's responsibility.
    """
    if delta.basis_sha256 != canonical_hash(state):
        raise ContractError("session_basis_drift")
    old = {n.id: n for n in state.nodes}
    if len(old) != len(state.nodes) or len({e.id for e in delta.edits}) != len(delta.edits):
        raise ContractError("session_duplicate_node")
    nodes = dict(old)
    changed = set()
    for edit in delta.edits:
        previous = old.get(edit.id)
        if edit.expected_version != (previous.version if previous else 0):
            raise ContractError("session_edit_version_drift")
        if previous and previous.status == "deleted":
            raise ContractError("session_deleted_identity_requires_new_node")
        if edit.basis_only and (previous is None or edit.deleted or
                (edit.text, edit.kind) != (previous.text, previous.kind)):
            raise ContractError("session_basis_edit_changes_content")
        changed.add(edit.id)
        nodes[edit.id] = Node(
            id=edit.id, version=edit.expected_version + (0 if edit.basis_only else 1),
            basis_revision=(previous.basis_revision + 1) if previous else 0, text=edit.text,
            kind=edit.kind, evidence=edit.evidence,
            status="deleted" if edit.deleted else "unresolved",
        )
    searched = set(delta.searched)
    if len(searched) != len(delta.searched) or not searched <= nodes.keys():
        raise ContractError("session_search_targets_invalid")
    for edge in delta.edges:
        if (edge.upstream not in nodes or edge.downstream not in searched
                or edge.upstream == edge.downstream):
            raise ContractError("session_edge_binding_invalid")
    if len({(e.upstream, e.downstream) for e in delta.edges}) != len(delta.edges):
        raise ContractError("session_duplicate_edge")
    # An incomplete search cannot erase even a candidate dependency.
    retained = tuple(e for e in state.edges if e.downstream not in searched)
    edges = retained + delta.edges
    if not delta.complete:
        pairs = {(e.upstream, e.downstream) for e in edges}
        edges += tuple(e for e in state.edges if (e.upstream, e.downstream) not in pairs)
    changed_edges = {
        e.downstream for e in (*state.edges, *edges)
        if (e in state.edges) != (e in edges)
    }
    invalidated = set(delta.invalidated)
    if not invalidated <= nodes.keys():
        raise ContractError("session_invalidation_target_invalid")
    affected = descendants(changed | changed_edges | invalidated, (*state.edges, *edges))
    pending = (set(state.pending_targets) - searched) if delta.complete else set(state.pending_targets) | searched
    pending |= changed - searched
    pending |= {e.downstream for e in edges if e.status == "pending"}
    affected |= descendants(pending, (*state.edges, *edges))
    for ident in affected:
        node = nodes[ident]
        if node.status != "deleted":
            nodes[ident] = node.model_copy(update={"status": "stale" if ident in old else "unresolved"})
    return SessionState(
        revision=state.revision + 1, nodes=tuple(nodes.values()), edges=edges,
        pending_targets=tuple(sorted(pending)),
    ), tuple(sorted(affected))


def release_fresh(state: SessionState, proof: Verification):
    """CAS release after semantic verification; stale upstreams block release.

    A basis hash includes the graph, all source versions and statuses visible
    here. Storage adapters must compare-and-swap this basis at commit too.
    """
    if proof.basis_sha256 != canonical_hash(state):
        raise ContractError("session_verification_basis_drift")
    nodes = {n.id: n for n in state.nodes}
    target = nodes.get(proof.target)
    if target is None or target.version != proof.target_version or target.status == "deleted":
        raise ContractError("session_verification_target_invalid")
    parents = {e.upstream for e in state.edges if e.downstream == target.id}
    expected = tuple(sorted((p, nodes[p].version) for p in parents))
    if tuple(sorted(proof.upstream_versions)) != expected:
        raise ContractError("session_verification_upstreams_incomplete")
    if (target.id in state.pending_targets or target.kind == "uncertain"
            or any(nodes[p].status != "fresh" for p in parents)
            or not all((proof.supported, proof.applicable, proof.conflicts_resolved, proof.coverage_complete))):
        raise ContractError("session_fresh_not_supported")
    if target.kind == "derived" and not parents:
        raise ContractError("session_derived_without_basis")
    nodes[target.id] = target.model_copy(update={"status": "fresh"})
    return state.model_copy(update={"revision": state.revision + 1, "nodes": tuple(nodes.values())})
