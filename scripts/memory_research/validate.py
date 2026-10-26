"""Real-PG memory-maintenance verification with deterministic HTTP model fixtures, not MEME."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from contexthub.memory.schemas import IndexConfig
from contexthub.memory.storage import MemoryStore


async def inspect_database(config, run_id):
    store = await MemoryStore.open(
        os.environ["MEMORY_DATABASE_URL"], IndexConfig.model_validate(config["index"])
    )
    try:
        async with store.pool.acquire() as conn:
            info = {
                "database": await conn.fetchval("SELECT current_database()"),
                "postgres": await conn.fetchval("SHOW server_version"),
                "pgvector": await conn.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname='vector'"
                ),
            }
            info["rows"] = {
                table: await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE tenant_id=$1", run_id
                )
                for table in (
                    "session_records",
                    "memories",
                    "memory_sources",
                    "execution_attempts",
                )
            }
            info["finished_requests"] = await conn.fetchval(
                "SELECT count(*) FROM execution_attempts WHERE tenant_id=$1 AND phase='finished'",
                run_id,
            )
            return info
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(
        (ROOT / "docs/memory-research/validation-config.json").read_text()
    )
    database_before = asyncio.run(inspect_database(config, args.run_id))
    if any(database_before["rows"].values()):
        raise ValueError("run_id already has data; use a new run_id")
    report = {
        "implementation": "contexthub.memory",
        "source_path": str(ROOT / "src/contexthub/memory"),
        "code_version": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "source_tree_sha256": subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import hashlib,pathlib; p=pathlib.Path('src/contexthub/memory'); print(hashlib.sha256(b''.join(f.name.encode()+f.read_bytes() for f in sorted(p.glob('*')) if f.is_file())).hexdigest())",
            ],
            cwd=ROOT,
            text=True,
        ).strip(),
        "tracked_changes_before": subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"], cwd=ROOT, text=True
        ).splitlines(),
        "run_id": args.run_id,
        "database": database_before["database"],
        "configuration": config,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real PostgreSQL persistence/retrieval; fixture chat and embedding responses; no MEME or real model quality claims",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    environment = dict(os.environ, MEMORY_VALIDATION_RUN_ID=args.run_id)
    groups = {
        "memory-mechanism": ["tests/memory"],
        "shared-regression": [
            "tests/test_knowledge_retry.py",
            "tests/test_knowledge_ledger.py",
            "tests/test_recorded_embedding.py",
            "tests/test_chat_client_retry_usage.py",
            "tests/test_memebench_patha_ledger.py",
            "tests/session_protocol/test_think_response.py",
        ],
    }
    report["checks"] = {}
    for name, files in groups.items():
        command = [
            sys.executable,
            "-m",
            "pytest",
            *files,
            "-q",
            "--junitxml=" + str(output / (name + ".xml")),
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print(result.stdout, flush=True)
        (output / (name + ".txt")).write_text(result.stdout)
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        xml_path = output / (name + ".xml")
        if xml_path.exists():
            for suite in ET.parse(xml_path).iter("testsuite"):
                for key in counts:
                    counts[key] += int(suite.get(key, 0))
        report["checks"][name] = {
            "exit_code": result.returncode,
            "command": command,
            **counts,
        }
    report.update(asyncio.run(inspect_database(config, args.run_id)))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["success"] = all(
        check["exit_code"] == 0 and check["tests"] > 0 and check["skipped"] == 0
        for check in report["checks"].values()
    )
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
