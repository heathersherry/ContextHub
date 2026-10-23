"""Executable, opt-in runner. Configuration preparation never calls models."""

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.jev_client import JevClient, JEV_MODEL, JEV_ENDPOINT
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.llm.request_parameters import RequestParameters, SeedPolicy
from contexthub.llm.retry_policy import RetryPolicy
from contexthub.models.execution import PriceBasis, Usage, calculate_cost
from contexthub.memory.storage import MemoryStore
from contexthub.memory.model_io import OUTPUT_ATTEMPTS
from contexthub.memory.schemas import RetrievalConfig
from contexthub.memory.index_cli import add_embedding_index_arguments, resolve_index_config
from contexthub.memory.extractor import Extractor
from contexthub.memory.jev_rule_gate import JevRuleGate
from contexthub.memory.indexer import Indexer
from contexthub.memory.retriever import P1_MIN_SIMILARITY, Retriever
from contexthub.memory.direct_candidates import DIRECT_CANDIDATE_POLICY
from contexthub.memory.condition_binder import ConditionBinder
from contexthub.memory.jev_condition_binder import JevConditionBinder
from contexthub.memory.jev_intent_classifier import JevIntentClassifier
from contexthub.memory.maintenance_executor import MaintenanceExecutor
from contexthub.memory.pipeline import MemoryPipeline
from .runner import (
    MemoryAnswerer,
    OfficialScorer,
    run_episode,
    selected_questions,
    TASKS,
)

ROOT = Path(__file__).resolve().parents[3]
DATA_SHA = "a88d28374a002b3e5b1683fb7201d06a1ce739d2ebf94c971c37bb65cf6ebdd3"


def retry_policy():
    return RetryPolicy(
        max_attempts=3,
        timeout_seconds=60,
        backoff_seconds=(10, 30),
        retry_status_codes=(429, 500, 502, 503, 504),
        retry_transport_errors=(
            "ConnectError",
            "ReadError",
            "WriteError",
            "RemoteProtocolError",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
        ),
    )


def memory_retry_policy():
    return retry_policy().model_copy(update={
        "max_attempts": 5,
        "timeout_increment_seconds": 30,
        "backoff_seconds": (10, 30, 30, 30),
    })


def decision_retry_policy(backend):
    if backend == "llm":
        return memory_retry_policy()
    policy = retry_policy()
    if backend == "jev":
        policy = policy.model_copy(
            update={
                "retry_status_codes": (*policy.retry_status_codes, 529),
            }
        )
    return policy


def probability_threshold(value):
    threshold = float(value)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise argparse.ArgumentTypeError("threshold must be between 0 and 1")
    return threshold


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def memory_request_parameters(model):
    if model == "gpt-4.1-mini":
        return None
    return RequestParameters(
        output_limit_field="max_tokens" if model == "deepseek-flash" else "max_completion_tokens",
        reasoning_effort="low",
        response_format=None,
        seed=SeedPolicy(support="unknown", enabled=False),
        temperature_supported=False,
        disabled_unknown_seed_authorization="No seed parameter is sent in the memory research run",
    )


