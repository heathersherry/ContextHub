"""Replay saved search ToolMessages through the new adapter, without model calls.

Run from the repository root, with src, sdk/src and plugins/deepagents/src on
PYTHONPATH. The output path must be new. No benchmark annotations are consumed.
"""

import argparse
import json
from pathlib import Path

from contexthub_sdk import ContextLevel, SearchResponse
from contexthub_deepagents.tools import _search_payload


def replay(traces, output):
    counts = dict(search_messages=0, hits=0, same_l0_l1=0,
                  duplicated_body_characters=0, before_json_characters=0,
                  after_json_characters=0)
    records = []
    for path in sorted(traces.glob("*.json")):
        for message in json.loads(path.read_text())["messages"]:
            if message["type"] != "tool" or message["data"].get("name") != "contexthub_search":
                continue
            before = json.loads(message["data"]["content"])
            if "results" not in before:
                continue
            after = _search_payload(SearchResponse.model_validate(before), ContextLevel.L1)
            sizes = [len(json.dumps(item, ensure_ascii=False)) for item in (before, after)]
            counts["search_messages"] += 1
            counts["hits"] += len(before["results"])
            counts["before_json_characters"] += sizes[0]
            counts["after_json_characters"] += sizes[1]
            for old, new in zip(before["results"], after["results"], strict=True):
                assert new["content"] == old["l1_content"]
                assert new["uri"] == old["uri"] and new["version"] == old["version"]
                if old["l0_content"] == old["l1_content"]:
                    counts["same_l0_l1"] += 1
                    counts["duplicated_body_characters"] += len(old["l0_content"])
            records.append(dict(case_id=path.stem, tool_call_id=message["data"]["tool_call_id"],
                                hits=len(before["results"]), before_characters=sizes[0], after_characters=sizes[1]))
    report = dict(counts=counts, records=records, actual_new_model_usage=None,
                  estimated_tokens_chars_div_4={key: counts[key] / 4 for key in
                                               ("before_json_characters", "after_json_characters")})
    with output.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    replay(args.traces, args.output)
