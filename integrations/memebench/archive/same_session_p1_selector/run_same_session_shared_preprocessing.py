"""Freeze shared MEME extraction and selector-candidate inputs for two P1 arms.

This runner is deliberately selector-free.  It calls only the chronological
raw-dialogue extractor, then performs deterministic turn alignment and builds
arrival-time history plus strict-earlier-turn same-session candidate unions.
"""

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
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
    _EXTRACT_PROMPT,
)
from integrations.memebench.gold_edge_audit import (
    append_jsonl,
    atomic_write_json,
    canonical_sha256,
    read_jsonl_tolerant,
    sha256_file,
    utc_timestamp,
)
from integrations.memebench.ingest import _session_text, _split_evidence
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.run_chronological_p1p2 import package_versions
from integrations.memebench.same_session_selector_intervention import (
    build_selector_cases,
)
from integrations.memebench.systems import DEFAULT_PROVIDERS_PATH, load_provider
from integrations.memebench.turn_candidate_mechanism import (
    DEFAULT_MIN_TOKEN_OVERLAP,
    PERMUTATION_SEEDS,
    audit_candidate_invariants,
    generate_turn_candidates,
    verify_permutation_invariance,
)


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "integrations" / "memebench" / "runs"
DEFAULT_OUT = RUNS / "p1_same_session_selector_full100_shared_20260824"
DEFAULT_DATA = Path("/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json")
FORMAL = RUNS / "chronological_20260820" / "formal"
OLD_AUDIT = RUNS / "p1_gold_edge_audit_20260822"
MANUAL = RUNS / "p1_same_session_manual_validation_20260823"
TURN = RUNS / "p1_same_session_turn_candidate_mechanism_20260824"
SELECTOR = RUNS / "p1_same_session_selector_intervention_20260824_v2"
EXTRACT_MODEL = "gpt-4.1-mini"
EXTRACT_PROVIDER = "openlux"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_PROVIDER = "aliyun"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (5.0, 15.0, 45.0)
EXTRACT_PRICE = {"prompt": 0.40, "completion": 1.60}
SOURCE_FILES = (
    "integrations/memebench/run_same_session_shared_preprocessing.py",
    "integrations/memebench/turn_candidate_mechanism.py",
    "integrations/memebench/same_session_selector_intervention.py",
    "integrations/memebench/ingest.py",
    "integrations/memebench/loader.py",
    "src/contexthub/llm/chat_client.py",
    "src/contexthub/services/conversation_extraction_service.py",
)
PROTECTED_ROOTS = {
    "formal": FORMAL,
    "gold-edge-audit-20260822": OLD_AUDIT,
    "manual-validation-20260823": MANUAL,
    "turn-mechanism-20260824": TURN,
    "selector-intervention-v2": SELECTOR,
}


class FatalRunError(RuntimeError):
    pass


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FatalRunError(f"{path} must contain a JSON object")
    return value


