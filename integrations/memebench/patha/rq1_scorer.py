"""Offline RQ1 necessary-path scoring, not the S8 aggregate/statistical scorer.

Semantic correspondence is an explicit, versioned offline review input. The
scorer NEVER invents certification from reachability, similarity or DEPENDS.
"""

import json
import re
from typing import Literal, Mapping
from pydantic import Field
from contexthub.models.knowledge import (
    Contract,
    ContextVersion,
    EvidenceRef,
    GraphSnapshot,
    SealRef,
    SourceSnapshot,
    Hash,
    Name,
    canonical_hash,
    text_hash,
)
from contexthub.discovery.contracts import NodeMapping
from .scoring_contract import validate_scoring_contract, ScoringContractError
from .schemas import canonical_sha256


class StepReview(Contract):
    # Exact ordered node versions and edge identity, reviewed against one frozen
    # reference step. PASS means a reviewer checked the actual semantic relation.
    upstream: ContextVersion
    downstream: ContextVersion
    edge_sha256: Hash
    reference_step_sha256: Hash
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    verdict: Literal["PASS", "FAIL", "UNKNOWN"]
    rationale: Name


class PathReview(Contract):
    record_id: Name
    path_id: Name
    reference_record_sha256: Hash
    graph: SealRef
    reviewer_id: Name
    review_method: Literal["offline_human_semantic_review"]
    review_version: Name
    # Node mapping is ordered exactly as the frozen reference path, with each
    # intermediate node present; labels never enter the runtime mapping.
    nodes: tuple[ContextVersion, ...]
    steps: tuple[StepReview, ...]


class GraphEvidence(Contract):
    graph: GraphSnapshot
    reference: SealRef
    status: Literal["complete", "pending", "timeout", "error"]
    mappings: tuple[NodeMapping, ...]
    # Allowed-input opaque source ID -> exact persisted source snapshot.
    source_map: tuple[tuple[str, SealRef], ...]
    # Original source text exported from EvidenceService, indexed by seal hash.
    source_texts: dict[str, str]
    source_snapshots: tuple[SourceSnapshot, ...]


def _witness_span(original, witness):
    """Locate a frozen turn's content token in the *original* JSON bytes/text.

    Decoding then re-encoding changes escaping/whitespace and loses locations.
    raw_decode instead walks array elements and object fields at their original
    offsets. Identical strings in another turn or nested metadata do not match.
    The returned interval excludes the JSON string's enclosing quotes.
    """
    index = witness["turn_index"]
    if type(index) is not int or index < 0:
        return None
    try:
        messages = json.loads(original)
    except ValueError:
        # Historical generic synthetic plain sources, exact whole-source only.
        return (
            (0, len(original))
            if index == 0 and original == witness["excerpt"]
            else None
        )
    if not isinstance(messages, list) or index >= len(messages):
        return None
    turn = messages[index]
    if (
        not isinstance(turn, dict)
        or turn.get("content") != witness["excerpt"]
        or turn.get("role") != witness.get("role", turn.get("role"))
    ):
        return None
    decoder = json.JSONDecoder()

    def skip(pos):
        while pos < len(original) and original[pos] in " \t\r\n":
            pos += 1
        return pos

    pos = skip(0) + 1  # opening array, already validated by json.loads
    for ordinal in range(index + 1):
        pos = skip(pos)
        _, end = decoder.raw_decode(original, pos)
        if ordinal != index:
            pos = skip(end) + 1  # comma
    pos = skip(pos + 1)  # target object's opening brace
    span = None
    keys = set()
    while original[pos] != "}":
        key, end = decoder.raw_decode(original, pos)
        if key in keys:
            return None  # duplicate keys have no unambiguous field identity
        keys.add(key)
        start = skip(skip(end) + 1)  # colon
        value, end = decoder.raw_decode(original, start)
        if key == "content" and isinstance(value, str):
            span = (start + 1, end - 1)
        pos = skip(end)
        if original[pos] == ",":
            pos = skip(pos + 1)
    return span


def _cites_witness(evidence, ref, original, span):
    if span is None:
        return False
    start, end = span
    # A citation may be narrow, a whole turn, or a larger source span. Require
    # nonempty overlap with the exact content interval, at complete JSON escape
    # boundaries. Mere quotes, metadata, or fragments of an escape do not count.
    boundaries = {start}
    boundaries.update(
        start + m.end()
        for m in re.finditer(r"\\u[0-9a-fA-F]{4}|\\.|[^\\]", original[start:end])
    )
    return any(
        e.source == ref
        and max(e.start, start) < min(e.end, end)
        and max(e.start, start) in boundaries
        and min(e.end, end) in boundaries
        for e in evidence
    )


