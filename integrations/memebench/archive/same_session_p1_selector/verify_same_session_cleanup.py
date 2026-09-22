"""Verify that all dedicated same-session validation accounts are empty."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from integrations.memebench.gold_edge_audit import atomic_write_json
from integrations.memebench.run_gold_edge_audit import _account_empty, _build_system
from integrations.memebench.run_same_session_manual_validation import validation_account


async def run(args) -> int:
    manifest = json.loads((args.out / "validation_manifest.json").read_text(encoding="utf-8"))
    system = await _build_system(args)
    try:
        rows = [
            {
                "episode_id": item["episode_id"],
                "account": validation_account(item["episode_id"]),
                "empty": await _account_empty(system, validation_account(item["episode_id"])),
            }
            for item in manifest["episodes"]
        ]
    finally:
        await system.close()
    result = {"all_accounts_empty": all(row["empty"] for row in rows), "accounts": rows}
    atomic_write_json(args.out / "account_cleanup_verification.json", result)
    if not result["all_accounts_empty"]:
        raise RuntimeError("one or more validation accounts are not empty")
    print("all_validation_accounts_empty=true")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent
        / "runs"
        / "p1_same_session_manual_validation_20260823",
    )
    parser.add_argument("--chat-model", default="gpt-4.1-mini")
    parser.add_argument("--extract-model", default="gpt-4.1-mini")
    parser.add_argument("--p1-cheap-model", default="gpt-4o-mini")
    parser.add_argument("--p1-strong-model", default="gpt-4.1-mini")
    parser.add_argument("--embedding-provider", default="aliyun")
    parser.add_argument("--embedding-model", default="text-embedding-v4")
    parser.add_argument("--provider", default="openlux")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
