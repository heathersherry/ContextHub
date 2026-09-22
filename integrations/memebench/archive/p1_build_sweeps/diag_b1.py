"""B1 诊断：坐实"标脏节点 ≠ 答案边上游节点 ≠ 答题命中节点"的身份错配。

只读、不改核心逻辑。复刻 run_one 的 cascade+raw+p2 流程，跑指定的几个 B1 铁案，
在 ON drain 后 dump 并对齐：
  (1) superseded 集合（收到 modified 事件的物理节点 id）
  (2) dependencies 表里的真实边（dependency -> dependent）
  (3) 答题命中的节点 id（用 retrieved_l2 反查 inserted_nodes）+ 各节点 stale 状态

用法：CONTEXTHUB_INTEGRATION=1 .venv/bin/python -u -m integrations.memebench.diag_b1 EP_ID [EP_ID...]
"""
from __future__ import annotations

import asyncio
import sys

from integrations.memebench.answer import answer_question
from integrations.memebench.ingest import (
    apply_root_change_raw,
    ingest_case_raw_cascade_e2e,
    ingest_filler,
)
from integrations.memebench.loader import extract_cascade_cases, load_episodes
from integrations.memebench.run_eval import DEFAULT_DATA, _account_for
from integrations.memebench.systems import build_system


async def _status_map(db, ids):
    """{id: status} for given node ids."""
    if not ids:
        return {}
    rows = await db.fetch(
        "SELECT id, status FROM contexts WHERE id = ANY($1)", list(ids)
    )
    return {r["id"]: r["status"] for r in rows}


async def _db_edges(db):
    rows = await db.fetch(
        "SELECT dependency_id, dependent_id, dep_type FROM dependencies"
    )
    return [(r["dependency_id"], r["dependent_id"], r["dep_type"]) for r in rows]


async def diag_one(system, case, brief=False):
    account = _account_for(case)
    embed_batch = system.embedding.embed_batch
    print(f"\n{'=' * 78}")
    print(f"=== {case.episode_id} / {case.target_entity} (hop{case.hop}) ===")
    print(f"    gold  = {case.gold_answer!r}")
    print(f"    root_change: {case.root_change.get('before')!r} -> {case.root_change.get('after')!r}")
    print(f"    after_q = {case.after_question.question!r}")

    async with system.repo.session(account) as db:
        await db.execute("TRUNCATE contexts, dependencies, change_events, audit_log CASCADE")

    async with system.repo.session(account) as db:
        graph, _tiers = await ingest_case_raw_cascade_e2e(
            db, case, account, embed_batch,
            extractor=system.extractor,
            disamb_cheap=system.cascade_cheap_svc,
            disamb_strong=system.cascade_strong_svc,
            edge_cheap=system.cascade_cheap_svc,
            edge_strong=system.cascade_strong_svc,
            tau_disamb=0.5, tau_cand=0.4, tau_edge=0.4, k=5,
        )
        await ingest_filler(db, case, account, embed_batch, granularity="session")

    # 变更到达 + superseded
    async with system.repo.session(account) as db:
        superseded = await apply_root_change_raw(
            db, case, account, graph, embed_batch,
            system.extractor, system.discovery_chat,
        )

    id2text = {nid: t for nid, t in graph.inserted_nodes}  # 含 change 节点

    # OFF 答题
    async with system.repo.session(account) as db:
        off = await answer_question(system, db, account, case.after_question.question)

    # ON: drain
    engine = system.build_engine(cascade_on_stale=True)
    engine._running = True
    for _ in range(8):
        await engine._drain_ready_events(context_id=None)

    async with system.repo.session(account) as db:
        on = await answer_question(system, db, account, case.after_question.question)
        status = await _status_map(db, list(id2text.keys()))
        edges = await _db_edges(db)

    _dump(case, graph, superseded, id2text, status, edges, off, on)