def freeze(output, config, args):
    output.mkdir(parents=True, exist_ok=False)
    paths = [
        "src/contexthub/memory",
        "src/contexthub/llm",
        "src/contexthub/models/execution.py",
        "integrations/memebench/memory_research",
        "prompts",
        "scripts/memory_research",
        "tests/memory",
    ]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *paths],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for name in files:
        src = ROOT / name
        if src.is_file():
            dest = output / "source" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    (output / "source.patch").write_bytes(
        subprocess.check_output(["git", "diff", "HEAD", "--", *paths], cwd=ROOT)
    )
    shutil.copytree(ROOT / "prompts", output / "prompts")
    memory_params = memory_request_parameters(args.memory_model)
    memory_effort = memory_params.reasoning_effort if memory_params else "omitted"
    manifest = {
        "run_id": output.name,
        "code_version": commit,
        "source_path": str(ROOT),
        "implementation": "contexthub.memory",
        "mode": args.mode,
        "episode": args.episode,
        "task_types": list(TASKS),
        "data_path": str(args.data),
        "data_sha256": DATA_SHA,
        "configuration": config,
        "prompt_sha256": {
            path.relative_to(output / "prompts").as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted((output / "prompts").rglob("*"))
            if path.is_file() and path.suffix in (".txt", ".json")
        },
        "model_parameters": {
            "extractor": {
                "model": args.memory_model,
                "reasoning_effort": memory_effort,
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "rule_gate": {
                "backend": args.rule_gate_backend,
                "model": JEV_MODEL if args.rule_gate_backend == "jev" else args.memory_model,
                "question_type": "choice" if args.rule_gate_backend == "jev" else "text",
            },
            "p1": {
                "backend": "jev",
                "model": JEV_MODEL,
                "endpoint": JEV_ENDPOINT,
                "question_type": "noul",
                "threshold": args.p1_jev_threshold,
                "decision": "noul >= threshold",
            }
            if args.p1_backend == "jev"
            else {
                "backend": "llm",
                "model": args.memory_model,
                "reasoning_effort": memory_effort,
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "p2": {
                "model": args.memory_model,
                "reasoning_effort": memory_effort,
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "p2_direct": {
                "backend": "jev", "model": JEV_MODEL, "endpoint": JEV_ENDPOINT,
                "question_type": "choice", "decision": "candidate-free new/forget/correction/hold; then LLM processing",
                "processing_model": args.memory_model,
                "processing_reasoning_effort": memory_effort,
            } if args.p2_direct_backend == "jev" else {
                "backend": "llm", "model": args.memory_model,
                "reasoning_effort": memory_effort,
                "temperature": "omitted", "max_tokens": "omitted",
            },
            "answer": {"model": "gpt-4.1-mini", "temperature": 0, "max_tokens": 500},
            "judge": {"model": "gpt-4o", "temperature": 0, "max_tokens": "omitted"},
            "embedding": {"provider": args.embedding_provider,
                          "model": config["index"]["embedding_model"],
                          "dimensions": config["index"]["dimensions"]},
        },
        "retry": retry_policy().model_dump(),
        "memory_retry": memory_retry_policy().model_dump(),
        "p1_retry": decision_retry_policy(args.p1_backend).model_dump(),
        "p2_direct_retry": decision_retry_policy(args.p2_direct_backend).model_dump(),
        "p2_processing_retry": memory_retry_policy().model_dump(),
        "rule_gate_retry": decision_retry_policy(args.rule_gate_backend).model_dump(),
        "model_output_attempts": {
            "p1": 1 if args.p1_backend == "jev" else OUTPUT_ATTEMPTS,
            "p2": OUTPUT_ATTEMPTS,
            "p2_direct": OUTPUT_ATTEMPTS,
            "p2_intent": 1 if args.p2_direct_backend == "jev" else None,
        },
        "p1_output_failure": "Jev malformed typed answers stop the trajectory; no text correction or fallback. LLM retains its existing output correction.",
        "repair_output_failure": "After two invalid outputs, log p2.repair_skipped, keep the target stale and continue. Other execution failures still stop the trajectory.",
        "direct_output_failure": "Jev malformed typed decisions stop the trajectory without fallback. LLM direct and forget-scope outputs: after two invalid outputs, log p2.direct_skipped and apply no operation for that item. Other execution failures stop the trajectory.",
        "extraction": {
            "stages": ["rules", "facts"],
            "prompts": ["extractor_rule_jev.json", "extractor_rules_normalize.txt", "extractor_facts.txt"]
            if args.rule_gate_backend == "jev" else ["extractor.txt", "extractor_facts.txt"],
            "rule_gate_backend": args.rule_gate_backend,
            "rule_candidates": "One source-anchored standalone if occurrence per candidate; Jev sees role and containing sentence only, with sentence-relative offsets and one shared state.rule_policy; pysbd 0.3.4 English spans over equal-length line-break substitutions; questions batched per session"
            if args.rule_gate_backend == "jev" else "Session-level if check, then full-session LLM rule extraction",
            "rule_gate_failure": "Malformed Jev answers or request failures stop extraction; no LLM fallback. Only not_rule is omitted; rule and unclear proceed to full-session LLM confirmation."
            if args.rule_gate_backend == "jev" else None,
            "normalization": "Full current session plus forwarded sentence candidates with original-message offsets; LLM confirms user adoption and resolves references before normalizing, may reject all candidates; output refs restricted to forwarded candidates and stripped before downstream use"
            if args.rule_gate_backend == "jev" else "LLM extracts and normalizes rules together",
            "output_attempts_per_stage": 4,
            "skip_rules_without_if": True,
            "merge": "both stages supply text/entities/topics after parsing; normalization candidate refs stay in call logs; program assigns rule/content types and session provenance; only P2-approved content becomes memory",
        },
        "maintenance_prompts": {
            "p1_conditions": "p1_jev.txt"
            if args.p1_backend == "jev"
            else "p1_conditions.txt",
            "p2_conditions": "p2_conditions.txt",
            "p2_direct": "p2_intent_jev.json" if args.p2_direct_backend == "jev" else "p2_direct.txt",
            "p2_forget_scope": "p2_forget_scope.txt",
            **({"p2_new": "p2_new.txt", "p2_correction": "p2_correction.txt"}
               if args.p2_direct_backend == "jev" else {}),
            "p2_repair": "p2_repair.txt",
        },
        "program_owned_references": "Incoming content, source sessions, rule version and repair target are program-owned. In the Jev intent path the memory LLM generates replacement text and selects multiple old targets; the program validates and applies the plan atomically. Models also select affected facts, triggered conditions and actual repair premises.",
        "reference_encoding": "call-local M memory versions; incoming direct content is not a selectable reference",
        "answer_materials": {
            "current": "fresh current facts only; exclude rules before retrieval top-k",
            "policy": "explicit policy/conditional/hypothetical question cues allow fresh facts and separately rendered rules",
            "history": "existing historical retrieval and temporal evidence",
            "routing": "question text only, no benchmark task labels or extra model call; default to current facts",
        },
        "condition_check_scope": "all current facts with active attached rules; no dependency graph",
        "model_explanations": "No reason, issues or additional_evidence_refs in P1/P2 model output. Database status descriptions are program-generated, not model explanations.",
        "session_timeout": 1800,
        "concurrency": 1,
        "retrieval": {
            "text_k": 20,
            "vector_k": 20,
            "hybrid_limit": 20,
            "entity_k": 10,
            "rrf_c": 60,
            "topic_similarity_threshold": args.topic_similarity_threshold,
        },
        "topic_candidates": {
            "selection": "additional OR channel; any normalized label pair cosine strictly above threshold",
            "threshold": args.topic_similarity_threshold,
            "scope": "shared maintenance retrieval (new/correction/default direct), forget supplement, P1 filtering, repair supplement",
            "limits": "eligible rows only; no top-k truncation of topic matches; no topic-only write/delete/binding decision",
            "embedding": "same database embedding model and document prefix on both labels; batch unseen labels and cache in topic_embeddings",
            "answer": "no additional topic-extraction call for answer/history questions",
            "labels": "extracted with facts/rules; Chinese property/topic descriptions independent of current values",
        },
        "p1_candidate_filter": {
            "queries": "condition and consequence from normalized rule text",
            "pool": "initial hits + accepted session facts + prior endpoints; latest current fact per matter",
            "score": "max(condition cosine similarity, affected cosine similarity)",
            "topic_override": "any label-pair similarity > topic_similarity_threshold",
            "min_similarity": P1_MIN_SIMILARITY,
            "top_k": None,
        },
        "p2_new_candidate_filter": {
            **DIRECT_CANDIDATE_POLICY,
            "enabled": args.p2_direct_backend == "jev",
            "scope": "new-intent fact view; shared P1 pool and correction unchanged",
            "query_vector": "reuse initial retrieval vector",
            "keywords": "PG lexemes; per-matter IDF over accepted non-deleted facts through this session; exclude generic lexemes",
            "identifier_exception": "literal query entity containing letters and digits or a URL/path",
            "versions": "retain current candidate of any selected matter",
            "empty": "save extracted text directly: current -> new, historical -> history; no p2_new call",
        },
        "forget_supplement_filter": {
            **DIRECT_CANDIDATE_POLICY,
            "implementation": "Retriever.filter_direct_candidates",
            "scope": "semantic supplement: accepted non-deleted facts and rules through this session",
            "literal_matches_and_primary": "retained without similarity filtering; Luna decides removal scope",
            "query_vector": "reuse initial retrieval vector on Jev path; otherwise share forget search vector",
            "versions": "include current versions of retrieved matters and retain if a past version is selected",
            "empty": "allowed",
        },
        "extracted_content": {
            "text": "save-ready, self-contained; preserve maintenance intent and exact quotations",
            "temporal_scope": ["current", "historical"],
            "empty_new_direct_save_enabled": args.p2_direct_backend == "jev",
            "topics": "required string list; independent assertions remain split; deletion requests describe the requested subject matter",
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    dump(output / "manifest.json", manifest)
    return manifest


def totals(records, paper_prices):
    groups = {}
    for record in records:
        stage = record["identity"]["stage"]
        if stage == "judge":
            group = "scoring"
        elif stage in ("indexing", "query_embedding", "topic_embedding"):
            group = "embedding"
        elif stage == "answer":
            group = "answer"
        else:
            group = "maintenance"
        groups.setdefault(group, []).append(record)
    result = {}
    for name, rows in groups.items():
        currencies = {r["cost"]["currency"] for r in rows if r["cost"].get("currency")}
        costs = [r["cost"].get("amount") for r in rows]
        paper = []
        for r in rows:
            cfg = paper_prices.get(r["requested_model"])
            cost = calculate_cost(
                Usage.model_validate(r["usage"]),
                PriceBasis.model_validate(cfg) if cfg else None,
                r["requested_model"],
            )
            paper.append(cost.amount)
        result[name] = {
            "requests": len(rows),
            "request_seconds": sum(r["wall_seconds"] for r in rows),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in rows)
            if all(r["usage"].get("input_tokens") is not None for r in rows)
            else None,
            "output_tokens": sum(r["usage"]["output_tokens"] for r in rows)
            if all(r["usage"].get("output_tokens") is not None for r in rows)
            else None,
            "actual_cost": sum(Decimal(str(c)) for c in costs)
            if len(currencies) == 1 and all(c is not None for c in costs)
            else None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "paper_price_cost": sum(paper)
            if paper and all(c is not None for c in paper)
            else None,
            "unknown_cost_requests": sum(c is None for c in costs),
        }
    return result


async def run(args):
    jev_key = None
    if "jev" in (args.p1_backend, args.p2_direct_backend, args.rule_gate_backend):
        from dotenv import dotenv_values

        jev_key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(
            ROOT / ".env"
        ).get("TYPESAFE_API_KEY")
        if not jev_key:
            raise ValueError(
                "Set TYPESAFE_API_KEY in the environment or project .env before running Jev"
            )
        probability_threshold(args.p1_jev_threshold)
    if args.episode and args.mode != "check":
        raise ValueError("--episode is only allowed in check mode")
    config = json.loads(args.config.read_text())
    if set(config) != {"index", "prices", "paper_prices"}:
        raise ValueError(
            "config must contain index, prices and paper_prices only; never credentials"
        )
    index_config = resolve_index_config(config, args)
    config["index"] = index_config.model_dump()
    if index_config.text_config not in ("english", "pg_catalog.english"):
        raise ValueError("English full-text configuration required")
    if hashlib.sha256(args.data.read_bytes()).hexdigest() != DATA_SHA:
        raise ValueError("MEME filler32k dataset hash mismatch")
    episodes = json.loads(args.data.read_text())
    if len(episodes) != 100 or {
        t: sum(
            q["task_type"].split(" (")[0] == t
            for e in episodes
            for q in selected_questions(e, "after")
        )
        for t in TASKS
    } != {"Cas": 164, "Abs": 130, "Del": 100, "ER": 100, "Agg": 100, "Tr": 100}:
        raise ValueError("unexpected dataset episode/question counts")
    if args.mode == "check":
        selected = (args.episode,) if args.episode else ("pl_001", "sw_001")
        episodes = [e for e in episodes if e["episode_id"] in selected]
        if len(episodes) != len(selected):
            raise ValueError("requested episode not found")
    run_id = f"memory-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output = args.output / run_id
    providers = json.loads((ROOT / "model_providers.local.json").read_text())
    provider = next(p for p in providers["targets"] if p["label"] == "openlux")
    if provider["base_url"].rstrip("/") != "https://api.openlux.ai/v1":
        raise ValueError("openlux endpoint differs from fixed design")
    memory_provider = next(p for p in providers["targets"] if p["label"] == args.memory_provider)
    embedding_provider = next(p for p in providers["targets"] if p["label"] == args.embedding_provider)
    embedding_config = EmbeddingConfig(
        model=index_config.embedding_model,
        base_url=embedding_provider["base_url"],
        dimensions=index_config.dimensions,
        send_dimensions=True,
        max_batch=1,
        response_archive_directory=str((output / "requests").resolve()),
        retry=retry_policy(),
    )
    manifest = freeze(output, config, args)
    manifest["memory_provider"] = {"label": memory_provider["label"], "base_url": memory_provider["base_url"]}
    manifest["evaluation_provider"] = {"label": provider["label"], "base_url": provider["base_url"]}
    manifest["model_parameters"]["embedding"]["base_url"] = embedding_config.base_url
    dump(output / "manifest.json", manifest)
    store = await MemoryStore.open(os.environ["MEMORY_DATABASE_URL"], index_config)
    from contexthub.memory import contracts

    previous_prompts = contracts.PROMPTS
    contracts.PROMPTS = (output / "prompts").resolve()
    clients = []
    start = time.monotonic()
    try:
        async with store.pool.acquire() as conn:
            manifest["database"] = await conn.fetchval("SELECT current_database()")
            if await conn.fetchval(
                "SELECT 1 FROM session_records WHERE run_id=$1 LIMIT 1", run_id
            ):
                raise ValueError("run already exists")
        dump(output / "manifest.json", manifest)
        print(json.dumps(manifest, default=str), flush=True)
        retry = retry_policy()
        memory_retry = memory_retry_policy()

        def price(model):
            return (
                PriceBasis.model_validate(config["prices"][model])
                if model in config["prices"]
                else None
            )

        def chat(temperature=None, judge=False, memory=False):
            params = (
                RequestParameters(
                    output_limit_field="max_tokens",
                    reasoning_effort=None,
                    response_format="json_object",
                    seed=SeedPolicy(support="unknown", enabled=False),
                    temperature_supported=True,
                    disabled_unknown_seed_authorization="No seed parameter is sent in the fixed memory research protocol",
                )
                if judge
                else memory_request_parameters(args.memory_model) if memory else None
            )
            if params is None:
                params = RequestParameters(
                    output_limit_field="max_tokens", reasoning_effort=None,
                    response_format=None, seed=SeedPolicy(support="unknown", enabled=False),
                    temperature_supported=True,
                    disabled_unknown_seed_authorization="No seed parameter is sent in the memory research run",
                )
            params = params.model_copy(update={
                "response_archive_directory": str((output / "http-responses").resolve()),
            })
            selected_provider = memory_provider if memory else provider
            c = OpenAIChatClient(
                selected_provider["api_key"],
                base_url=selected_provider["base_url"],
                model="gpt-4o" if judge else args.memory_model if memory else "gpt-4.1-mini",
                temperature=temperature,
                timeout=60,
                request_parameters=params,
            )
            clients.append(c)
            return c

        maintenance = chat(memory=True)
        answer_client = chat(0)
        judge_client = chat(0, True)
        emb = RecordedEmbeddingClient(
            embedding_config,
            api_key=embedding_provider["api_key"],
        )
        clients.append(emb)
        indexer = Indexer(store, emb, price=price(index_config.embedding_model))
        retriever = Retriever(
            store,
            indexer,
            RetrievalConfig(
                text_k=20, vector_k=20, hybrid_limit=20, entity_k=10, rrf_c=60,
                topic_similarity_threshold=args.topic_similarity_threshold,
            ),
        )
        jev = None
        if "jev" in (args.p1_backend, args.p2_direct_backend, args.rule_gate_backend):
            jev = JevClient(jev_key)
            clients.append(jev)
        if args.p1_backend == "jev":
            discovery = JevConditionBinder(
                store,
                jev,
                retriever,
                retry=decision_retry_policy("jev"),
                threshold=args.p1_jev_threshold,
                price=price(JEV_MODEL),
            )
        else:
            discovery = ConditionBinder(
                store, maintenance, retriever, retry=memory_retry, price=price(args.memory_model)
            )
        executor = MaintenanceExecutor(
            store,
            retriever,
            indexer,
            maintenance,
            retry=memory_retry,
            price=price(args.memory_model),
            intent_classifier=JevIntentClassifier(
                jev, retry=decision_retry_policy("jev"), price=price(JEV_MODEL),
            ) if args.p2_direct_backend == "jev" else None,
        )
        pipeline = MemoryPipeline(
            store,
            Extractor(
                maintenance, retry=memory_retry, price=price(args.memory_model),
                rule_gate=JevRuleGate(
                    jev, retry=decision_retry_policy("jev"), price=price(JEV_MODEL),
                ) if args.rule_gate_backend == "jev" else None,
            ),
            retriever,
            discovery,
            executor,
            code_version=manifest["code_version"],
        )
        answerer = MemoryAnswerer(
            store, retriever, answer_client, retry, price("gpt-4.1-mini")
        )
        scorer = OfficialScorer(
            args.official_judge,
            judge_client,
            store,
            manifest["code_version"],
            retry.model_copy(update={"max_attempts": 1, "backoff_seconds": ()}),
            price("gpt-4o"),
        )
        reports = []
        for episode in episodes:
            episode_started = time.monotonic()
            result = await run_episode(
                episode,
                run_id=run_id,
                pipeline=pipeline,
                answerer=answerer,
                output=output / (episode["episode_id"] + ".json"),
            )
            async with store.pool.acquire() as conn:
                result["direct_failures"] = await conn.fetchval(
                    "SELECT count(*) FROM memory_steps WHERE run_id=$1 AND episode_id=$2 AND strategy_id='local_base' AND stage='p2.direct_skipped'",
                    run_id,
                    episode["episode_id"],
                )
                result["repair_failures"] = await conn.fetchval(
                    "SELECT count(*) FROM memory_steps WHERE run_id=$1 AND episode_id=$2 AND strategy_id='local_base' AND stage='p2.repair_skipped'",
                    run_id,
                    episode["episode_id"],
                )
            dump(output / (episode["episode_id"] + ".json"), result)
            scored = await scorer.score(result, episode, run_id)
            scored["timings"]["episode_with_scoring"] = (
                time.monotonic() - episode_started
            )
            dump(output / (episode["episode_id"] + ".scored.json"), scored)
            reports.append(scored)
        summary = {
            "run_id": run_id,
            "mode": args.mode,
            "episodes": len(episodes),
            "completed": sum(r["maintenance_status"] == "complete" for r in reports),
            "unjudged": sum(r["unjudged"] for r in reports),
            "repair_failures": sum(r["repair_failures"] for r in reports),
            "direct_failures": sum(r["direct_failures"] for r in reports),
            "wall_seconds": time.monotonic() - start,
            "episode_timings": {r["episode_id"]: r["timings"] for r in reports},
            "timings": {
                stage: {
                    "total_seconds": sum(r["timings"][stage] for r in reports),
                    "mean_seconds": sum(r["timings"][stage] for r in reports)
                    / len(reports),
                }
                for stage in reports[0]["timings"]
            },
            "tasks": {},
        }
        for task in TASKS:
            if task in ("Cas", "Abs", "Del"):
                count = sum(r["trivial_analysis"][task]["total"] for r in reports)
                passed = sum(r["trivial_analysis"][task]["real_pass"] for r in reports)
            else:
                rows = [
                    q for r in reports for q in r["after_answers"]
                    if q["task_type"].split(" (")[0] == task
                ]
                count = len(rows)
                passed = sum(q["u_pass"] is True for q in rows)
            summary["tasks"][task] = {
                "passed": passed,
                "total": count,
                "accuracy": passed / count if count else None,
                "criterion": "paired_real_pass" if task in ("Cas", "Abs", "Del") else "after_u_pass",
            }
        async with store.pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT payload FROM execution_attempts WHERE run_id=$1 AND phase='finished' ORDER BY recorded_at",
                run_id,
            )
            payloads = [r["payload"] for r in records]
            summary["costs"] = totals(payloads, config["paper_prices"])
            summary["p1_costs"] = totals(
                [r for r in payloads if r["identity"]["stage"] == "condition_binding"],
                config["paper_prices"],
            ).get("maintenance")
            summary["episode_costs"] = {
                e["episode_id"]: totals(
                    [
                        r["payload"]
                        for r in await conn.fetch(
                            "SELECT payload FROM execution_attempts WHERE run_id=$1 AND episode_id=$2 AND phase='finished' ORDER BY recorded_at",
                            run_id,
                            e["episode_id"],
                        )
                    ],
                    config["paper_prices"],
                )
                for e in episodes
            }
            for table in (
                "memory_steps",
                "execution_attempts",
                "memories",
                "memory_sources",
                "session_records",
            ):
                rows = await conn.fetch(
                    f"SELECT * FROM {table} WHERE run_id=$1", run_id
                )
                dump(output / (table + ".json"), [dict(r) for r in rows])
            # Include query-only labels (e.g. forget requests), not other runs'
            # cache entries. These vectors make topic decisions reproducible.
            topic_vectors = await conn.fetch(
                """WITH used AS (
                    SELECT unnest(topics) AS topic FROM memories WHERE run_id=$1
                    UNION
                    SELECT jsonb_array_elements_text(item->'topics') AS topic
                    FROM memory_steps,
                         jsonb_array_elements(payload->'items') AS item
                    WHERE run_id=$1 AND stage='extraction.parsed'
                )
                SELECT topic,embedding::text AS embedding FROM topic_embeddings
                WHERE topic IN (SELECT topic FROM used) ORDER BY topic""",
                run_id,
            )
            dump(output / "topic_embeddings.json", [dict(r) for r in topic_vectors])
        dump(output / "summary.json", summary)
    finally:
        contracts.PROMPTS = previous_prompts
        for client in clients:
            await client.close()
        await store.close()


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(
            "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_filler32k.json"
        ),
    )
    parser.add_argument(
        "--official-judge",
        type=Path,
        default=Path(
            "/Users/sherrylin/Documents/PythonProjects/public/MEME-public/code/eval/judge.py"
        ),
    )
    parser.add_argument("--mode", choices=("check", "formal"), required=True)
    parser.add_argument("--episode", help="Run only this episode in check mode")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-model", choices=("gpt-4.1-mini", "gpt-6-luna", "deepseek-flash"),
                        default="gpt-4.1-mini", help="Memory LLM only; Luna and DeepSeek use low reasoning. Answer/judge stay fixed.")
    parser.add_argument("--memory-provider", default="openlux",
                        help="Provider label in model_providers.local.json; only affects the memory LLM")
    parser.add_argument("--embedding-provider", default="openlux", help="Provider label in model_providers.local.json; only affects embeddings")
    add_embedding_index_arguments(parser)
    parser.add_argument("--p1-backend", choices=("llm", "jev"), default="llm")
    parser.add_argument("--p1-jev-threshold", type=probability_threshold, default=0.5)
    parser.add_argument("--topic-similarity-threshold", type=probability_threshold, default=0.8)
    parser.add_argument("--p2-direct-backend", choices=("llm", "jev"), default="llm",
                        help="jev: classify content intent with Jev, then process with the memory LLM; rules are added directly")
    parser.add_argument("--rule-gate-backend", choices=("llm", "jev"), default="llm")
    return parser


def main():
    asyncio.run(run(argument_parser().parse_args()))


if __name__ == "__main__":
    main()
