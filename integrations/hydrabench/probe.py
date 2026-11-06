"""Measure fixed-query retrieval separately from agent-generated queries.

Table queries use only the bound database and final question. Text queries use
the release's annotated search_question as an explicitly oracle diagnostic;
these annotations are never sent to the answering agent.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from contexthub_sdk import ContextHubClient, ContextLevel, ContextType


async def probe(args):
    cases = json.loads(args.cases.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    async with ContextHubClient(url=args.url, api_key=os.environ["CONTEXTHUB_API_KEY"],
                                account_id="acme", agent_id="query-agent", timeout=120) as client:
        for case in cases:
            result = {"case_id": case["id"], "type": case["type"], "db_id": case["db_id"],
                      "reference_tables": case["reference_tables"], "top_k": 3}
            for kind, query in [
                ("table_schema", f"Database: {case['db_id']}. {case['final_question']}"),
                ("resource", case["search_question"]),
            ]:
                response = await client.search(query, context_type=[ContextType(kind)],
                                               top_k=3, level=ContextLevel.L1)
                result[kind] = {"query": query, "response": response.model_dump(mode="json")}
            required = {f"ctx://datalake/hydra/{case['db_id']}/{t}".casefold()
                        for t in case["reference_tables"]}
            found = [r["uri"].casefold() for r in result["table_schema"]["response"]["results"]]
            hits = required.intersection(found)
            result["table_metrics"] = {"required": len(required), "hits_at_3": len(hits),
                                       "recall_at_3": len(hits) / len(required),
                                       "all_required_at_3": hits == required,
                                       "first_hit_rank": next((i for i, uri in enumerate(found, 1) if uri in required), None)}
            (args.output / f"{case['id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(case["id"], result["table_metrics"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(probe(parser.parse_args()))
