"""Run the fail-closed, minimum-paid legacy MEME layer-oracle preflight."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.memebench.oracle_paid_layer_experiment import (
    JUDGE_CASES,
    RunLock,
    build_retrieval_manifest,
    canonical_sha256,
    exact_case_manifest,
    sha256_file,
    stage_gate_summary,
)

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "runs" / "meme_paid_oracle_layers_20260825_v1"
AUDIT = HERE / "runs" / "meme_oracle_layer_audit_20260825_v1" / "case_audit.json"
LEDGER = HERE / "adjudications" / "meme_oracle_layer_decisions_20260825_v1.json"
HOP1 = HERE / "runs" / "p1p2_hop1_strong41mini"
HOP2 = HERE / "runs" / "p1p2_hop2_strong41mini"
JUDGE_OUTPUT = HERE / "adjudications" / "meme_oracle_judge_blind_20260825_v1.json"
CODE_INPUTS = (
    HERE / "_run_p1p2_pair.sh", HERE / "run_eval.py", HERE / "systems.py",
    HERE / "answer.py", HERE / "judge.py", HERE / "cost.py",
    Path(__file__).with_name("oracle_paid_layer_experiment.py"), Path(__file__),
)
FROZEN_INPUTS = (
    AUDIT, LEDGER, HOP1 / "cases.json", HOP1 / "checkpoint.jsonl", HOP1 / "summary.json",
    HOP2 / "cases.json", HOP2 / "checkpoint.jsonl", HOP2 / "summary.json",
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def judge_artifact(cases: dict[str, dict]) -> dict:
    # Decisions were made from only the fields in blind_input.  Old booleans,
    # taxonomy labels, gate evidence and retrieval are deliberately not copied.
    decisions = {
        "sw_023|hop1|approval_authority": {
            "before": ("yes", "姓名与参考一致；职位前缀不改变“谁有权限”的实体答案。"),
            "after": ("yes", "姓名与参考一致；省略 VP 前缀不改变所指人员。"),
        },
        "sw_026|hop1|project_structure": {
            "before": ("yes", "目录结构值完整，省略 monorepo 类型词不改变核心结构。"),
            "after": ("yes", "三个目录均与参考一致，省略 layered 类型词不改变核心结构。"),
        },
        "sw_038|hop1|project_structure": {
            "before": ("yes", "目录值完整，省略 modular 类型词不影响核心结构。"),
            "after": ("no", "回答仍给出旧的 /modules 与 /shared，和参考的新结构冲突。"),
        },
    }
    rows = []
    for case_id in JUDGE_CASES:
        case = cases[case_id]
        blind_input = {
            "before": {
                "question": case["before_question"], "gold_rubric": case["before_gold"],
                "answer": case["before_answer"],
            },
            "after": {
                "question": case["after_question"], "gold_rubric": case["gold_answer"],
                "answer": case["on_answer"],
            },
        }
        before, after = decisions[case_id]["before"], decisions[case_id]["after"]
        rows.append({
            "case_id": case_id,
            "blind_input": blind_input,
            "blind_input_sha256": canonical_sha256(blind_input),
            "decision": {
                "before": {"verdict": before[0], "rationale": before[1]},
                "after": {"verdict": after[0], "rationale": after[1]},
            },
            "final_recovered_by_scoring_correction": before[0] == "yes" and after[0] == "yes",
        })
    payload = {"schema_version": "meme-oracle-judge-blind-v1", "immutable": True,
               "method": "offline blind AI-assisted semantic adjudication; no model/API call",
               "rubric": "yes/no/ambiguous; old verdict and failure class hidden", "cases": rows}
    payload["blind_decisions_canonical_sha256"] = canonical_sha256(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    lock = RunLock(args.output / "run.lock")
    lock.acquire()
    try:
        before = {str(path): sha256_file(path) for path in FROZEN_INPUTS}
        write_json(args.output / "input_hashes_before.json", before)
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        audit = json.loads(AUDIT.read_text(encoding="utf-8"))
        cases = {row["case_id"]: row["evidence"] for row in audit}
        groups = exact_case_manifest(ledger)
        write_json(args.output / "case_manifest.json", groups)

        # Exhaustive repository scan found no immutable historical node snapshot,
        # SQL dump, DB dump, or node-content checkpoint for these old runs.  The
        # mutable current DB is intentionally not consulted.
        retrieval = build_retrieval_manifest(cases, frozen_nodes={})
        retrieval["source_inventory"] = {
            "accepted_sources": [],
            "rejected_source": "current mutable database",
            "searched_classes": ["old DB snapshot", "node/evidence artifact", "raw session",
                                 "old checkpoint", "URI materialization"],
            "result": "no immutable historical node-content source found",
        }
        retrieval["manifest_canonical_sha256"] = canonical_sha256(retrieval)
        write_json(args.output / "retrieval_oracle_manifest.json", retrieval)

        config = {
            "schema_version": "meme-paid-oracle-run-config-v1",
            "development_diagnostic_only": True,
            "old_run": {"hop1": str(HOP1), "hop2": str(HOP2)},
            "model_roles_observed": {
                "answer": "gpt-4.1-mini", "judge": None,
                "provider": "openlux", "p2_cheap": "gpt-4o-mini",
                "p2_verify": "gpt-4.1-mini", "extract": "gpt-4.1-mini",
                "cascade_cheap": "gpt-4o-mini", "cascade_strong": "gpt-4.1-mini",
            },
            "answer_parameters_observed_from_source": {"max_tokens": 50, "top_k": 8,
                                                       "include_stale": False},
            "historical_config_binding_complete": False,
            "config_blocker": (
                "old run directory has no immutable run config/provider snapshot/prompt hash; "
                "the command survives only in a mutable shell script and current source"
            ),
            "code_hashes": {str(path): sha256_file(path) for path in CODE_INPUTS},
            "frozen_input_hashes": before,
            "decision_ledger_sha256": sha256_file(LEDGER),
            "oracle_manifest_sha256": retrieval["manifest_canonical_sha256"],
            "price_table_source": str(HERE / "metrics.py"),
            "price_code_sha256": sha256_file(HERE / "metrics.py"),
            "paid_calls_authorized_by_preflight": False,
        }
        config["run_config_canonical_sha256"] = canonical_sha256(config)
        write_json(args.output / "run_config.json", config)

        judge = judge_artifact(cases)
        if JUDGE_OUTPUT.exists():
            raise RuntimeError(f"refusing to overwrite immutable adjudication: {JUDGE_OUTPUT}")
        write_json(JUDGE_OUTPUT, judge)

        gates = stage_gate_summary(retrieval)
        gates["judge"] = {"eligible": 3, "run": 3, "success": 3, "failed": 0,
                          "missing": 0, "cost_incomplete": 0,
                          "final_answer_recovered": sum(
                              row["final_recovered_by_scoring_correction"] for row in judge["cases"]
                          )}
        gates["blocking_reasons"] = {
            "stage1": "Stage 0 recovered no frozen retrieval body",
            "stage2": "no case proven to have sufficient conflict-free original evidence",
            "stage3": "old P1 graph/change event/durable queue snapshot absent",
            "stage4": "old graph/queue/execution state absent; cannot alter verdict alone",
            "stage5": "typed state with validity interval and origin span cannot be built from frozen run",
        }
        write_json(args.output / "stage_summary.json", gates)
        matrix = {
            "schema_version": "meme-oracle-attribution-matrix-v2",
            "does_not_modify": str(LEDGER),
            "necessary_opportunity": {
                "P2 enqueue/execution": list(groups["stage3_p2_execution"]),
                "P2 semantic verdict": list(groups["stage4_p2_verdict"]),
            },
            "measured_sufficient_recovery": {
                "judge/scoring": [
                    row["case_id"] for row in judge["cases"]
                    if row["final_recovered_by_scoring_correction"]
                ],
            },
            "unknown_due_preflight_gate": {
                "retrieval": list(groups["stage0_retrieval"]),
                "answer": list(groups["stage0_retrieval"]),
                "P2 enqueue/execution": list(groups["stage3_p2_execution"]),
                "P2 semantic verdict": list(groups["stage4_p2_verdict"]),
                "state-model": list(groups["stage5_state"]),
            },
            "case_level_recovery_union": [
                row["case_id"] for row in judge["cases"]
                if row["final_recovered_by_scoring_correction"]
            ],
            "combination_oracles_tested": False,
        }
        write_json(args.output / "oracle_attribution_matrix.json", matrix)
        usage = {
            "external_api_calls": 0, "models_called": [], "prompt_tokens": 0,
            "completion_tokens": 0, "actual_cost_usd": 0.0, "retry_count": 0,
            "errors": [], "cost_complete": True,
            "checkpoint": {"completed_cases": 3, "paid_cases": 0,
                           "lock_owner_recorded": True, "stale_recovery_supported": True},
        }
        write_json(args.output / "usage.json", usage)
        after = {str(path): sha256_file(path) for path in FROZEN_INPUTS}
        if after != before:
            raise RuntimeError("frozen input changed during experiment")
        write_json(args.output / "input_hashes_after.json", after)
        outputs = sorted(path for path in args.output.glob("*.json"))
        manifest = {
            "schema_version": "meme-paid-oracle-artifact-manifest-v1",
            "frozen_inputs_byte_identical": before == after,
            "output_hashes": {path.name: sha256_file(path) for path in outputs},
            "judge_artifact": str(JUDGE_OUTPUT),
            "judge_artifact_sha256": sha256_file(JUDGE_OUTPUT),
            "zero_external_api_calls": True,
            "paid_stages_fail_closed": True,
        }
        manifest["canonical_sha256"] = canonical_sha256(manifest)
        write_json(args.output / "manifest.json", manifest)
        print(json.dumps({"output": str(args.output),
                          "manifest_sha256": sha256_file(args.output / "manifest.json")}))
    finally:
        lock.release()


if __name__ == "__main__":
    main()