def _path_reason(reference, review, delivered):
    graph = delivered.graph
    if delivered.status != "complete":
        return "run_" + delivered.status
    if graph.reference() != delivered.reference or review.graph != delivered.reference:
        return "graph_identity_mismatch"
    if not graph.nodes:
        return "empty_graph"
    expected = reference["necessary_path_candidates"]
    path = next((p for p in expected if p["path_id"] == review.path_id), None)
    if path is None:
        return "reference_path_missing"
    if review.reference_record_sha256 != reference["record_sha256"]:
        return "reference_version_mismatch"
    if path.get("direction") != "event_root_to_target" or not path.get(
        "p2_traversable_if_all_edges_delivered"
    ):
        return "reference_direction_or_traversal_unknown"
    if len(review.nodes) != len(path["entities"]) or len(review.steps) != len(
        path["edges"]
    ):
        return "incomplete_path"
    if len(review.nodes) != len(review.steps) + 1 or len(review.nodes) < 2:
        return "incomplete_path"
    mapped = {m.node for m in delivered.mappings}
    if any(n not in graph.nodes or n not in mapped for n in review.nodes):
        return "node_or_version_missing"
    source_map = dict(delivered.source_map)
    for index, (step, ref_step) in enumerate(zip(review.steps, path["edges"])):
        if step.verdict != "PASS":
            return "semantic_" + step.verdict.lower()
        if step.reference_step_sha256 != canonical_sha256(ref_step):
            return "reference_step_mismatch"
        if (step.upstream, step.downstream) != (
            review.nodes[index],
            review.nodes[index + 1],
        ):
            return "disconnected_mapping"
        if any(
            p.upstream == step.upstream and p.downstream == step.downstream
            for p in graph.pending
        ):
            return "pending_edge"
        edge = next(
            (
                e
                for e in graph.edges
                if e.upstream == step.upstream and e.downstream == step.downstream
            ),
            None,
        )
        if edge is None:
            return "missing_or_reversed_edge"
        if canonical_hash(edge) != step.edge_sha256:
            return "edge_version_mismatch"
        if not edge.semantic_basis or not edge.provenance:
            return "semantic_basis_missing"
        for ev in (*edge.evidence, *step.evidence):
            original = delivered.source_texts.get(ev.source.sha256)
            if (
                original is None
                or original[ev.start : ev.end] != ev.excerpt
                or text_hash(ev.excerpt) != ev.excerpt_sha256
            ):
                return "invalid_original_evidence"
            snapshot = next(
                (s for s in delivered.source_snapshots if s.reference() == ev.source),
                None,
            )
            if (
                snapshot is None
                or snapshot.account_id != graph.account_id
                or text_hash(original) != snapshot.content_sha256
            ):
                return "source_snapshot_mismatch"
            if ev.source not in source_map.values():
                return "source_version_mismatch"
        if any(e not in edge.evidence for e in step.evidence):
            return "review_evidence_not_on_edge"
        # At least one frozen dialogue witness must occur in an actual cited
        # source/turn. Metadata/gold-only declarations are not semantic evidence.
        witnesses = ref_step.get("dialogue_evidence", [])
        matched = False
        for witness in witnesses:
            ref = source_map.get(witness["source_id"])
            if ref is None:
                continue
            original = delivered.source_texts.get(ref.sha256)
            if (
                original is None
                or text_hash(witness["excerpt"]) != witness["excerpt_sha256"]
            ):
                continue
            if _cites_witness(
                step.evidence, ref, original, _witness_span(original, witness)
            ):
                matched = True
        if not matched:
            return "reference_witness_not_supported"
    return None


def score_rq1(
    contract,
    paths,
    graphs: Mapping[str, GraphEvidence],
    reviews: tuple[PathReview, ...],
):
    """One row for every frozen event-target, including missing/failed runs."""
    validate_scoring_contract(contract)
    universe = contract["frozen_record_universe"]["record_ids"]
    path_map = {p["record_id"]: p for p in paths}
    if len(path_map) != len(paths) or set(path_map) != set(universe):
        raise ScoringContractError("path records must match frozen universe exactly")
    for path in paths:
        payload = dict(path)
        digest = payload.pop("record_sha256")
        if canonical_sha256(payload) != digest:
            raise ScoringContractError("path record hash mismatch")
    if any(r.record_id not in path_map for r in reviews):
        raise ScoringContractError("review outside frozen universe")
    rows = []
    for record in universe:
        reference = path_map[record]
        graph = graphs.get(reference["episode_id"])
        applicable = [r for r in reviews if r.record_id == record]
        reasons = []
        hit = None
        if graph is None:
            reasons = ["missing_result"]
        elif graph.status != "complete":
            reasons = ["run_" + graph.status]
        elif not applicable:
            reasons = ["semantic_mapping_not_reviewed"]
        else:
            for review in applicable:
                reason = _path_reason(reference, review, graph)
                if reason is None:
                    hit = review.path_id
                    break
                reasons.append(reason)
        rows.append(
            dict(
                record_id=record,
                episode_id=reference["episode_id"],
                covered=hit is not None,
                matched_path_id=hit,
                reasons=sorted(set(reasons)) if hit is None else [],
                retained_in_denominator=True,
                data_quality_notes=reference.get("data_quality_notes", []),
            )
        )
    return {
        "scorer": "patha-rq1-s4-v2",
        "denominator": len(rows),
        "covered": sum(r["covered"] for r in rows),
        "rows": rows,
        "semantic_certification": "explicit offline review plus exact evidence/version checks",
        "s8_statistics_complete": False,
    }


async def load_graph_evidence(db, *, graph_ref, report_refs, source_map, status):
    """Export real sealed/runtime evidence for offline scoring; no labels needed."""
    from contexthub.services.artifact_service import ArtifactService
    from contexthub.services.evidence_service import EvidenceService

    artifacts = ArtifactService()
    evidence = EvidenceService(artifacts)
    graph = await artifacts.get(db, graph_ref)
    mappings = {}
    for ref in report_refs:
        report = await artifacts.get(db, ref)
        for mapping in report.mappings:
            mappings[mapping.node] = mapping
    snapshots = []
    texts = {}
    for _, ref in source_map:
        snapshot = await artifacts.get(db, ref)
        snapshots.append(snapshot)
        texts[ref.sha256] = await evidence.read_source(db, snapshot)
    return GraphEvidence(
        graph=graph,
        reference=graph_ref,
        status=status,
        mappings=tuple(mappings.values()),
        source_map=source_map,
        source_texts=texts,
        source_snapshots=tuple(snapshots),
    )
