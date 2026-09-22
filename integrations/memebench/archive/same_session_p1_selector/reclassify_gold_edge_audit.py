"""Rebuild the versioned P1 gold-edge v1.1 reclassification view."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.memebench.gold_edge_audit import (
    atomic_write_text,
    canonical_json_bytes,
    read_jsonl_tolerant,
    reclassify_distinct_same_session_records,
    sha256_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    records = reclassify_distinct_same_session_records(read_jsonl_tolerant(args.raw))
    rendered = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
    if args.out.exists() and args.out.read_bytes() != rendered:
        raise ValueError(f"immutable reclassification output differs: {args.out}")
    if not args.out.exists():
        atomic_write_text(args.out, rendered.decode("utf-8"))
    actual = sha256_file(args.out)
    if args.expected_sha256 and actual != args.expected_sha256:
        raise ValueError(
            f"reclassified SHA-256 mismatch: expected={args.expected_sha256}, actual={actual}"
        )
    print(f"records={len(records)}")
    print(f"sha256={actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