def _dump(case, graph, superseded, id2text, status, edges, off, on):
    order = {nid: i for i, (nid, _t) in enumerate(graph.inserted_nodes)}

    def short(nid):
        return f"#{order.get(nid, -1):02d}:{str(nid)[:8]}"

    def txt(nid):
        return (id2text.get(nid) or "?").replace("\n", " ")[:72]

    def st(nid):
        return status.get(nid, "?")

    print(f"\n  OFF answer = {off.answer!r}")
    print(f"  ON  answer = {on.answer!r}")
    print(f"  off==on ? {off.answer.strip() == on.answer.strip()}   (相同 => 标脏没碰到答题读到的节点)")

    print(f"\n  [1] superseded（收到 modified 事件的节点） n={len(superseded)}")
    for nid in superseded:
        print(f"      {short(nid)} status={st(nid):6} | {txt(nid)}")

    print(f"\n  [2] dependencies 表里的边（up -> down） n={len(edges)}   "
          f"(graph.persisted_edges n={len(graph.persisted_edges)})")
    sup = set(superseded)
    for up, down, dtype in edges:
        mark = "  <== up 是 superseded" if up in sup else ""
        print(f"      up={short(up)}[{st(up)}] -> down={short(down)}[{st(down)}] ({dtype}){mark}")
        print(f"         up  : {txt(up)}")
        print(f"         down: {txt(down)}")

    print(f"\n  [3] ON 答题命中的节点（retrieved_l2 反查 id） n={len(on.retrieved_l2)}")
    text2id = {}
    for nid, t in id2text.items():
        if t:
            text2id.setdefault(t.strip(), nid)
    hit_ids = []
    for l2 in on.retrieved_l2:
        hid = text2id.get((l2 or "").strip())
        if hid:
            hit_ids.append(hid)
        tag = f"{short(hid)}[{st(hid)}]" if hid else "??(未映射,可能是filler)"
        print(f"      {tag} | {(l2 or '').replace(chr(10), ' ')[:72]}")

    stale_ids = [nid for nid, s in status.items() if s == "stale"]
    print(f"\n  [诊断]")
    print(f"      stale 节点总数 = {len(stale_ids)}")
    for nid in stale_ids:
        print(f"        stale: {short(nid)} | {txt(nid)}")
    print(f"      superseded 的下游 dependents：")
    for nid in superseded:
        downs = [short(d) for u, d, _ in edges if u == nid]
        print(f"        {short(nid)} -> {downs or '无（=标脏够不到任何派生节点）'}")
    hit_set = set(hit_ids)
    print(f"      答题命中 ∩ superseded = {[short(x) for x in hit_set & sup] or '空'}")
    print(f"      答题命中 ∩ stale      = {[short(x) for x in hit_set & set(stale_ids)] or '空'}")
    upstreams = {u for u, d, _ in edges if d in hit_set}
    print(f"      答题命中节点的上游 ∩ superseded = "
          f"{[short(x) for x in upstreams & sup] or '空（=答案边挂在另一张纸条上 → B1 实锤）'}")

    # 关键：答案节点 vs superseded 的插入顺序。若答案节点更早，增量写路径
    # （只跟更早的节点比）在原理上就建不出 superseded -> 答案节点 这条边。
    print(f"      插入顺序检查（增量写路径只允许 晚→早 的边）：")
    for s in superseded:
        for h in hit_set:
            os_, oh = order.get(s, -1), order.get(h, -1)
            if os_ < 0 or oh < 0:
                continue
            ok = "可建（答案节点更晚）" if oh > os_ else "★不可建（答案节点比 superseded 更早到）"
            print(f"        superseded {short(s)} vs 命中 {short(h)} -> {ok}")


async def main(ep_ids):
    system = await build_system(
        provider_label="openlux",
        chat_model="gpt-4.1-mini",
        extract_model="gpt-4.1-mini",  # diag uses system.extractor; must be named
        cascade=True, cascade_cheap_model="gpt-4o-mini", cascade_strong_model="gpt-5.5",
        p2_cascade=True, p2_cheap_model="gpt-4o-mini", oracle_model="gpt-5.5",
    )
    try:
        episodes = load_episodes(DEFAULT_DATA)
        by_id = {}
        for c in extract_cascade_cases(episodes, hop=None):
            by_id.setdefault(c.episode_id, c)
        for ep in ep_ids:
            case = by_id.get(ep)
            if case is None:
                print(f"!! {ep} not found")
                continue
            await diag_one(system, case)
    finally:
        await system.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["pl_012", "sw_009", "sw_011"]))