def _protected_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for label, root in PROTECTED_ROOTS.items():
        if not root.is_dir():
            raise FatalRunError(f"protected directory missing: {root}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            hashes[f"{label}/{path.relative_to(root)}"] = sha256_file(path)
    return hashes


def _provider_public(label: str, required_model: str) -> tuple[dict[str, Any], dict[str, Any]]:
    provider = load_provider(label, DEFAULT_PROVIDERS_PATH)
    if not provider.get("api_key") or not provider.get("base_url"):
        raise FatalRunError(f"{label} credentials/config missing")
    models = provider.get("models")
    if isinstance(models, list) and required_model not in set(map(str, models)):
        raise FatalRunError(f"{label} config lacks required model {required_model}")
    parsed = urlparse(str(provider["base_url"]))
    public = {
        "label": label,
        "base_url_origin": f"{parsed.scheme}://{parsed.netloc}",
        "required_model": required_model,
        "provider_file_sha256": sha256_file(DEFAULT_PROVIDERS_PATH),
        "secrets_redacted": True,
    }
    return provider, public


def _episode_id(raw: Mapping[str, Any]) -> str:
    return str(raw.get("episode_id") or "")


def _raw_episode_map(data: Path) -> dict[str, dict[str, Any]]:
    rows = load_episodes(data)
    result = {_episode_id(row): row for row in rows}
    if len(result) != len(rows):
        raise FatalRunError("dataset has duplicate episode IDs")
    return result


def _case_maps(data: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    episodes = load_episodes(data)
    hop1 = {case.episode_id: case for case in extract_cascade_cases(episodes, hop=1)}
    hop2 = {case.episode_id: case for case in extract_cascade_cases(episodes, hop=2)}
    if len(hop1) != 100 or len(hop2) != 64:
        raise FatalRunError(f"expected 100 hop1 and 64 hop2 episodes, got {len(hop1)}/{len(hop2)}")
    return hop1, hop2


def _stable_node_id(
    episode_id: str, session_index: int, original_session_id: str, text: str
) -> str:
    return "node-" + canonical_sha256(
        {
            "episode_id": episode_id,
            "session_index": session_index,
            "original_session_id": original_session_id,
            "text": text,
        }
    )


def _raw_session_record(session_index: int, session: Mapping[str, Any]) -> dict[str, Any]:
    turns = [
        {
            "turn_index": turn_index,
            "role": str(turn.get("role") or ""),
            "content": str(turn.get("content") or ""),
        }
        for turn_index, turn in enumerate(session.get("conversation", ()))
    ]
    record = {
        "session_index": session_index,
        "original_session_id": str(session.get("session_id") or ""),
        "session_type": str(session.get("type") or ""),
        "turns": turns,
    }
    record["session_sha256"] = canonical_sha256(record)
    for turn in turns:
        turn["turn_sha256"] = canonical_sha256(
            {
                "session_index": session_index,
                "original_session_id": record["original_session_id"],
                **turn,
            }
        )
    return record


class ExtractionAuditClient:
    """Label extractor calls and preserve exact successful provider usage."""

    def __init__(self, inner: OpenAIChatClient):
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        started = time.perf_counter()
        answer = await self.inner.complete(prompt, max_tokens=max_tokens)
        latency = time.perf_counter() - started
        usage = self.inner.last_usage
        clean = None
        if isinstance(usage, dict) and usage.get("total_tokens"):
            clean = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        self.calls.append(
            {
                "role": "extractor",
                "provider": EXTRACT_PROVIDER,
                "model": EXTRACT_MODEL,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                "prompt": prompt,
                "answer": answer,
                "max_tokens": max_tokens,
                "usage": clean,
                "cost_incomplete": clean is None,
                "latency_seconds": latency,
            }
        )
        return answer


def _cost_of_calls(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    complete = [row for row in calls if isinstance(row.get("usage"), Mapping)]
    prompt = sum(int(row["usage"]["prompt_tokens"]) for row in complete)
    completion = sum(int(row["usage"]["completion_tokens"]) for row in complete)
    known = prompt * EXTRACT_PRICE["prompt"] / 1_000_000 + completion * EXTRACT_PRICE["completion"] / 1_000_000
    incomplete = sum(bool(row.get("cost_incomplete")) for row in calls)
    return {
        "extraction": {
            "calls": len(calls),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "known_usd": known,
            "cost_incomplete_calls": incomplete,
        },
        "embedding": {
            "applicable": False,
            "calls": 0,
            "known_usd": 0.0,
            "reason": "full/no-pruning candidate routing uses no vector similarity; arrival snapshots are identity sets",
        },
        "shared_preprocessing_total": {
            "known_usd": known,
            "cost_complete": incomplete == 0,
        },
    }


async def build_episode(
    episode_id: str,
    case: Any,
    raw_episode: Mapping[str, Any],
    extractor: ConversationExtractionService,
    audit: ExtractionAuditClient,
) -> dict[str, Any]:
    pre_sessions, _ = _split_evidence(case)
    nodes: list[dict[str, Any]] = []
    raw_sessions: list[dict[str, Any]] = []
    published: list[str] = []
    call_start = len(audit.calls)
    raw_extracted_count = 0
    for session_index, session in enumerate(pre_sessions):
        session_record = _raw_session_record(session_index, session)
        raw_sessions.append(session_record)
        facts = await extractor.extract(_session_text(session))
        raw_extracted_count += len(facts)
        session_nodes: dict[str, dict[str, Any]] = {}
        for fact in facts:
            text = str(fact.text or "").strip()
            if not text:
                continue
            node_id = _stable_node_id(
                episode_id, session_index, session_record["original_session_id"], text
            )
            session_nodes[node_id] = {
                "node_id": node_id,
                "text": text,
                "session_index": session_index,
                "original_session_id": session_record["original_session_id"],
                "original_turns": session_record["turns"],
                "embedding_present": False,
            }
        for node in sorted(session_nodes.values(), key=lambda row: row["node_id"]):
            node["candidate_snapshot_ids"] = sorted(set(published))
            nodes.append(node)
        published.extend(sorted(session_nodes))

    traces = [
        {
            "node_id": node["node_id"],
            "candidate_snapshot_ids": node["candidate_snapshot_ids"],
            "candidate_snapshot_size": len(node["candidate_snapshot_ids"]),
            "candidate_snapshot_hash": canonical_sha256(node["candidate_snapshot_ids"]),
        }
        for node in nodes
    ]
    evidence = {"episode_id": episode_id, "nodes": nodes, "candidate_traces": traces}
    mechanism = generate_turn_candidates(episode_id, nodes)
    permutation = verify_permutation_invariance(episode_id, nodes)
    if not permutation["all_identical"]:
        raise FatalRunError(f"permutation invariant failed for {episode_id}")
    selector_cases = build_selector_cases([evidence])
    calls = audit.calls[call_start:]
    result = {
        "schema_version": "p1-same-session-shared-preprocessing-v1",
        "episode_id": episode_id,
        "raw_episode_sha256": canonical_sha256(raw_episode),
        "raw_sessions": raw_sessions,
        "raw_session_count": len(raw_sessions),
        "raw_extracted_fact_count": raw_extracted_count,
        "deduplicated_node_count": len(nodes),
        "nodes": nodes,
        "candidate_traces": traces,
        "alignments": mechanism["alignments"],
        "same_session_candidates": mechanism["candidates"],
        "same_turn_unresolved": mechanism["same_turn_unresolved"],
        "selector_cases": selector_cases,
        "permutation_check": permutation,
        "extractor_calls": calls,
        "cost": _cost_of_calls(calls),
    }
    result["episode_manifest_sha256"] = canonical_sha256(result)
    return result


def _scoring_side(hop1: Mapping[str, Any], hop2: Mapping[str, Any]) -> dict[str, Any]:
    episodes = []
    for episode_id in sorted(hop1):
        rows = []
        for hop, cases in ((1, hop1), (2, hop2)):
            case = cases.get(episode_id)
            if case is None:
                continue
            scoring_id = "score-" + canonical_sha256(
                {"episode_id": episode_id, "hop": hop, "target_entity": case.target_entity}
            )
            rows.append(
                {
                    "gold_scoring_id": scoring_id,
                    "hop": hop,
                    "target_entity": case.target_entity,
                    "gold_answer": case.gold_answer,
                    "gold_edge_identities": sorted(
                        {
                            canonical_sha256(
                                {"source": edge.source, "target": edge.target, "hop": edge.hop}
                            )
                            for edge in case.edges
                        }
                    ),
                }
            )
        episodes.append({"episode_id": episode_id, "scoring_records": rows})
    artifact = {
        "schema_version": "p1-shared-gold-scoring-side-v1",
        "generator_isolation": "never passed to extraction, alignment, snapshots, or candidate generation",
        "episodes": episodes,
    }
    artifact["artifact_sha256"] = canonical_sha256(artifact)
    return artifact


def _source_hashes() -> dict[str, str]:
    return {name: sha256_file(ROOT / name) for name in SOURCE_FILES}


def prepare(out: Path, data: Path = DEFAULT_DATA) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    protected = _protected_hashes()
    before = out / "protected_artifact_hashes_before.json"
    if before.exists() and _json(before) != protected:
        raise FatalRunError("protected before hash map mismatch")
    if not before.exists():
        atomic_write_json(before, protected)
    raw = _raw_episode_map(data)
    hop1, hop2 = _case_maps(data)
    expected = sorted(hop1)
    if set(expected) != set(raw):
        raise FatalRunError("formal hop1 IDs differ from raw dataset IDs")
    _, extract_public = _provider_public(EXTRACT_PROVIDER, EXTRACT_MODEL)
    _, embedding_public = _provider_public(EMBEDDING_PROVIDER, EMBEDDING_MODEL)
    config: dict[str, Any] = {
        "schema_version": "p1-same-session-shared-preprocessing-v1",
        "experiment_label": "full-dataset development intervention evaluation",
        "held_out_certification": False,
        "benchmark_only": True,
        "data_path": str(data),
        "data_sha256": sha256_file(data),
        "expected_episode_ids": expected,
        "expected_episode_count": 100,
        "hop2_applicable_episode_ids": sorted(hop2),
        "hop2_applicable_count": 64,
        "extractor": {
            "role": "extractor",
            "model": EXTRACT_MODEL,
            "provider": extract_public,
            "prompt_sha256": hashlib.sha256(_EXTRACT_PROMPT.encode()).hexdigest(),
        },
        "embedding": {
            "model": EMBEDDING_MODEL,
            "provider": embedding_public,
            "calls_planned": 0,
            "omitted": True,
            "first_principles_reason": (
                "Both downstream arms use identical full/no-pruning candidate sets. "
                "Arrival history is the complete earlier-session identity snapshot, "
                "and same-session candidates use raw turn order; neither operation "
                "uses vector similarity. Omitting vectors cannot change candidate IDs, "
                "text, ordering, hashes, or either arm's input."
            ),
        },
        "forbidden_model_roles": [
            "dependency selector cheap gpt-4o-mini",
            "dependency selector strong gpt-4.1-mini",
            "answer",
            "judge",
            "P2",
        ],
        "candidate_generator_forbidden_inputs": [
            "gold/manual IDs",
            "gold entity/value/answer",
            "extractor array position",
            "node UUID ordering",
            "future turns",
            "future sessions",
        ],
        "alignment": {
            "min_token_overlap": DEFAULT_MIN_TOKEN_OVERLAP,
            "ambiguous_policy": "quarantine",
            "same_turn_policy": "unresolved",
            "permutation_seeds": list(PERMUTATION_SEEDS),
        },
        "retry": {
            "max_attempts": MAX_ATTEMPTS,
            "backoff_seconds": list(BACKOFF_SECONDS),
            "retryable": ["timeout/connection", "429", "5xx"],
            "fatal": ["authentication/authorization/other 4xx", "schema/config"],
            "inner_read_timeouts_seconds": [60, 90, 120],
        },
        "pricing_per_million_usd": {EXTRACT_MODEL: EXTRACT_PRICE},
        "source_hashes": _source_hashes(),
        "package_versions": {
            key: value
            for key, value in package_versions().items()
            if key in {"python", "asyncpg", "httpx"}
        },
        "python": sys.version,
        "platform": platform.platform(),
    }
    config["config_sha256"] = canonical_sha256(config)
    path = out / "config.json"
    if path.exists() and _json(path) != config:
        raise FatalRunError("immutable config/input/source mismatch; use a new directory")
    if not path.exists():
        if (out / "attempts.jsonl").exists() or (out / "episode_success.jsonl").exists():
            raise FatalRunError("unbound checkpoints exist")
        atomic_write_json(path, config)
        scoring = _scoring_side(hop1, hop2)
        atomic_write_json(out / "gold_scoring_side.json", scoring)
        scoring_ids = {
            row["episode_id"]: [
                item["gold_scoring_id"] for item in row["scoring_records"]
            ]
            for row in scoring["episodes"]
        }
        expected_rows = [
            {
                "episode_id": episode_id,
                "hop1_applicable": True,
                "hop2_applicable": episode_id in hop2,
                "gold_scoring_ids": scoring_ids[episode_id],
                "raw_episode_sha256": canonical_sha256(raw[episode_id]),
            }
            for episode_id in expected
        ]
        atomic_write_json(
            out / "expected_episodes.json",
            {
                "config_sha256": config["config_sha256"],
                "episodes": expected_rows,
                "artifact_sha256": canonical_sha256(expected_rows),
            },
        )
    return config


def _successes(out: Path, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    expected = set(map(str, config["expected_episode_ids"]))
    successes: dict[str, dict[str, Any]] = {}
    for row in read_jsonl_tolerant(out / "episode_success.jsonl"):
        episode_id = str(row.get("episode_id"))
        shard = out / str(row.get("manifest_path") or "")
        if (
            episode_id in expected
            and row.get("config_sha256") == config["config_sha256"]
            and shard.is_file()
            and sha256_file(shard) == row.get("manifest_file_sha256")
        ):
            successes[episode_id] = row
    return successes


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


@contextmanager
def run_lock(out: Path):
    lock_path = out / ".run.lock"
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise FatalRunError(f"another writer holds {lock_path}") from exc
    atomic_write_json(
        out / "RUN_STATE.json",
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": utc_timestamp(),
            "heartbeat_at": utc_timestamp(),
            "status": "running",
        },
    )
    try:
        yield
    finally:
        state = _json(out / "RUN_STATE.json")
        state.update({"heartbeat_at": utc_timestamp(), "status": "stopped"})
        atomic_write_json(out / "RUN_STATE.json", state)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


async def run(out: Path, data: Path, limit: int | None = None) -> None:
    config = prepare(out, data)
    raw = _raw_episode_map(data)
    hop1, _ = _case_maps(data)
    successes = _successes(out, config)
    selected = list(config["expected_episode_ids"])
    if limit is not None:
        selected = selected[:limit]
    pending = [eid for eid in selected if eid not in successes]
    provider = load_provider(EXTRACT_PROVIDER, DEFAULT_PROVIDERS_PATH)
    client = OpenAIChatClient(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        model=EXTRACT_MODEL,
    )
    audit = ExtractionAuditClient(client)
    extractor = ConversationExtractionService(audit)
    try:
        for episode_id in pending:
            for attempt_number in range(1, MAX_ATTEMPTS + 1):
                attempt_id = str(uuid.uuid4())
                call_start = len(audit.calls)
                append_jsonl(
                    out / "attempts.jsonl",
                    {
                        "event": "attempt_started",
                        "episode_id": episode_id,
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "config_sha256": config["config_sha256"],
                        "timestamp": utc_timestamp(),
                    },
                )
                try:
                    result = await build_episode(
                        episode_id, hop1[episode_id], raw[episode_id], extractor, audit
                    )
                    shard = out / "episodes" / f"{episode_id}.json"
                    if shard.exists():
                        raise FatalRunError(f"immutable shard already exists: {shard}")
                    atomic_write_json(shard, result)
                    success = {
                        "episode_id": episode_id,
                        "config_sha256": config["config_sha256"],
                        "manifest_path": str(shard.relative_to(out)),
                        "manifest_file_sha256": sha256_file(shard),
                        "episode_manifest_sha256": result["episode_manifest_sha256"],
                        "cost": result["cost"],
                        "completed_at": utc_timestamp(),
                    }
                    append_jsonl(out / "episode_success.jsonl", success)
                    append_jsonl(
                        out / "attempts.jsonl",
                        {
                            "event": "attempt_finished",
                            "status": "success",
                            "episode_id": episode_id,
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "extractor_calls": audit.calls[call_start:],
                            "timestamp": utc_timestamp(),
                        },
                    )
                    state = _json(out / "RUN_STATE.json")
                    state.update(
                        {
                            "heartbeat_at": utc_timestamp(),
                            "last_success_episode_id": episode_id,
                            "successful_episode_count": len(_successes(out, config)),
                        }
                    )
                    atomic_write_json(out / "RUN_STATE.json", state)
                    break
                except Exception as exc:
                    retryable = _retryable(exc)
                    append_jsonl(
                        out / "attempts.jsonl",
                        {
                            "event": "attempt_finished",
                            "status": "retryable_error" if retryable else "fatal_error",
                            "episode_id": episode_id,
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "extractor_calls": audit.calls[call_start:],
                            "cost_incomplete": True,
                            "timestamp": utc_timestamp(),
                        },
                    )
                    if not retryable or attempt_number >= MAX_ATTEMPTS:
                        raise
                    await asyncio.sleep(BACKOFF_SECONDS[attempt_number - 1])
    finally:
        await client.close()


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * q))]


