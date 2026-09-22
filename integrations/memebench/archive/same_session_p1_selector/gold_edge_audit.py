"""Scoring-side helpers for the MEME P1 gold-edge audit.

This module is benchmark-only.  Gold labels are accepted only after a run has
produced node and routing traces; none of the helpers call a model or router.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from integrations.memebench.ingest import _norm


AUDIT_SCHEMA_VERSION = "p1-gold-edge-v1"
ENTITY_MATCHER_VERSION = "p1-gold-edge-entity-v1"
FUZZY_MATCH_RULE = "sequence-matcher-v1:ratio>=0.90"
RECLASSIFICATION_REVISION = "p1-gold-edge-v1.1-distinct-same-session-existential"
VALID_STAGES = frozenset(
    {
        "extraction_both",
        "extraction_source",
        "extraction_target",
        "same_session_atomic_boundary",
        "arrival_snapshot",
        "hmax_envelope",
        "candidate_routing",
        "edge_discovery",
        "persistence",
        "none",
        "ambiguous",
    }
)
PAIR_COUNT_FIELDS = (
    "n_pairs_after_extraction",
    "n_pairs_after_temporal_boundary",
    "n_pairs_after_snapshot",
    "n_pairs_after_hmax",
    "n_pairs_after_routing",
    "n_pairs_after_selection",
    "n_pairs_persisted",
)


@dataclass(frozen=True)
class AuditNode:
    node_id: str
    text: str
    session_index: int
    embedding_present: bool = False

    @classmethod
    def from_value(cls, value: "AuditNode | Mapping[str, Any]") -> "AuditNode":
        if isinstance(value, cls):
            return value
        return cls(
            node_id=str(value["node_id"]),
            text=str(value.get("text") or ""),
            session_index=int(value["session_index"]),
            embedding_present=bool(value.get("embedding_present", False)),
        )


@dataclass(frozen=True)
class RouteTrace:
    node_id: str
    session_index: int
    candidate_snapshot_ids: tuple[str, ...] = ()
    candidate_snapshot_hash: str = ""
    candidate_snapshot_size: int = 0
    hmax_candidate_ids: tuple[str, ...] = ()
    hmax_hash: str = ""
    hmax_size: int = 0
    routed_candidate_ids: tuple[str, ...] = ()
    candidate_tier: str = ""
    edge_tier: str = ""
    final_selected_source_ids: tuple[str, ...] = ()
    persisted_source_ids: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: "RouteTrace | Mapping[str, Any]") -> "RouteTrace":
        if isinstance(value, cls):
            return value

        def ids(*names: str) -> tuple[str, ...]:
            for name in names:
                if name in value:
                    return tuple(str(item) for item in (value.get(name) or ()))
            return ()

        snapshot = ids("candidate_snapshot_ids", "snapshot_ids")
        hmax = ids("hmax_candidate_ids", "envelope_ids")
        return cls(
            node_id=str(value["node_id"]),
            session_index=int(value["session_index"]),
            candidate_snapshot_ids=snapshot,
            candidate_snapshot_hash=str(
                value.get("candidate_snapshot_hash", value.get("snapshot_hash", ""))
            ),
            candidate_snapshot_size=int(
                value.get("candidate_snapshot_size", value.get("snapshot_size", len(snapshot)))
            ),
            hmax_candidate_ids=hmax,
            hmax_hash=str(value.get("hmax_hash", value.get("envelope_hash", ""))),
            hmax_size=int(value.get("hmax_size", value.get("envelope_size", len(hmax)))),
            routed_candidate_ids=ids("routed_candidate_ids"),
            candidate_tier=str(value.get("candidate_tier", value.get("cand_tier", ""))),
            edge_tier=str(value.get("edge_tier", "")),
            final_selected_source_ids=ids(
                "final_selected_source_ids", "selected_source_ids", "source_ids"
            ),
            persisted_source_ids=ids("persisted_source_ids"),
        )


@dataclass(frozen=True)
class EntityNodeMapping:
    entity: str
    before_value: Any
    node_ids: tuple[str, ...]
    match_method: str
    match_count: int
    mapping_ambiguous: bool
    first_session_index: int | None
    all_session_indices: tuple[int, ...]
    matcher_version: str = ENTITY_MATCHER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GoldEdgeAuditRecord:
    """The stable ``p1-gold-edge-v1`` record DTO.

    ``details`` holds forward-compatible fields.  Core schema fields always
    occupy the top level when serialized.
    """

    case_key: str
    episode_id: str
    hop: int
    target_entity: str
    policy: str
    gold_edge_id: str
    gold_source_entity: str
    gold_target_entity: str
    first_failure_stage: str
    gold_edge_recalled: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    audit_schema_version: str = AUDIT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            {
                "audit_schema_version": self.audit_schema_version,
                "case_key": self.case_key,
                "episode_id": self.episode_id,
                "hop": self.hop,
                "target_entity": self.target_entity,
                "policy": self.policy,
                "gold_edge_id": self.gold_edge_id,
                "gold_source_entity": self.gold_source_entity,
                "gold_target_entity": self.gold_target_entity,
                "gold_edge_recalled": self.gold_edge_recalled,
                "first_failure_stage": self.first_failure_stage,
            }
        )
        validate_audit_record(payload)
        return payload


def _raw_substring(value: Any, text: str) -> bool:
    needle = str(value or "")
    return bool(needle) and needle in text


def _fuzzy_match(value: Any, text: str) -> bool:
    """Deterministic stdlib-only fuzzy rule, frozen by ``FUZZY_MATCH_RULE``."""

    needle = _norm(value)
    haystack = _norm(text)
    if not needle or not haystack:
        return False
    if SequenceMatcher(None, needle, haystack, autojunk=False).ratio() >= 0.90:
        return True
    # Compare same-width token windows so surrounding prose does not suppress a
    # one-character spelling variation in a value.
    needle_tokens = needle.split()
    haystack_tokens = haystack.split()
    width = len(needle_tokens)
    return any(
        SequenceMatcher(
            None, needle, " ".join(haystack_tokens[index : index + width]), autojunk=False
        ).ratio()
        >= 0.90
        for index in range(max(0, len(haystack_tokens) - width + 1))
    )


def map_gold_entities_to_nodes(
    entities: Mapping[str, Any],
    nodes: Sequence[AuditNode | Mapping[str, Any]],
) -> dict[str, EntityNodeMapping]:
    """Map every entity's before value without discarding duplicate nodes.

    Matching is tiered globally per entity: raw case-sensitive substring,
    normalized substring using :func:`ingest._norm`, then the frozen fuzzy rule.
    A lower tier is considered only when all higher tiers are empty.
    """

    parsed = [AuditNode.from_value(node) for node in nodes]
    preliminary: dict[str, tuple[Any, str, list[AuditNode]]] = {}
    for entity, raw in entities.items():
        before = raw.get("before") if isinstance(raw, Mapping) else getattr(raw, "before", raw)
        exact = [node for node in parsed if _raw_substring(before, node.text)]
        normalized = [
            node
            for node in parsed
            if not exact and _norm(before) and _norm(before) in _norm(node.text)
        ]
        fuzzy = [
            node
            for node in parsed
            if not exact and not normalized and _fuzzy_match(before, node.text)
        ]
        if exact:
            method, matched = "exact", exact
        elif normalized:
            method, matched = "normalized", normalized
        elif fuzzy:
            method, matched = "fuzzy", fuzzy
        else:
            method, matched = "none", []
        preliminary[str(entity)] = (before, method, matched)

    entities_by_node: dict[str, set[str]] = defaultdict(set)
    for entity, (_, _, matched) in preliminary.items():
        for node in matched:
            entities_by_node[node.node_id].add(entity)

    result: dict[str, EntityNodeMapping] = {}
    for entity, (before, method, matched) in preliminary.items():
        sessions = tuple(sorted({node.session_index for node in matched}))
        shared = any(len(entities_by_node[node.node_id]) > 1 for node in matched)
        result[entity] = EntityNodeMapping(
            entity=entity,
            before_value=before,
            node_ids=tuple(node.node_id for node in matched),
            match_method=method,
            match_count=len(matched),
            mapping_ambiguous=len(matched) > 1 or shared,
            first_session_index=sessions[0] if sessions else None,
            all_session_indices=sessions,
        )
    return result


def _pairs_with_trace(
    pairs: Iterable[tuple[AuditNode, AuditNode]],
    traces: Mapping[str, RouteTrace],
    field_name: str,
) -> list[tuple[AuditNode, AuditNode]]:
    return [
        pair
        for pair in pairs
        if pair[0].node_id in set(getattr(traces[pair[1].node_id], field_name))
    ]


def classify_gold_edge(
    *,
    case_key: str,
    episode_id: str,
    hop: int,
    target_entity: str,
    policy: str,
    gold_source_entity: str,
    gold_target_entity: str,
    source_before_value: Any,
    target_before_value: Any,
    nodes: Sequence[AuditNode | Mapping[str, Any]],
    route_traces: Sequence[RouteTrace | Mapping[str, Any]],
    mappings: Mapping[str, EntityNodeMapping] | None = None,
) -> dict[str, Any]:
    """Attribute one gold edge with all-node-pair existential semantics."""

    parsed_nodes = [AuditNode.from_value(node) for node in nodes]
    by_id = {node.node_id: node for node in parsed_nodes}
    if len(by_id) != len(parsed_nodes):
        raise ValueError("node_id values must be unique")
    trace_by_id = {
        trace.node_id: trace
        for trace in (RouteTrace.from_value(item) for item in route_traces)
    }
    if mappings is None:
        mappings = map_gold_entities_to_nodes(
            {
                gold_source_entity: {"before": source_before_value},
                gold_target_entity: {"before": target_before_value},
            },
            parsed_nodes,
        )
    source_mapping = mappings[gold_source_entity]
    target_mapping = mappings[gold_target_entity]
    source_nodes = [by_id[node_id] for node_id in source_mapping.node_ids]
    target_nodes = [by_id[node_id] for node_id in target_mapping.node_ids]
    mapping_ambiguous = (
        source_mapping.mapping_ambiguous or target_mapping.mapping_ambiguous
    )

    all_pairs = [(source, target) for source in source_nodes for target in target_nodes]
    temporal = [
        pair for pair in all_pairs if pair[0].session_index < pair[1].session_index
    ]
    missing_traces = sorted(
        {target.node_id for _, target in temporal if target.node_id not in trace_by_id}
    )
    if missing_traces:
        raise ValueError(f"missing route trace for target nodes: {missing_traces}")

    snapshot = _pairs_with_trace(temporal, trace_by_id, "candidate_snapshot_ids")
    hmax = _pairs_with_trace(snapshot, trace_by_id, "hmax_candidate_ids")
    routed = _pairs_with_trace(hmax, trace_by_id, "routed_candidate_ids")
    selected = _pairs_with_trace(routed, trace_by_id, "final_selected_source_ids")
    persisted = _pairs_with_trace(selected, trace_by_id, "persisted_source_ids")
    counts = dict(
        zip(
            PAIR_COUNT_FIELDS,
            map(len, (all_pairs, temporal, snapshot, hmax, routed, selected, persisted)),
        )
    )

    same_session_pairs = [
        pair for pair in all_pairs if pair[0].session_index == pair[1].session_index
    ]
    distinct_same_session_pairs = [
        pair for pair in same_session_pairs if pair[0].node_id != pair[1].node_id
    ]
    same_session_only = bool(all_pairs and not temporal and same_session_pairs)
    earlier_source_duplicate_exists = bool(temporal)
    shared_identity = bool(
        set(source_mapping.node_ids) & set(target_mapping.node_ids)
    )

    if not source_nodes and not target_nodes:
        stage = "extraction_both"
    elif not source_nodes:
        stage = "extraction_source"
    elif not target_nodes:
        stage = "extraction_target"
    elif not temporal:
        # Multiple matches do not by themselves make attribution ambiguous. If
        # any distinct source/target node pair exists and every feasible pair is
        # blocked by the atomic session boundary, that boundary is the earliest
        # failure. A single shared node claimed by both entities has no auditable
        # edge identity and remains ambiguous.
        stage = (
            "same_session_atomic_boundary"
            if same_session_only and distinct_same_session_pairs
            else "ambiguous"
        )
    elif not snapshot:
        stage = "arrival_snapshot"
    elif not hmax:
        stage = "hmax_envelope"
    elif not routed:
        stage = "candidate_routing"
    elif not selected:
        stage = "edge_discovery"
    elif not persisted:
        stage = "persistence"
    else:
        stage = "none"

    candidate_targets = sorted({target.node_id for _, target in temporal})
    candidate_tiers = sorted(
        {
            trace_by_id[node_id].candidate_tier
            for node_id in candidate_targets
            if trace_by_id[node_id].candidate_tier
        }
    )
    edge_tiers = sorted(
        {
            trace_by_id[node_id].edge_tier
            for node_id in candidate_targets
            if trace_by_id[node_id].edge_tier
        }
    )
    details = {
        "source_before_value": source_before_value,
        "target_before_value": target_before_value,
        "source_extracted_node_ids": list(source_mapping.node_ids),
        "target_extracted_node_ids": list(target_mapping.node_ids),
        "source_match_method": source_mapping.match_method,
        "target_match_method": target_mapping.match_method,
        "mapping_ambiguous": mapping_ambiguous,
        "source_first_session": source_mapping.first_session_index,
        "target_first_session": target_mapping.first_session_index,
        "source_session_indices": list(source_mapping.all_session_indices),
        "target_session_indices": list(target_mapping.all_session_indices),
        "same_session_only": same_session_only,
        "earlier_source_duplicate_exists": earlier_source_duplicate_exists,
        "source_target_node_identity_overlap": shared_identity,
        "candidate_target_node_ids": candidate_targets,
        "source_in_arrival_snapshot": bool(snapshot),
        "source_in_hmax": bool(hmax),
        "source_in_routed_candidates": bool(routed),
        "source_selected_by_final_route": bool(selected),
        "persisted_edge_exists": bool(persisted),
        "candidate_tiers": candidate_tiers,
        "edge_tiers": edge_tiers,
        "entity_matcher_version": ENTITY_MATCHER_VERSION,
        "fuzzy_match_rule": FUZZY_MATCH_RULE,
        **counts,
    }
    record = GoldEdgeAuditRecord(
        case_key=case_key,
        episode_id=episode_id,
        hop=hop,
        target_entity=target_entity,
        policy=policy,
        gold_edge_id=(
            f"{episode_id}|{hop}|{gold_source_entity}|{gold_target_entity}"
        ),
        gold_source_entity=gold_source_entity,
        gold_target_entity=gold_target_entity,
        first_failure_stage=stage,
        gold_edge_recalled=stage == "none",
        details=details,
    )
    return record.to_dict()


def validate_audit_record(record: Mapping[str, Any]) -> None:
    required = {
        "audit_schema_version",
        "case_key",
        "episode_id",
        "hop",
        "target_entity",
        "policy",
        "gold_edge_id",
        "gold_source_entity",
        "gold_target_entity",
        "first_failure_stage",
        "gold_edge_recalled",
        *PAIR_COUNT_FIELDS,
    }
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(f"audit record missing fields: {missing}")
    if record["audit_schema_version"] != AUDIT_SCHEMA_VERSION:
        raise ValueError(f"unsupported audit schema: {record['audit_schema_version']!r}")
    if record["first_failure_stage"] not in VALID_STAGES:
        raise ValueError(f"illegal first_failure_stage: {record['first_failure_stage']!r}")
    counts = [record[name] for name in PAIR_COUNT_FIELDS]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("pair counts must be non-negative integers")
    if any(later > earlier for earlier, later in zip(counts, counts[1:])):
        raise ValueError("pair counts must be monotonically non-increasing")
    if bool(record["gold_edge_recalled"]) != (record["first_failure_stage"] == "none"):
        raise ValueError("gold_edge_recalled is inconsistent with first_failure_stage")


def reclassify_distinct_same_session_record(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Reproduce the frozen v1.1 raw-ambiguous offline correction."""

    validate_audit_record(record)
    output = dict(record)
    output["original_first_failure_stage"] = str(record["first_failure_stage"])
    output["classification_revision"] = RECLASSIFICATION_REVISION
    if (
        record["first_failure_stage"] == "ambiguous"
        and bool(record.get("same_session_only"))
        and not bool(record.get("earlier_source_duplicate_exists"))
        and int(record.get("n_pairs_after_extraction", 0)) > 0
        and int(record.get("n_pairs_after_temporal_boundary", 0)) == 0
        and len(set(map(str, record.get("source_extracted_node_ids", ()))))
        > 0
        and len(set(map(str, record.get("target_extracted_node_ids", ()))))
        > 0
        and any(
            source != target
            for source in map(str, record.get("source_extracted_node_ids", ()))
            for target in map(str, record.get("target_extracted_node_ids", ()))
        )
    ):
        output["first_failure_stage"] = "same_session_atomic_boundary"
        output["gold_edge_recalled"] = False
    validate_audit_record(output)
    return output


