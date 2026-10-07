"""Executable, opt-in runner. Configuration preparation never calls models."""

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.llm.request_parameters import RequestParameters, SeedPolicy
from contexthub.llm.retry_policy import RetryPolicy
from contexthub.models.execution import PriceBasis, Usage, calculate_cost
from contexthub.memory.storage import MemoryStore
from contexthub.memory.schemas import IndexConfig, RetrievalConfig
from contexthub.memory.extractor import Extractor
from contexthub.memory.indexer import Indexer
from contexthub.memory.retriever import Retriever
from contexthub.memory.dependency_discovery import DependencyDiscovery
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


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


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
    manifest = {
        "run_id": output.name,
        "code_version": commit,
        "source_path": str(ROOT),
        "implementation": "contexthub.memory",
        "mode": args.mode,
        "data_path": str(args.data),
        "data_sha256": DATA_SHA,
        "configuration": config,
        "model_parameters": {
            "extractor": {
                "model": "gpt-4.1-mini",
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "p1": {
                "model": "gpt-4.1-mini",
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "p2": {
                "model": "gpt-4.1-mini",
                "temperature": "omitted",
                "max_tokens": "omitted",
            },
            "answer": {"model": "gpt-4.1-mini", "temperature": 0, "max_tokens": 500},
            "judge": {"model": "gpt-4o", "temperature": 0, "max_tokens": "omitted"},
            "embedding": {"model": "text-embedding-3-small", "dimensions": 1536},
        },
        "retry": retry_policy().model_dump(),
        "session_timeout": 1800,
        "concurrency": 1,
        "retrieval": {
            "text_k": 20,
            "vector_k": 20,
            "hybrid_limit": 20,
            "entity_k": 10,
            "rrf_c": 60,
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
        elif stage in ("indexing", "query_embedding"):
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
            "wall_seconds": sum(r["wall_seconds"] for r in rows),
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
    config = json.loads(args.config.read_text())
    if set(config) != {"index", "prices", "paper_prices"}:
        raise ValueError(
            "config must contain index, prices and paper_prices only; never credentials"
        )
    index_config = IndexConfig.model_validate(config["index"])
    if (
        index_config.embedding_model != "text-embedding-3-small"
        or index_config.dimensions != 1536
        or index_config.text_config not in ("english", "pg_catalog.english")
    ):
        raise ValueError("first-run model/dimensions/English configuration mismatch")
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
    } != {"Cas": 164, "Abs": 130, "Del": 100}:
        raise ValueError("unexpected dataset episode/question counts")
    if args.mode == "check":
        episodes = [e for e in episodes if e["episode_id"] in ("pl_001", "sw_001")]
    run_id = f"memory-{args.mode}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output = args.output / run_id
    manifest = freeze(output, config, args)
    providers = json.loads((ROOT / "model_providers.local.json").read_text())
    provider = next(p for p in providers["targets"] if p["label"] == "openlux")
    if provider["base_url"].rstrip("/") != "https://api.openlux.ai/v1":
        raise ValueError("openlux endpoint differs from fixed design")
    key = provider["api_key"]
    base = provider["base_url"]
    store = await MemoryStore.open(os.environ["MEMORY_DATABASE_URL"], index_config)
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

        def price(model):
            return (
                PriceBasis.model_validate(config["prices"][model])
                if model in config["prices"]
                else None
            )

        def chat(temperature=None, judge=False):
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
                else None
            )
            c = OpenAIChatClient(
                key,
                base_url=base,
                model="gpt-4o" if judge else "gpt-4.1-mini",
                temperature=temperature,
                timeout=60,
                request_parameters=params,
            )
            clients.append(c)
            return c

        maintenance = chat()
        answer_client = chat(0)
        judge_client = chat(0, True)
        emb = RecordedEmbeddingClient(
            EmbeddingConfig(
                model=index_config.embedding_model,
                base_url=base,
                dimensions=1536,
                send_dimensions=True,
                max_batch=1,
                response_archive_directory=str((output / "requests").resolve()),
                retry=retry,
            ),
            api_key=key,
        )
        clients.append(emb)
        indexer = Indexer(store, emb, price=price(index_config.embedding_model))
        retriever = Retriever(
            store,
            indexer,
            RetrievalConfig(
                text_k=20, vector_k=20, hybrid_limit=20, entity_k=10, rrf_c=60
            ),
        )
        discovery = DependencyDiscovery(
            store, retriever, maintenance, retry=retry, price=price("gpt-4.1-mini")
        )
        executor = MaintenanceExecutor(
            store,
            retriever,
            indexer,
            discovery,
            maintenance,
            retry=retry,
            price=price("gpt-4.1-mini"),
        )
        pipeline = MemoryPipeline(
            store,
            Extractor(maintenance, retry=retry, price=price("gpt-4.1-mini")),
            indexer,
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
            result = await run_episode(
                episode,
                run_id=run_id,
                pipeline=pipeline,
                answerer=answerer,
                output=output / (episode["episode_id"] + ".json"),
            )
            scored = await scorer.score(result, episode, run_id)
            dump(output / (episode["episode_id"] + ".scored.json"), scored)
            reports.append(scored)
        summary = {
            "run_id": run_id,
            "mode": args.mode,
            "episodes": len(episodes),
            "completed": sum(r["maintenance_status"] == "complete" for r in reports),
            "unjudged": sum(r["unjudged"] for r in reports),
            "wall_seconds": time.monotonic() - start,
            "tasks": {},
        }
        for task in TASKS:
            count = sum(r["trivial_analysis"][task]["total"] for r in reports)
            passed = sum(r["trivial_analysis"][task]["real_pass"] for r in reports)
            summary["tasks"][task] = {
                "passed": passed,
                "total": count,
                "accuracy": passed / count if count else None,
            }
        async with store.pool.acquire() as conn:
            records = await conn.fetch(
                "SELECT payload FROM execution_attempts WHERE run_id=$1 AND phase='finished' ORDER BY recorded_at",
                run_id,
            )
            payloads = [r["payload"] for r in records]
            summary["costs"] = totals(payloads, config["paper_prices"])
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
                "dependencies",
                "change_events",
                "session_records",
            ):
                rows = await conn.fetch(
                    f"SELECT * FROM {table} WHERE run_id=$1", run_id
                )
                dump(output / (table + ".json"), [dict(r) for r in rows])
        dump(output / "summary.json", summary)
    finally:
        for client in clients:
            await client.close()
        await store.close()


def main():
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
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
