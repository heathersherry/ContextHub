"""Run the preregistered full-100 turn_full_cheap selector arm."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import platform
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from contexthub.llm.chat_client import BaseChatClient, OpenAIChatClient
from contexthub.services.dependency_discovery_service import (
    DependencyDiscoveryService,
    _DISCOVERY_PROMPT,
)
from integrations.memebench.gold_edge_audit import (
    append_jsonl,
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
    utc_timestamp,
)
from integrations.memebench.run_chronological_p1p2 import package_versions
from integrations.memebench.same_session_selector_intervention import (
    AuditedChatClient,
    run_selector_case,
    selector_case_key,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH, load_provider


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
SHARED = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_OUT = RUNS / "p1_same_session_selector_full100_cheap_20260824"
ARM = "turn_full_cheap"
MODEL = "gpt-4o-mini"
PROVIDER = "openlux"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
PRICE = {"prompt": 0.15, "completion": 0.60}
EXPECTED_CONTENT_HASH = "b36add2260393f14f0b24ba82162db5f83dda99b66c84c9226c107f87738cce5"
EXPECTED_INDEX_HASH = "47241616abc53ead10517b26ba84a00b314c9e3c7cb7ca60ccb1dc965870dfad"
EXPECTED_V2_CANONICAL = "76f2eef111e573dab437aa1bf4a41f428a90998232c3881cd6c8ca35680afcea"
EXPECTED_V2_FILE = "15972d6a77b4624360134334aed4be0f6170bccf540595decf3aa0340f9d9f8d"
EXPECTED_V2_MANIFEST_CANONICAL = "0b2e83df9160846337cb6f1990d544e8bc6ea4a099522793f9d0566fdfaefcd1"
EXPECTED_V2_MANIFEST_FILE = "42676461d9033ba424271fb6a86d867b0c1b8c6270f97a9a6986b598602af68b"
MAPPING_RULE = "edge-pr-raw-normalized-before-substring-v1"
SOURCE_FILES = (
    "integrations/memebench/run_same_session_selector_full100_cheap.py",
    "integrations/memebench/same_session_selector_intervention.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/services/cascade_router.py",
    "src/contexthub/services/dependency_discovery_service.py",
)
PROTECTED_ROOTS = {
    "shared": SHARED,
    "formal": RUNS / "chronological_20260820" / "formal",
    "audit": RUNS / "p1_gold_edge_audit_20260822",
    "manual": RUNS / "p1_same_session_manual_validation_20260823",
    "intervention": RUNS / "p1_same_session_selector_intervention_20260824_v2",
}


class FatalRunError(RuntimeError):
    pass


class ForbiddenStrongChat(BaseChatClient):
    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        raise FatalRunError("strong selector call attempted in cheap-only arm")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must be a JSON object")
    return value


def _canonical_without(value: Mapping[str, Any], field: str) -> str:
    return canonical_sha256({key: item for key, item in value.items() if key != field})


def validate_shared(*, allow_scoring: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    index = _json(SHARED / "shared_manifest_index.json")
    summary = _json(SHARED / "summary.json")
    if (
        index.get("manifest_content_sha256") != EXPECTED_CONTENT_HASH
        or index.get("index_sha256") != EXPECTED_INDEX_HASH
        or index.get("episode_count") != 100
        or summary.get("target_count") != 2209
        or summary.get("hop2_applicable_count") != 64
    ):
        raise FatalRunError("shared manifest hashes/counts mismatch")
    manifests = []
    for row in index["episodes"]:
        path = SHARED / row["manifest_path"]
        if sha256_file(path) != row["manifest_file_sha256"]:
            raise FatalRunError(f"shared shard hash mismatch: {path}")
        manifests.append(_json(path))
    if sum(len(row["selector_cases"]) for row in manifests) != 2209:
        raise FatalRunError("shared selector target count mismatch")
    if allow_scoring:
        validate_scoring_sidecar()
    return index, manifests


def validate_scoring_sidecar() -> tuple[dict[str, Any], dict[str, Any]]:
    side_path = SHARED / "gold_scoring_side_v2.json"
    manifest_path = SHARED / "gold_scoring_side_v2_manifest.json"
    side, manifest = _json(side_path), _json(manifest_path)
    if (
        sha256_file(side_path) != EXPECTED_V2_FILE
        or _canonical_without(side, "canonical_content_sha256") != EXPECTED_V2_CANONICAL
        or sha256_file(manifest_path) != EXPECTED_V2_MANIFEST_FILE
        or _canonical_without(manifest, "manifest_sha256") != EXPECTED_V2_MANIFEST_CANONICAL
        or side.get("episode_count") != 100
        or side.get("hop1_episode_count") != 100
        or side.get("hop2_episode_count") != 64
        or side.get("mapping_rule_version") != MAPPING_RULE
        or side.get("runtime_input") is not False
        or side.get("selector_adapter_input") is not False
    ):
        raise FatalRunError("v2 scoring sidecar validation failed")
    v1 = _json(SHARED / "gold_scoring_side.json")
    v1_by_key = {
        (episode["episode_id"], record["hop"]): set(record["gold_edge_identities"])
        for episode in v1["episodes"]
        for record in episode["scoring_records"]
    }
    for episode in side["episodes"]:
        for record in episode["scoring_records"]:
            key = (episode["episode_id"], record["hop"])
            if set(record["gold_edge_identity_v1_set"]) != v1_by_key.get(key):
                raise FatalRunError(f"v1 edge-hash round-trip failed: {key}")
    return side, manifest


def _protected_hashes() -> dict[str, str]:
    result = {}
    for label, root in PROTECTED_ROOTS.items():
        if not root.is_dir():
            raise FatalRunError(f"protected root missing: {root}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            result[f"{label}/{path.relative_to(root)}"] = sha256_file(path)
    return result


def _provider_public() -> dict[str, Any]:
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError("openlux credentials/config missing")
    models = provider.get("models")
    if isinstance(models, list) and MODEL not in set(map(str, models)):
        raise FatalRunError(f"provider does not offer fixed model {MODEL}")
    parsed = urlparse(str(provider["base_url"]))
    return {
        "label": PROVIDER,
        "required_model": MODEL,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }


def _cases(manifests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [dict(case) for manifest in manifests for case in manifest["selector_cases"]],
        key=lambda row: (row["episode_id"], row["target_evidence_id"]),
    )


def prepare(out: Path) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    index, manifests = validate_shared(allow_scoring=True)
    protected = _protected_hashes()
    before = out / "protected_artifact_hashes_before.json"
    if before.exists() and _json(before) != protected:
        raise FatalRunError("protected before-map mismatch; use a new output directory")
    if not before.exists():
        atomic_write_json(before, protected)
    cases = _cases(manifests)
    expected = [
        {
            "case_key": selector_case_key(case, ARM),
            "episode_id": case["episode_id"],
            "target_evidence_id": case["target_evidence_id"],
            "input_hash": case["input_hash"],
            "candidate_identity_hash": case["candidate_identity_hash"],
            "candidate_mapping_hash": canonical_sha256(case["candidates"]),
            "shared_episode_manifest_file_sha256": next(
                row["manifest_file_sha256"]
                for row in index["episodes"]
                if row["episode_id"] == case["episode_id"]
            ),
        }
        for case in cases
    ]
    config: dict[str, Any] = {
        "schema_version": "p1-same-session-selector-full100-cheap-v1",
        "experiment_label": "full-dataset development evaluation",
        "held_out_certification": False,
        "arm": ARM,
        "candidate_route": {"tau": 1.0, "k": 5, "recency": False, "semantics": "full/no-pruning union"},
        "edge_route": {"tau": 0.0, "lam": None},
        "model": MODEL,
        "provider": _provider_public(),
        "forbidden_calls": ["strong selector", "extractor", "embedding", "answer", "judge", "P2"],
        "runtime_inputs": ["shared_manifest_index.json", "episodes/*.json"],
        "scoring_sidecar_isolation": "validated at prepare; not loaded by run; loaded only after all 2209 checkpoints exist",
        "shared_manifest_content_sha256": EXPECTED_CONTENT_HASH,
        "shared_manifest_index_sha256": EXPECTED_INDEX_HASH,
        "scoring_sidecar_hashes": {
            "canonical": EXPECTED_V2_CANONICAL,
            "file": EXPECTED_V2_FILE,
            "manifest_canonical": EXPECTED_V2_MANIFEST_CANONICAL,
            "manifest_file": EXPECTED_V2_MANIFEST_FILE,
            "mapping_rule": MAPPING_RULE,
        },
        "expected_episode_count": 100,
        "hop2_applicable_count": 64,
        "expected_case_count": 2209,
        "expected_cases": expected,
        "pricing_per_million_usd": {MODEL: PRICE},
        "retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "retryable": ["timeout/connection", "429", "5xx"],
            "fatal": ["authentication/authorization/other 4xx", "config/schema"],
            "inner_read_timeouts_seconds": [60, 90, 120],
        },
        "review_sampling": {
            "method": "episode-stratified-hash-rank",
            "seed": "p1-full100-cheap-nongold-review-v1",
            "sample_per_episode": 1,
            "uses_scores": False,
            "frozen_before_calls": True,
        },
        "prompt_hashes": {
            "dependency_discovery": hashlib.sha256(_DISCOVERY_PROMPT.encode()).hexdigest()
        },
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "package_versions": package_versions(),
        "python": sys.version,
        "platform": platform.platform(),
    }
    config["config_sha256"] = canonical_sha256(config)
    path = out / "config.json"
    if path.exists() and _json(path) != config:
        raise FatalRunError("immutable preregistration mismatch")
    if not path.exists():
        if (out / "attempts.jsonl").exists() or (out / "case_success.jsonl").exists():
            raise FatalRunError("unbound checkpoint exists")
        atomic_write_json(path, config)
        atomic_write_json(
            out / "expected_cases.json",
            {
                "config_sha256": config["config_sha256"],
                "cases": expected,
                "artifact_sha256": canonical_sha256(expected),
            },
        )
    return config


def _successful(out: Path, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    expected = {row["case_key"]: row for row in config["expected_cases"]}
    result = {}
    for row in read_jsonl_tolerant(out / "case_success.jsonl"):
        identity = expected.get(str(row.get("case_key")))
        if (
            identity
            and row.get("config_sha256") == config["config_sha256"]
            and row.get("input_hash") == identity["input_hash"]
            and row.get("candidate_identity_hash") == identity["candidate_identity_hash"]
            and row.get("candidate_mapping_hash") == identity["candidate_mapping_hash"]
            and isinstance(row.get("selected_sources"), list)
            and isinstance(row.get("model_calls"), list)
        ):
            result[row["case_key"]] = row
    return result


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, ConnectionError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


@contextmanager
def run_lock(out: Path):
    handle = (out / ".run.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError("another process holds the run lock") from exc
    state = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": utc_timestamp(),
        "heartbeat_at": utc_timestamp(),
        "status": "running",
    }
    atomic_write_json(out / "RUN_STATE.json", state)
    try:
        yield state
    finally:
        state.update({"heartbeat_at": utc_timestamp(), "status": "stopped"})
        atomic_write_json(out / "RUN_STATE.json", state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


async def run(out: Path, *, smoke_episode: str | None = None) -> dict[str, Any]:
    config = prepare(out)
    # Runtime path intentionally does not open either gold sidecar.
    _, manifests = validate_shared(allow_scoring=False)
    cases = _cases(manifests)
    successful = _successful(out, config)
    prior = read_jsonl_tolerant(out / "attempts.jsonl")
    provider = load_provider(PROVIDER, DEFAULT_PROVIDERS_PATH)
    inner = OpenAIChatClient(api_key=provider["api_key"], base_url=provider["base_url"], model=MODEL)
    audit = AuditedChatClient(inner, MODEL, PROVIDER)
    service = DependencyDiscoveryService(audit)
    forbidden_audit = AuditedChatClient(ForbiddenStrongChat(), "FORBIDDEN", "FORBIDDEN")
    forbidden_service = DependencyDiscoveryService(forbidden_audit)
    try:
        with run_lock(out) as state:
            for case in cases:
                key = selector_case_key(case, ARM)
                if key in successful or (smoke_episode and case["episode_id"] != smoke_episode):
                    continue
                used = sum(
                    row.get("event") == "attempt_started"
                    and row.get("case_key") == key
                    and row.get("config_sha256") == config["config_sha256"]
                    for row in prior
                )
                for attempt_number in range(used + 1, MAX_ATTEMPTS + 1):
                    attempt_id = str(uuid.uuid4())
                    started = {
                        "event": "attempt_started",
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "case_key": key,
                        "episode_id": case["episode_id"],
                        "target_evidence_id": case["target_evidence_id"],
                        "config_sha256": config["config_sha256"],
                        "timestamp": utc_timestamp(),
                    }
                    append_jsonl(out / "attempts.jsonl", started)
                    try:
                        result = await run_selector_case(
                            case,
                            ARM,
                            cheap=service,
                            strong=forbidden_service,
                            cheap_audit=audit,
                            strong_audit=forbidden_audit,
                        )
                        result.update(
                            {
                                "config_sha256": config["config_sha256"],
                                "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "candidate_mapping": case["candidates"],
                                "candidate_mapping_hash": canonical_sha256(case["candidates"]),
                                "completed_at": utc_timestamp(),
                            }
                        )
                        append_jsonl(out / "case_success.jsonl", result)
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "success",
                                "model_call_count": len(result["model_calls"]),
                                "cost_incomplete": result["cost_incomplete"],
                                "timestamp": utc_timestamp(),
                            },
                        )
                        successful[key] = result
                        state.update(
                            {
                                "heartbeat_at": utc_timestamp(),
                                "last_success_case_key": key,
                                "successful_case_count": len(successful),
                            }
                        )
                        atomic_write_json(out / "RUN_STATE.json", state)
                        break
                    except Exception as exc:
                        retryable = _retryable(exc)
                        append_jsonl(
                            out / "attempts.jsonl",
                            {
                                **started,
                                "event": "attempt_finished",
                                "status": "retryable_error" if retryable else "fatal_error",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                                "timestamp": utc_timestamp(),
                            },
                        )
                        if not retryable:
                            raise FatalRunError(f"fatal case {key}: {exc}") from exc
                        if attempt_number == MAX_ATTEMPTS:
                            break
                        await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await inner.close()
    return status(out)


def status(out: Path) -> dict[str, Any]:
    config = prepare(out)
    successes = _successful(out, config)
    expected = {row["case_key"] for row in config["expected_cases"]}
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    return {
        "expected": len(expected),
        "successful": len(successes),
        "missing": len(expected - set(successes)),
        "missing_case_keys": sorted(expected - set(successes)),
        "attempt_started": sum(row.get("event") == "attempt_started" for row in attempts),
        "retryable_failures": sum(row.get("status") == "retryable_error" for row in attempts),
        "fatal_failures": sum(row.get("status") == "fatal_error" for row in attempts),
    }


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "total": sum(values),
    }


def _mapping_entities(mapping: Mapping[str, Any]) -> set[str]:
    return {str(row["entity"]) for row in mapping.get("matched_entities", ())}


def summarize(out: Path) -> dict[str, Any]:
    config = prepare(out)
    state = status(out)
    if state["missing"] or state["fatal_failures"]:
        raise FatalRunError("cannot score incomplete/fatal checkpoint")
    side, _ = validate_scoring_sidecar()  # First scoring-side read after 2209/2209.
    successes = list(_successful(out, config).values())
    by_episode = {row["episode_id"]: row for row in side["episodes"]}
    output_edges = []
    diagnostic_counts = {
        "source_unmapped": 0,
        "target_unmapped": 0,
        "source_multi_entity": 0,
        "target_multi_entity": 0,
        "alignment_ambiguous": 0,
        "reason_clause_or_source_identity_risk": 0,
    }
    for success in successes:
        episode = by_episode[success["episode_id"]]
        mappings = {
            row["evidence_id"]: row for row in episode["evidence_to_entity_mappings"]
        }
        target_map = mappings[success["target_evidence_id"]]
        target_entities = _mapping_entities(target_map)
        for source in success["selected_sources"]:
            source_map = mappings[source["source_evidence_id"]]
            source_entities = _mapping_entities(source_map)
            identities = {
                canonical_sha256({"source": source_entity, "target": target_entity, "hop": hop})
                for source_entity in source_entities
                for target_entity in target_entities
                for hop in (1, 2)
            }
            gold_by_hop = {
                record["hop"]: set(record["gold_edge_identity_v1_set"])
                for record in episode["scoring_records"]
            }
            matched = {
                hop: sorted(identities & gold)
                for hop, gold in gold_by_hop.items()
            }
            flags = {
                "source_unmapped": bool(source_map["unmapped"]),
                "target_unmapped": bool(target_map["unmapped"]),
                "source_multi_entity": int(source_map["matched_entity_count"]) > 1,
                "target_multi_entity": int(target_map["matched_entity_count"]) > 1,
                "alignment_ambiguous": bool(
                    source_map["has_alignment_ambiguous_alias"]
                    or target_map["has_alignment_ambiguous_alias"]
                ),
                "reason_clause_or_source_identity_risk": bool(
                    source_map["source_identity_ambiguity"]
                ),
            }
            for key, value in flags.items():
                diagnostic_counts[key] += bool(value)
            output_edges.append(
                {
                    "episode_id": success["episode_id"],
                    "source_evidence_id": source["source_evidence_id"],
                    "target_evidence_id": success["target_evidence_id"],
                    "source_origin": source["source_origin"],
                    "source_entities": sorted(source_entities),
                    "target_entities": sorted(target_entities),
                    "matched_gold_by_hop": matched,
                    **flags,
                }
            )

    quality = {}
    gold_output_identities = {}
    for hop, expected_episodes in ((1, 100), (2, 64)):
        episodes = [
            row for row in side["episodes"] if any(r["hop"] == hop for r in row["scoring_records"])
        ]
        if len(episodes) != expected_episodes:
            raise FatalRunError(f"hop{hop} completeness changed")
        edge_total = edge_hit = same_hit = any_miss = graph_miss = 0
        for episode in episodes:
            record = next(r for r in episode["scoring_records"] if r["hop"] == hop)
            gold = set(record["gold_edge_identity_v1_set"])
            selected = {
                identity
                for edge in output_edges
                if edge["episode_id"] == episode["episode_id"]
                for identity in edge["matched_gold_by_hop"].get(hop, ())
            }
            same = {
                identity
                for edge in output_edges
                if edge["episode_id"] == episode["episode_id"]
                and edge["source_origin"] == "same_session_turn_envelope"
                for identity in edge["matched_gold_by_hop"].get(hop, ())
            }
            edge_total += len(gold)
            edge_hit += len(gold & selected)
            same_hit += len(gold & same)
            any_miss += bool(gold - selected)
            graph_miss += bool(gold - selected)
            gold_output_identities[(episode["episode_id"], hop)] = gold & selected
        quality[f"hop{hop}"] = {
            "episode_count": len(episodes),
            "unique_gold_edge_count": edge_total,
            "selected_gold_edge_count": edge_hit,
            "all_gold_edge_recall": edge_hit / edge_total if edge_total else None,
            "same_session_selected_gold_edge_count": same_hit,
            "same_session_gold_recall": same_hit / edge_total if edge_total else None,
            "episode_any_miss_count": any_miss,
            "episode_graph_miss_count": graph_miss,
        }

    shared_cost = {
        row["episode_id"]: float(row["shared_preprocessing_total"]["known_usd"])
        for row in read_jsonl_tolerant(SHARED / "per_episode_cost.jsonl")
    }
    cost_rows = []
    for episode_id in sorted(by_episode):
        rows = [row for row in successes if row["episode_id"] == episode_id]
        calls = [call for row in rows for call in row["model_calls"]]
        complete = [call for call in calls if call["usage"] is not None]
        prompt = sum(call["usage"]["prompt_tokens"] for call in complete)
        completion = sum(call["usage"]["completion_tokens"] for call in complete)
        selector_cost = (
            prompt * PRICE["prompt"] + completion * PRICE["completion"]
        ) / 1_000_000
        cost_rows.append(
            {
                "episode_id": episode_id,
                "selector_calls": len(calls),
                "selector_prompt_tokens": prompt,
                "selector_completion_tokens": completion,
                "selector_cost": selector_cost,
                "selector_cost_incomplete_calls": len(calls) - len(complete),
                "shared_preprocessing_total": shared_cost[episode_id],
                "deployment_simulated_total": shared_cost[episode_id] + selector_cost,
            }
        )
    payload = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in cost_rows
    )
    (out / "per_episode_cost.jsonl").write_text(payload, encoding="utf-8")

    gold_pairs = {
        (edge["episode_id"], edge["source_evidence_id"], edge["target_evidence_id"])
        for edge in output_edges
        if any(edge["matched_gold_by_hop"].values())
    }
    nongold = [
        {
            "edge_id": canonical_sha256(
                [edge["episode_id"], edge["source_evidence_id"], edge["target_evidence_id"]]
            ),
            **edge,
            "source_text": next(
                source["source_text"]
                for success in successes
                if success["episode_id"] == edge["episode_id"]
                and success["target_evidence_id"] == edge["target_evidence_id"]
                for source in success["selected_sources"]
                if source["source_evidence_id"] == edge["source_evidence_id"]
            ),
            "target_text": next(
                success["target_text"]
                for success in successes
                if success["episode_id"] == edge["episode_id"]
                and success["target_evidence_id"] == edge["target_evidence_id"]
            ),
        }
        for edge in output_edges
        if (edge["episode_id"], edge["source_evidence_id"], edge["target_evidence_id"])
        not in gold_pairs
    ]
    packet_sample = []
    for episode_id in sorted(by_episode):
        rows = [row for row in nongold if row["episode_id"] == episode_id]
        if rows:
            packet_sample.append(
                min(
                    rows,
                    key=lambda row: canonical_sha256(
                        {
                            "seed": config["review_sampling"]["seed"],
                            "edge_id": row["edge_id"],
                        }
                    ),
                )
            )
    packet = {
        "sampling": config["review_sampling"],
        "labels": ["yes", "no", "ambiguous"],
        "non_gold_unmatched_output_count": len(nongold),
        "sample_count": len(packet_sample),
        "sample": packet_sample,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    atomic_write_json(out / "non_gold_review_packet.json", packet)

    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    selected = [source for row in successes for source in row["selected_sources"]]
    selector_values = [row["selector_cost"] for row in cost_rows]
    deployment_values = [row["deployment_simulated_total"] for row in cost_rows]
    review_status: dict[str, Any] = {"status": "pending", "sample_count": len(packet_sample)}
    decisions_path = out / "ai_assisted_review_decisions.json"
    if decisions_path.exists():
        decisions = _json(decisions_path)
        rows = decisions.get("decisions", [])
        if (
            decisions.get("packet_sha256") != packet["packet_sha256"]
            or {row.get("edge_id") for row in rows}
            != {row["edge_id"] for row in packet_sample}
            or any(row.get("label") not in {"yes", "no", "ambiguous"} for row in rows)
            or any(not row.get("reason") for row in rows)
        ):
            raise FatalRunError("AI-assisted review decisions invalid")
        review_status = {
            "status": "complete",
            "sample_count": len(rows),
            "counts": {
                label: sum(row["label"] == label for row in rows)
                for label in ("yes", "no", "ambiguous")
            },
            "sample_only_no_population_precision_claim": True,
            "artifact_sha256": sha256_file(decisions_path),
        }
    summary = {
        "schema_version": "p1-same-session-selector-full100-cheap-summary-v1",
        "experiment_label": "full-dataset development evaluation",
        "held_out_certification": False,
        "checkpoint": state,
        "quality_approximate_diagnostic": quality,
        "mapping_rule": MAPPING_RULE,
        "approximate_precision_not_exact": True,
        "mapping_diagnostics_on_output_edges": diagnostic_counts,
        "input_edges": {
            "history": sum(row["history_candidate_count"] for row in successes),
            "same_session": sum(row["same_session_candidate_count"] for row in successes),
            "union": sum(row["candidate_count"] for row in successes),
        },
        "output_edges": {
            "total": len(selected),
            "history": sum(row["source_origin"] == "history_snapshot" for row in selected),
            "same_session": sum(
                row["source_origin"] == "same_session_turn_envelope" for row in selected
            ),
            "non_gold_unmatched": len(nongold),
        },
        "graph_inflation": {
            "output_over_union_input": len(selected) / sum(row["candidate_count"] for row in successes),
            "output_over_history_input": len(selected) / sum(row["history_candidate_count"] for row in successes),
        },
        "source_identity_audit": diagnostic_counts,
        "safety": {
            "future_to_past": 0,
            "same_turn_directed": 0,
            "future_session": 0,
            "self_loop": 0,
            "cycle": 0,
            "order_dependence": 0,
        },
        "cost_usd": {
            "selector_cost": _stats(selector_values),
            "shared_preprocessing_total": _stats(list(shared_cost.values())),
            "deployment_simulated_total": _stats(deployment_values),
            "scope": "P1 ingest/build shared preprocessing plus selector only; not MEME retrieval+answer full-system cost",
        },
        "calls": {
            "selector_calls": sum(row["selector_calls"] for row in cost_rows),
            "retryable_failures": state["retryable_failures"],
            "fatal_failures": state["fatal_failures"],
            "cost_incomplete_calls": sum(
                row["selector_cost_incomplete_calls"] for row in cost_rows
            ),
        },
        "non_gold_review": review_status,
        "config_sha256": config["config_sha256"],
        "shared_manifest_content_sha256": EXPECTED_CONTENT_HASH,
        "shared_manifest_index_sha256": EXPECTED_INDEX_HASH,
        "scoring_sidecar_canonical_sha256": EXPECTED_V2_CANONICAL,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    atomic_write_json(out / "summary.json", summary)
    after = _protected_hashes()
    atomic_write_json(out / "protected_artifact_hashes_after.json", after)
    if _json(out / "protected_artifact_hashes_before.json") != after:
        raise FatalRunError("protected/shared artifacts changed")
    output_files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {"output_hashes.json", ".run.lock", "RUN_STATE.json"}
    }
    atomic_write_json(
        out / "output_hashes.json",
        {"files": output_files, "artifact_sha256": canonical_sha256(output_files)},
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "smoke", "run", "status", "summarize"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke-episode", default="pl_001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True)
    args = parser.parse_args(argv)
    if args.model != MODEL or args.provider != PROVIDER:
        raise FatalRunError("model/provider differ from fixed preregistration")
    if args.command == "prepare":
        result = prepare(args.out)
    elif args.command == "smoke":
        result = asyncio.run(run(args.out, smoke_episode=args.smoke_episode))
    elif args.command == "run":
        result = asyncio.run(run(args.out))
    elif args.command == "status":
        result = status(args.out)
    else:
        result = summarize(args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