def reclassify_distinct_same_session_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    # The frozen v1.1 view was written in deterministic case/edge order, not in
    # append order from the concurrent raw audit journal.
    return sorted(
        (reclassify_distinct_same_session_record(record) for record in records),
        key=lambda record: (
            str(record["case_key"]),
            str(record["gold_edge_id"]),
            str(record["policy"]),
        ),
    )


def canonical_json_bytes(value: Any, *, exclude_fields: Iterable[str] = ()) -> bytes:
    excluded = set(exclude_fields)

    def scrub(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): scrub(value)
                for key, value in item.items()
                if str(key) not in excluded
            }
        if isinstance(item, (list, tuple)):
            return [scrub(value) for value in item]
        if isinstance(item, Path):
            return str(item)
        return item

    return json.dumps(
        scrub(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any, *, exclude_fields: Iterable[str] = ()) -> str:
    return hashlib.sha256(
        canonical_json_bytes(value, exclude_fields=exclude_fields)
    ).hexdigest()


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return canonical_sha256(
        manifest, exclude_fields=("manifest_sha256", "manifest_hash")
    )


def validate_manifest_hash(manifest: Mapping[str, Any]) -> str:
    expected = manifest.get("manifest_sha256", manifest.get("manifest_hash"))
    if not isinstance(expected, str) or not expected:
        raise ValueError("manifest has no hash")
    actual = manifest_sha256(manifest)
    if actual != expected:
        raise ValueError(f"manifest hash mismatch: expected {expected}, got {actual}")
    return actual


def validate_selection_evaluation_disjoint(
    selection_ids: Iterable[str], evaluation_ids: Iterable[str]
) -> None:
    overlap = sorted(set(map(str, selection_ids)) & set(map(str, evaluation_ids)))
    if overlap:
        raise ValueError(f"selection/evaluation overlap: {overlap}")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def formal_path_hashes(root: str | Path) -> dict[str, str]:
    base = Path(root)
    if not base.is_dir():
        raise ValueError(f"formal artifact root is not a directory: {base}")
    paths = sorted(path for path in base.rglob("*") if path.is_file())
    return {path.relative_to(base).as_posix(): sha256_file(path) for path in paths}


def verify_formal_artifact_hashes(
    before: Mapping[str, str], after: Mapping[str, str]
) -> None:
    if dict(before) != dict(after):
        changed = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        raise ValueError(f"formal artifact contamination: changed={changed}")


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    atomic_write_text(path, rendered)


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json_bytes(record) + b"\n"
    with target.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl_tolerant(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL while tolerating only a malformed, unterminated final line."""

    target = Path(path)
    if not target.exists():
        return []
    raw = target.read_bytes()
    lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            is_torn_last = index == len(lines) - 1 and not line.endswith((b"\n", b"\r"))
            if is_torn_last:
                break
            raise ValueError(f"{target}: malformed JSONL line {index + 1}") from None
        if not isinstance(value, dict):
            raise ValueError(f"{target}: JSONL line {index + 1} is not an object")
        records.append(value)
    return records


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def attempt_started_event(
    *, case_key: str, attempt_id: str, attempt_number: int, run_config_hash: str
) -> dict[str, Any]:
    return {
        "event": "attempt_started",
        "case_key": case_key,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "timestamp": utc_timestamp(),
        "run_config_hash": run_config_hash,
    }


def attempt_finished_event(
    *,
    case_key: str,
    attempt_id: str,
    attempt_number: int,
    run_config_hash: str,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    cost_incomplete: bool = False,
) -> dict[str, Any]:
    if status not in {"success", "retryable_error", "fatal_error"}:
        raise ValueError(f"invalid attempt status: {status!r}")
    return {
        "event": "attempt_finished",
        "case_key": case_key,
        "attempt_id": attempt_id,
        "attempt_number": attempt_number,
        "timestamp": utc_timestamp(),
        "run_config_hash": run_config_hash,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "cost_incomplete": bool(cost_incomplete),
    }


def stale_attempts(
    journal: Sequence[Mapping[str, Any]], *, run_config_hash: str | None = None
) -> list[dict[str, Any]]:
    started: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    for event in journal:
        if run_config_hash is not None and event.get("run_config_hash") != run_config_hash:
            continue
        attempt_id = str(event.get("attempt_id") or "")
        if not attempt_id:
            continue
        if event.get("event") == "attempt_started":
            started[attempt_id] = dict(event)
        elif event.get("event") == "attempt_finished":
            finished.add(attempt_id)
    return [event for attempt_id, event in started.items() if attempt_id not in finished]


def recovery_events_for_stale(
    journal: Sequence[Mapping[str, Any]], *, run_config_hash: str
) -> list[dict[str, Any]]:
    return [
        {
            "event": "attempt_recovered",
            "case_key": item["case_key"],
            "attempt_id": item["attempt_id"],
            "attempt_number": item["attempt_number"],
            "timestamp": utc_timestamp(),
            "run_config_hash": run_config_hash,
            "status": "retryable_error",
            "error_type": "stale_attempt",
            "error_message": "attempt_started has no matching attempt_finished",
            "cost_incomplete": True,
        }
        for item in stale_attempts(journal, run_config_hash=run_config_hash)
    ]


def validate_checkpoint_config(
    records: Sequence[Mapping[str, Any]], *, run_config_hash: str
) -> None:
    """Reject reuse or mixing of a checkpoint created for another config."""

    observed = {
        str(record["run_config_hash"])
        for record in records
        if record.get("run_config_hash") is not None
    }
    mismatched = sorted(observed - {run_config_hash})
    if mismatched:
        raise ValueError(
            "run config hash mismatch; use a new output directory: "
            f"expected={run_config_hash}, observed={mismatched}"
        )


def _records_by_attempt(
    records: Sequence[Mapping[str, Any]], *, run_config_hash: str
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("run_config_hash") != run_config_hash:
            continue
        grouped[(str(record.get("case_key")), str(record.get("attempt_id")))].append(
            dict(record)
        )
    return grouped


def last_complete_successes(
    journal: Sequence[Mapping[str, Any]],
    gold_edge_records: Sequence[Mapping[str, Any]],
    *,
    run_config_hash: str,
    expected_gold_edges_by_case: Mapping[str, int],
    case_success_records: Sequence[Mapping[str, Any]] = (),
    expected_gold_edge_ids_by_case: Mapping[str, Iterable[str]] | None = None,
    expected_gold_edge_ids_hash_by_case: Mapping[str, str] | None = None,
    expected_case_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return each case's last successful attempt that is actually complete."""

    grouped = _records_by_attempt(gold_edge_records, run_config_hash=run_config_hash)
    cleanup_by_attempt = {
        (str(item.get("case_key")), str(item.get("attempt_id"))): bool(
            item.get("account_cleanup_complete")
        )
        for item in case_success_records
        if item.get("run_config_hash") == run_config_hash
    }
    require_success_record = bool(case_success_records)
    complete: dict[str, dict[str, Any]] = {}
    for event in journal:
        if (
            event.get("event") != "attempt_finished"
            or event.get("status") != "success"
            or event.get("run_config_hash") != run_config_hash
        ):
            continue
        case_key = str(event.get("case_key"))
        attempt_id = str(event.get("attempt_id"))
        expected = expected_gold_edges_by_case.get(case_key)
        records = grouped.get((case_key, attempt_id), [])
        try:
            for record in records:
                validate_audit_record(record)
        except ValueError:
            continue
        unique_edges = {str(record["gold_edge_id"]) for record in records}
        if expected is None or len(records) != expected or len(unique_edges) != expected:
            continue
        if expected_gold_edge_ids_by_case is not None:
            expected_ids = set(map(str, expected_gold_edge_ids_by_case.get(case_key, ())))
            if unique_edges != expected_ids:
                continue
        if expected_gold_edge_ids_hash_by_case is not None:
            if canonical_sha256(sorted(unique_edges)) != expected_gold_edge_ids_hash_by_case.get(
                case_key
            ):
                continue
        if expected_case_metadata is not None:
            metadata = expected_case_metadata.get(case_key)
            if metadata is None or any(
                any(record.get(field) != value for record in records)
                for field, value in metadata.items()
            ):
                continue
        if require_success_record and not cleanup_by_attempt.get((case_key, attempt_id), False):
            continue
        complete[case_key] = {
            "attempt": dict(event),
            "records": records,
            "account_cleanup_complete": (
                cleanup_by_attempt.get((case_key, attempt_id), True)
            ),
        }
    return complete


def completeness_report(
    expected_case_keys: Iterable[str],
    journal: Sequence[Mapping[str, Any]],
    gold_edge_records: Sequence[Mapping[str, Any]],
    *,
    run_config_hash: str,
    expected_gold_edges_by_case: Mapping[str, int],
    case_success_records: Sequence[Mapping[str, Any]] = (),
    expected_gold_edge_ids_by_case: Mapping[str, Iterable[str]] | None = None,
    expected_gold_edge_ids_hash_by_case: Mapping[str, str] | None = None,
    expected_case_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    expected = tuple(dict.fromkeys(map(str, expected_case_keys)))
    successes = last_complete_successes(
        journal,
        gold_edge_records,
        run_config_hash=run_config_hash,
        expected_gold_edges_by_case=expected_gold_edges_by_case,
        case_success_records=case_success_records,
        expected_gold_edge_ids_by_case=expected_gold_edge_ids_by_case,
        expected_gold_edge_ids_hash_by_case=expected_gold_edge_ids_hash_by_case,
        expected_case_metadata=expected_case_metadata,
    )
    stale = stale_attempts(journal, run_config_hash=run_config_hash)
    latest_status: dict[str, str] = {}
    for event in journal:
        if (
            event.get("event") == "attempt_finished"
            and event.get("run_config_hash") == run_config_hash
        ):
            latest_status[str(event.get("case_key"))] = str(event.get("status"))
    success_keys = sorted(set(expected) & successes.keys())
    missing = sorted(set(expected) - set(success_keys))
    return {
        "run_config_hash": run_config_hash,
        "expected_cases": len(expected),
        "success_cases": len(success_keys),
        "success_case_keys": success_keys,
        "missing_case_keys": missing,
        "retryable_case_keys": sorted(
            key for key in missing if latest_status.get(key) == "retryable_error"
        ),
        "fatal_case_keys": sorted(
            key for key in missing if latest_status.get(key) == "fatal_error"
        ),
        "stale_attempt_ids": sorted(str(item["attempt_id"]) for item in stale),
        "stale_cases": len(stale),
        "complete": not missing and not stale,
    }


def records_from_last_complete_successes(
    journal: Sequence[Mapping[str, Any]],
    gold_edge_records: Sequence[Mapping[str, Any]],
    *,
    run_config_hash: str,
    expected_gold_edges_by_case: Mapping[str, int],
    case_success_records: Sequence[Mapping[str, Any]] = (),
    expected_gold_edge_ids_by_case: Mapping[str, Iterable[str]] | None = None,
    expected_gold_edge_ids_hash_by_case: Mapping[str, str] | None = None,
    expected_case_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    successes = last_complete_successes(
        journal,
        gold_edge_records,
        run_config_hash=run_config_hash,
        expected_gold_edges_by_case=expected_gold_edges_by_case,
        case_success_records=case_success_records,
        expected_gold_edge_ids_by_case=expected_gold_edge_ids_by_case,
        expected_gold_edge_ids_hash_by_case=expected_gold_edge_ids_hash_by_case,
        expected_case_metadata=expected_case_metadata,
    )
    return [
        record
        for case_key in sorted(successes)
        for record in successes[case_key]["records"]
    ]


def summarize_audit(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for record in records:
        validate_audit_record(record)
    stages = Counter(str(record["first_failure_stage"]) for record in records)
    stage_ambiguous = stages["ambiguous"]
    mapping_ambiguous = sum(bool(record.get("mapping_ambiguous")) for record in records)
    recalled = sum(bool(record["gold_edge_recalled"]) for record in records)
    episode_misses: dict[tuple[Any, ...], bool] = defaultdict(bool)
    by_hop_policy_stage: Counter[tuple[Any, ...]] = Counter()
    episodes_by_stage: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for record in records:
        episode_key = (record["episode_id"], record["hop"], record["policy"])
        episode_misses[episode_key] |= not bool(record["gold_edge_recalled"])
        stage = str(record["first_failure_stage"])
        by_hop_policy_stage[(record["hop"], record["policy"], stage)] += 1
        episodes_by_stage[stage].add(
            (str(record["episode_id"]), int(record["hop"]))
        )
    total = len(records)
    unique = total - stage_ambiguous
    return {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "gold_edge_total": total,
        "gold_edge_recalled": recalled,
        "gold_edge_recall": recalled / total if total else None,
        "uniquely_classified_edges": unique,
        "uniquely_classified_ratio": unique / total if total else None,
        "ambiguous_edges": stage_ambiguous,
        "ambiguous_ratio": stage_ambiguous / total if total else None,
        "mapping_ambiguous_edges": mapping_ambiguous,
        "mapping_ambiguous_ratio": (
            mapping_ambiguous / total if total else None
        ),
        "stage_counts": dict(sorted(stages.items())),
        "stage_ratios": {
            stage: count / total if total else None
            for stage, count in sorted(stages.items())
        },
        "stage_episode_counts": {
            stage: len(episodes) for stage, episodes in sorted(episodes_by_stage.items())
        },
        "episode_any_miss_count": sum(episode_misses.values()),
        "by_hop_policy_stage": [
            {"hop": hop, "policy": policy, "stage": stage, "count": count}
            for (hop, policy, stage), count in sorted(by_hop_policy_stage.items())
        ],
        "oracle_repair_upper_bounds": oracle_repair_upper_bounds(records),
    }


def oracle_repair_upper_bounds(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    groups = {
        "extraction": {"extraction_both", "extraction_source", "extraction_target"},
        "same_session_atomic_boundary": {"same_session_atomic_boundary"},
        "snapshot_hmax_candidate": {
            "arrival_snapshot",
            "hmax_envelope",
            "candidate_routing",
        },
        "edge_discovery": {"edge_discovery"},
        "persistence": {"persistence"},
    }
    output: dict[str, dict[str, int]] = {}
    for name, stages in groups.items():
        matching = [
            record for record in records if record.get("first_failure_stage") in stages
        ]
        output[name] = {
            "max_recoverable_edges": len(matching),
            "max_recoverable_episodes": len(
                {
                    (str(record.get("episode_id")), int(record.get("hop")))
                    for record in matching
                }
            ),
        }
    return output


# Compatibility aliases kept explicit for the runner and external audit scripts.
compute_formal_path_hashes = formal_path_hashes
assert_formal_artifacts_unchanged = verify_formal_artifact_hashes
assert_no_selection_evaluation_overlap = validate_selection_evaluation_disjoint
read_jsonl = read_jsonl_tolerant
append_jsonl_fsync = append_jsonl
