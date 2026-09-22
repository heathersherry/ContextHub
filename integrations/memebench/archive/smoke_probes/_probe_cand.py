"""一次性探针：坐实变量4的 recency 档是否把 block 档已捞到的真上游丢掉了。

只读诊断，不改核心库、不写 eval 结果。做法：重跑一个 case 的建图，
在 route_candidate_selection 外面包一层，逐条 fact 记录
  - block 档捞到几条、conf_block 多少（决定是否升档）
  - 最终选中的档位、返回候选数
  - 真上游（文本含指定关键词的已插入节点）是否 在 block 里 / 在最终候选里
"block 有、最终候选没有" 的行数即"升档反而丢候选"的实证。

用法：CONTEXTHUB_INTEGRATION=1 .venv/bin/python -u -m integrations.memebench._probe_cand sw_011 --key Quenthar
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from contexthub.services import cascade_router as cr
from integrations.memebench.ingest import ingest_case_raw_cascade_e2e
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.run_eval import DEFAULT_DATA, _account_for
from integrations.memebench.systems import build_system


def install_probe(keys: list[str], rows: list[dict]) -> None:
    """Wrap route_candidate_selection in-place; restore is unnecessary (one-shot script)."""
    orig = cr.route_candidate_selection

    def probed(new_text, new_emb, pool, tau, *, k=10, min_cands=3, recency=False):
        route = orig(new_text, new_emb, pool, tau, k=k, min_cands=min_cands, recency=recency)

        nw = cr._content_words(new_text)
        block = [c for c in pool if cr._content_words(c.text) & nw]
        conf_block = max((cr._cosine(new_emb, c.embedding) for c in block), default=0.0)
        conf_block = max(conf_block, 0.0)

        def hits(cands):
            return [c for c in cands if any(kw.lower() in c.text.lower() for kw in keys)]

        up_pool, up_block, up_final = hits(pool), hits(block), hits(route.candidates)
        best_pool = max((cr._cosine(new_emb, c.embedding) for c in pool), default=0.0)

        rows.append(
            {
                "text": new_text[:70],
                "pool": len(pool),
                "block_n": len(block),
                "conf_block": round(conf_block, 3),
                "tier": route.tier,
                "final_n": len(route.candidates),
                "conf": round(route.confidence, 3),
                "best_pool_cos": round(best_pool, 3),
                "up_in_pool": len(up_pool),
                "up_in_block": len(up_block),
                "up_in_final": len(up_final),
            }
        )
        return route

    cr.route_candidate_selection = probed
    # ingest.py imports the symbol inside the function body, so patching the
    # module attribute is enough (no stale local binding).


async def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--key", action="append", default=[], help="真上游关键词，可多次")
    ap.add_argument("--tau-cand", type=float, default=0.4)
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args(argv)

    cases = extract_cascade_cases(load_episodes(DEFAULT_DATA))
    case = next((c for c in cases if c.episode_id == args.episode), None)
    if case is None:
        sys.exit(f"episode {args.episode} not found")

    rows: list[dict] = []
    install_probe(args.key or ["Quenthar"], rows)

    # Same wiring as diag_b1.py so the ingest path is identical.
    system = await build_system(
        provider_label="openlux",
        chat_model="gpt-4.1-mini",
        extract_model="gpt-4.1-mini",
        cascade=True, cascade_cheap_model="gpt-4o-mini", cascade_strong_model="gpt-5.5",
        p2_cascade=True, p2_cheap_model="gpt-4o-mini", oracle_model="gpt-5.5",
    )
    account = _account_for(case)
    try:
        async with system.repo.session(account) as db:
            await db.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")
        async with system.repo.session(account) as db:
            await ingest_case_raw_cascade_e2e(
                db, case, account, system.embedding.embed_batch,
                extractor=system.extractor,
                disamb_cheap=system.cascade_cheap_svc,
                disamb_strong=system.cascade_strong_svc,
                edge_cheap=system.cascade_cheap_svc,
                edge_strong=system.cascade_strong_svc,
                tau_disamb=0.5, tau_cand=args.tau_cand, tau_edge=0.4,
                k=args.k,
            )
    finally:
        if hasattr(system, "close"):
            maybe = system.close()
            if asyncio.iscoroutine(maybe):
                await maybe

    hdr = ("fact", "pool", "blk", "confB", "tier", "finN", "conf", "bestCos",
           "upPool", "upBlk", "upFin", "text")
    print("\n" + " | ".join(hdr))
    lost = 0
    for i, r in enumerate(rows, start=2):
        flag = ""
        if r["up_in_block"] > 0 and r["up_in_final"] == 0:
            flag = "  <== BLOCK有/最终无（升档丢候选）"
            lost += 1
        elif r["up_in_pool"] > 0 and r["up_in_final"] == 0:
            flag = "  <== 池里有/最终无"
        print(
            f"{i:>4} | {r['pool']:>4} | {r['block_n']:>3} | {r['conf_block']:>5} | "
            f"{r['tier']:<10} | {r['final_n']:>4} | {r['conf']:>5} | {r['best_pool_cos']:>7} | "
            f"{r['up_in_pool']:>6} | {r['up_in_block']:>5} | {r['up_in_final']:>5} | "
            f"{r['text']}{flag}"
        )
    print(f"\n升档丢候选的 fact 数 = {lost} / {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
