"""Runner-scaffold tests for the archived chronological P1+P2 experiment.

Split out of ``test_memebench_planned_propagation.py`` on 2026-09-07.  Both tests
below exercise ``run_chronological_p1p2``'s own case-record shape and CLI, not the
live ``planned_propagation`` executor, so they move with that archived runner.
The helpers and the two test bodies are copied verbatim from the original file.

Coverage that the original file kept (verified before splitting):
  * ``execute_frontier`` -- 4 other tests still cover it
  * ``point``/``cp-upper`` contracts -- ``test_cp_upper_plan_is_not_point_only``
  * the five registered build plans -- ``test_memebench_chronological_policy.py``
  * ``build_parser`` rejecting missing models -- ``test_e2e_requires_judge_model``
    in ``test_memebench_chronological_runner.py`` covers the same parser
"""

from __future__ import annotations

import uuid

import pytest

from integrations.memebench.chronological_policy import registered_build_plans
from integrations.memebench.planned_propagation import (
    PlannedDerivedMemoryRule,
    execute_frontier,
    plan_published_graph,
)
from integrations.memebench.run_chronological_p1p2 import (
    GROUPS,
    build_case_record,
    build_parser,
    group_spec,
    json_safe,
)


class CountingChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []
        self.total_tokens = 0

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        self.total_tokens += max(1, len(prompt) // 4) + 3
        return self.reply


class FakeRepo:
    def __init__(self, texts: dict) -> None:
        self._texts = {str(key): value for key, value in texts.items()}

    def session(self, account_id: str):
        texts = self._texts

        class _Ctx:
            async def __aenter__(self_):
                class _Db:
                    async def fetchrow(self__, sql, ctx_id):
                        text = texts.get(str(ctx_id), "derived note about the team lead")
                        return {
                            "l2_content": text,
                            "l1_content": None,
                            "l0_content": None,
                        }

                return _Db()

            async def __aexit__(self_, *exc):
                return False

        return _Ctx()


CONTRACT = {
    "J1": {"expected_cost": 0.0, "delta": 0.4},
    "J3": {"expected_cost": 20.0, "delta": 0.2},
    "J4": {"expected_cost": 80.0, "delta": 0.05},
    "cascade": {"expected_cost": 30.0, "delta": 0.05},
    "direct-stale": {"expected_cost": 1000.0, "delta": 0.0},
}


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _rule(modes, cheap="YES\ncheap", strong="YES\nstrong", texts=None):
    cheap_chat = CountingChat(cheap)
    strong_chat = CountingChat(strong)
    rule = PlannedDerivedMemoryRule(
        modes=modes,
        repo=FakeRepo(texts or {}),
        strong_chat=strong_chat,
        cheap_chat=cheap_chat,
    )
    return rule, cheap_chat, strong_chat


@pytest.mark.asyncio
async def test_g0_g3_smoke_records_and_both_contracts() -> None:
    assert set(GROUPS) == {"G0", "G1", "G2", "G3"}
    assert group_spec("G0") == {"p1": "old", "p2": "old"}
    assert group_spec("G3") == {"p1": "new", "p2": "new"}
    root, child, _ = _ids()
    for method in ("point", "cp-upper"):
        planned = plan_published_graph(
            [(root, child)],
            contract=CONTRACT,
            epsilon=0.25,
            contract_method=method,
        )
        rule, _, _ = _rule(planned["assignments"], cheap="YES\n", strong="YES\n")
        frontier = await execute_frontier(
            rule,
            root_id=root,
            adjacency={root: [child]},
            account_id="acct",
        )
        for group in GROUPS:
            record = build_case_record(
                episode_id="ep-smoke",
                hop=1,
                group=group,
                p1_policy="T_current_tau",
                consolidation_mode="async-each-session",
                p1={"n_gold": 1, "n_pred": 1, "n_tp": 1},
                p2={
                    "contract_method": method,
                    "solver": planned["solver"],
                    "planned_mode_mix": planned["planned_mode_mix"],
                    "executed_mode_mix": frontier.executed_mode_mix,
                    "max_path_risk": planned["max_path_risk"],
                    "direct_stale_count": planned["planned_mode_mix"].get(
                        "direct-stale", 0
                    ),
                },
            )
            dumped = json_safe(record)
            assert dumped["group"] == group
            assert dumped["errors"] == []
            assert dumped["p1"]["scored"] is True
            assert dumped["p1"]["graph_miss"] is False
            assert dumped["p2"]["receding_horizon_complete"] is False
            assert "Infinity" not in str(dumped)
    failed = build_case_record(
        episode_id="ep-fail",
        hop=1,
        group="G0",
        p1_policy="T_current_tau",
        consolidation_mode="sync-inline",
        errors=["boom"],
    )
    assert failed["p1"]["scored"] is False
    assert failed["p1"]["graph_miss"] is None
    assert failed["outcome"]["false_fresh"] is None
    assert set(registered_build_plans()) == {
        "E_economy",
        "T_current_tau",
        "B_lambda",
        "R_full_cheap",
        "R_full_verify",
    }


def test_runner_requires_explicit_models() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run-e2e", "--hop", "1", "--contract", "point"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run-p1-selection", "--hop", "1"])
    args = parser.parse_args(["prepare-split", "--hop", "1", "--data", "/tmp/x.json"])
    assert args.command == "prepare-split"