def finalize(out: Path, data: Path = DEFAULT_DATA, *, require_complete: bool = True) -> dict[str, Any]:
    config = prepare(out, data)
    successes = _successes(out, config)
    if require_complete and len(successes) != 100:
        raise FatalRunError(f"cannot freeze incomplete run: {len(successes)}/100")
    manifests = [_json(out / successes[eid]["manifest_path"]) for eid in sorted(successes)]
    all_mechanisms = [
        {
            "episode_id": row["episode_id"],
            "alignments": row["alignments"],
            "candidates": row["same_session_candidates"],
            "same_turn_unresolved": row["same_turn_unresolved"],
        }
        for row in manifests
    ]
    invariants = audit_candidate_invariants(all_mechanisms)
    future_session = sum(
        source["session_index"] >= case["target_session_index"]
        for row in manifests
        for case in row["selector_cases"]
        for source in case["candidates"]
        if source["source_origin"] == "history_snapshot"
    )
    permutation_failures = sum(
        not row["permutation_check"]["all_identical"] for row in manifests
    )
    target_count = sum(len(row["selector_cases"]) for row in manifests)
    history_count = sum(
        case["history_candidate_count"]
        for row in manifests
        for case in row["selector_cases"]
    )
    same_count = sum(
        case["same_session_candidate_count"]
        for row in manifests
        for case in row["selector_cases"]
    )
    union_count = sum(
        len(case["candidates"]) for row in manifests for case in row["selector_cases"]
    )
    costs = [float(row["cost"]["shared_preprocessing_total"]["known_usd"]) for row in manifests]
    cost_rows = [
        {
            "episode_id": row["episode_id"],
            **row["cost"],
            "deployment_simulation_rule": (
                "arm episode cost = this shared_preprocessing_total + that arm's selector episode cost"
            ),
        }
        for row in manifests
    ]
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in cost_rows
    )
    (out / "per_episode_cost.jsonl").write_text(payload, encoding="utf-8")
    index_rows = [
        {
            "episode_id": eid,
            "manifest_path": successes[eid]["manifest_path"],
            "manifest_file_sha256": successes[eid]["manifest_file_sha256"],
            "episode_manifest_sha256": successes[eid]["episode_manifest_sha256"],
            "hop1_applicable": True,
            "hop2_applicable": eid in set(config["hop2_applicable_episode_ids"]),
        }
        for eid in sorted(successes)
    ]
    index = {
        "schema_version": "p1-same-session-shared-manifest-index-v1",
        "immutable": True,
        "read_only_input_for_arms": True,
        "config_sha256": config["config_sha256"],
        "expected_episodes_artifact_sha256": _json(out / "expected_episodes.json")[
            "artifact_sha256"
        ],
        "episode_count": len(index_rows),
        "episodes": index_rows,
    }
    index["manifest_content_sha256"] = canonical_sha256(
        [
            {
                "episode_id": row["episode_id"],
                "manifest_file_sha256": row["manifest_file_sha256"],
            }
            for row in index_rows
        ]
    )
    index["index_sha256"] = canonical_sha256(index)
    atomic_write_json(out / "shared_manifest_index.json", index)
    attempts = read_jsonl_tolerant(out / "attempts.jsonl")
    retry_failures = sum(row.get("status") == "retryable_error" for row in attempts)
    fatal_failures = sum(row.get("status") == "fatal_error" for row in attempts)
    incomplete = [
        row["episode_id"]
        for row in manifests
        if not row["cost"]["shared_preprocessing_total"]["cost_complete"]
    ]
    summary = {
        "schema_version": "p1-same-session-shared-preprocessing-summary-v1",
        "experiment_label": config["experiment_label"],
        "completed_episode_count": len(manifests),
        "expected_episode_count": 100,
        "hop2_applicable_count": len(config["hop2_applicable_episode_ids"]),
        "raw_extracted_fact_count": sum(row["raw_extracted_fact_count"] for row in manifests),
        "deduplicated_node_count": sum(row["deduplicated_node_count"] for row in manifests),
        "aligned_node_count": sum(
            alignment["status"] == "aligned"
            for row in manifests
            for alignment in row["alignments"]
        ),
        "ambiguous_alignment_count": invariants["ambiguous_alignment_count"],
        "target_count": target_count,
        "history_candidate_incidence_count": history_count,
        "same_session_candidate_incidence_count": same_count,
        "union_candidate_incidence_count": union_count,
        "same_turn_unresolved_count": invariants["same_turn_unresolved_count"],
        "safety_invariants": {
            **invariants,
            "future_session_source_count": future_session,
            "look_ahead_count": future_session + invariants["future_to_past_count"],
            "extractor_order_dependence_count": permutation_failures,
        },
        "shared_manifest_content_sha256": index["manifest_content_sha256"],
        "shared_manifest_index_sha256": index["index_sha256"],
        "cost_usd": {
            "mean": sum(costs) / len(costs) if costs else None,
            "median": _percentile(costs, 0.50),
            "p50": _percentile(costs, 0.50),
            "p95": _percentile(costs, 0.95),
            "min": min(costs) if costs else None,
            "max": max(costs) if costs else None,
            "total": sum(costs),
            "cost_incomplete_episode_ids": incomplete,
            "scope": "P1 ingest/build shared preprocessing only; excludes selector/retrieval/answer/judge/P2",
        },
        "retryable_failure_count": retry_failures,
        "fatal_failure_count": fatal_failures,
        "missing_episode_ids": sorted(set(config["expected_episode_ids"]) - set(successes)),
    }
    atomic_write_json(out / "summary.json", summary)
    protected_after = _protected_hashes()
    atomic_write_json(out / "protected_artifact_hashes_after.json", protected_after)
    if _json(out / "protected_artifact_hashes_before.json") != protected_after:
        raise FatalRunError("protected artifacts changed")
    output_files = {
        str(path.relative_to(out)): sha256_file(path)
        for path in sorted(item for item in out.rglob("*") if item.is_file())
        if path.name not in {"output_hashes.json", ".run.lock", "RUN_STATE.json"}
    }
    output_hashes = {"files": output_files}
    output_hashes["artifact_sha256"] = canonical_sha256(output_hashes)
    atomic_write_json(out / "output_hashes.json", output_hashes)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--allow-incomplete-finalize", action="store_true")
    args = parser.parse_args()
    with run_lock(args.out):
        if not args.finalize_only:
            asyncio.run(run(args.out, args.data, args.limit))
        summary = finalize(
            args.out, args.data, require_complete=not args.allow_incomplete_finalize
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
