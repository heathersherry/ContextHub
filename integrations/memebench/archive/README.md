# archive/ — 已结题实验的封存代码

这里的 **72 个 py 文件**（含 19 个测试）**不在当前方案的任何执行路径上**，也**没有任何仍在跑的
测试引用它们**。它们是**已发表数字的复现代码**，按研究家族分目录封存；每个家族的测试放在
该家族的 `tests/` 子目录下。跨家族共用的模块放 `shared/`。

判据是「活代码 import 闭包 **∪ 测试引用**」。测试这一项不能省 —— 第一轮归档只算了活代码闭包，
结果 17 个测试文件静默失效（详见本文末尾"归档判据的一次修正"）。

## 封存原则：字节不变，要跑先还原

这些文件之间有 **68 条互相 import**，且全部写成绝对路径 `integrations.memebench.X`。
移动后这些 import 解析不到 —— 这是**有意为之**：

> 改 import 让它们在新位置能跑，就意味着改动复现代码的字节，它便不再是当初产出那些数字的那一份。
> 可复现性 > "移完还能直接跑"。

**要重跑某个家族时，先还原到根目录**（在仓库根执行）：

```bash
# 例：还原 same_session_p1_selector 家族
cp integrations/memebench/archive/same_session_p1_selector/*.py integrations/memebench/
cp integrations/memebench/archive/same_session_p1_selector/tests/*.py tests/   # 若要跑其测试

# 例：还原 chronological_p1_certification（注意它有跨家族依赖）
A=integrations/memebench/archive
cp $A/chronological_p1_certification/*.py integrations/memebench/
cp $A/shared/metrics.py $A/stats_synthetic/provenance_simulation.py integrations/memebench/
cp $A/chronological_p1_certification/tests/*.py tests/

# 跑完后删掉根目录/tests 下的副本即可，archive/ 里的原件不动
```

注意两点：

1. 还原会让这些文件出现在根目录，可能影响 `run_full100_v3_p2.py` 的 identity 闭包扫描 ——
   **不要在还原状态下跑正式 run**。
2. 已封存的 `run_chronological_p1p2.py` 里有一张**软判断**指纹表 `SOURCE_FINGERPRINT_FILES`，
   用 `path.exists()` 读字节：文件缺失时不报错、指纹静默改变。所以该家族的指纹只在
   **完整还原后**才与当初一致。

---

## 各家族对应的结果文档

结果文档路径均相对于 `ContextHub-research-plan/research/proposal/`。

### `same_session_p1_selector/` — 20 个 + 11 个测试（2026-08-23 ~ 08-24）

P1 same-session turn 候选机制 → selector 干预 → full100 两臂。
`gold_edge_audit.py` 被本家族 24 处 import，是这棵子树的树根（主运行器从未引用）。

- `experiments/p1/same-session/p1-same-session-turn-candidate-mechanism-experiment.md`
- `experiments/p1/same-session/p1-same-session-selector-intervention-experiment.md`
- `experiments/p1/same-session/p1-same-session-manual-validation.md`
- `experiments/p1/same-session/full100/p1-same-session-selector-full100-{cheap,verify,comparison}.md`
- `experiments/p1/diagnostics/p1-gold-edge-audit-results.md`

关键结论：full100 verify 2209/2209；hop1 recall 90.39% / hop2 93.49%。

### `full100_endpoint/` — 5 个 + 2 个测试（2026-08-24）

full100 endpoint 反事实、selector 重跑、候选 envelope 重评、v3 冻结盲审。

- `experiments/p1/full100-v3/p1-full100-offline-endpoint-adjudication-counterfactual.md`
- `experiments/p1/full100-v3/p1-full100-endpoint-selector-rerun.md`
- `experiments/p1/full100-v3/p1-full100-candidate-envelope-development-reevaluation.md`
- `experiments/p1/full100-v3/p1-full100-v3-blind-precision-review.md`

关键结论：真实 miss 6/100，6/6 恢复；候选膨胀 1.2717×。

### `oracle_layer/` — 4 个 + 2 个测试（2026-08-25）

旧 E2E 逐层 oracle 审计 + fail-closed 最小付费实验。

- `experiments/oracle/meme-legacy-e2e-oracle-layer-audit.md`
- `experiments/oracle/meme-legacy-e2e-minimum-paid-oracle-results.md`

关键结论：Stage0 历史正文 0/88，付费臂全 fail-closed。

### `p1_build_sweeps/` — 13 个（2026-08-10 前后，含第二轮加入的 `judge_routing_sweep.py`）

P1 建图侧阶段一/二：级联 τ 扫描、λ 前沿、候选筛选、抽取模型、负边集、B1 诊断。
含驱动脚本 `_run_p1p2_pair.sh`（8/11 的两强档 × 两 hop 串行跑）。
`judge_routing_sweep.py` 是第二轮加入的：`planned_propagation` 曾借它的 `judge_j1`，
该符号现已在 `../../common.py`。

- proposal 附录 E.6 / E.7（τ=0.40 甜点、λ 曲线）
- B1 归因见记忆 `contexthub-b1-root-cause`（三个归因全否证）

### `stats_synthetic/` — 3 个（2026-08-19 ~ 08-20）

`cluster_inference`（2×2 聚类推断）、`cascade_order_cost`（cheap-first 顺序成本）、
`provenance_simulation`（合成 provenance DAG，与 MEME 无关）。

> `synthetic_planner_eval.py` 原本也在这里，**已还原回根目录** ——
> 它被覆盖活模块的测试引用着。见文末"归档判据的一次修正"。

- 记忆 `cluster-aware-2x2-and-cascade-order`、`judge-tiers-not-independent-not-nested`

关键结论：两档判定集**不嵌套**、漏判强正相关（OR 19–35）⇒ 不能用单调族 RCPS；
cheap-first break-even 价格比仅 1.8%。

⚠️ `cluster_inference.py` **不是** δ 的 bootstrap 机器
（WORKING-NOTES 第 890 行的说法有误，详见根目录 README 第六节）。

### `smoke_probes/` — 3 个（2026-07 ~ 08）

早期单例 go/no-go smoke 与一次性探针（`_probe_cand` 查变量 4 的 recency 档）。

### `chronological_p1_certification/` — 4 个模块 + 4 个测试（2026-09-04 / 09-07 两轮）

**09-04 第一批**（抽 `common.py` 断尸体依赖后）：

- `run_eval.py` —— 旧 OFF/ON 配对主 runner，已被 `run_full100_v3_p2` 取代
- `p1_policy_certification.py` —— P1 策略认证，实验结论 No-Go
- `tests/test_memebench_p1_certification.py`

**09-07 第二批**（拆掉混合测试文件后）：

- `run_chronological_p1p2.py` —— 时间顺序 P1+P2 runner
- `chronological_ingest.py` —— 会话回放式入库（唯一活引用者就是上面那个 runner）
- `tests/test_memebench_chronological_runner.py`
- `tests/test_memebench_chronological_ingest.py`
- `tests/test_memebench_chronological_runner_scaffold.py` —— **09-07 新建**：
  从 `test_memebench_planned_propagation.py` 剪出的两个 runner 脚手架测试
  （`test_g0_g3_smoke_records_and_both_contracts`、`test_runner_requires_explicit_models`），
  测试体逐字节复制，文件头记录了拆前核实过的覆盖归属

它们被借走的符号现在都在 `../../common.py` 里（逐字节搬运）。
参见 `experiments/p1/chronological/p1-p2-meme-chronological-results.md`。

⚠️ **还原时注意跨家族依赖**：本家族的测试需要另外两个目录里的文件 ——
`tests/test_memebench_p1_certification.py` 要 `stats_synthetic/provenance_simulation.py`，
`run_eval.py` 要 `shared/metrics.py`。完整还原命令见下。

**实测**（2026-09-07）：还原本家族 4 个模块 + `shared/metrics.py` +
`stats_synthetic/provenance_simulation.py` 后，4 个测试文件 **36 测全过**。

### `shared/` — 1 个（2026-09-07）

跨家族共用、放进任一家族都会破坏另一家族还原完整性的模块。

- `metrics.py` —— 汇总对比表与成本。两个用户分属不同家族：
  `chronological_p1_certification/run_eval.py` 与 `p1_build_sweeps/recompute_cost.py`。
  它不 import 任何 memebench 模块（叶子），所以放这里不引入新的跨家族依赖。

---

## 归档时的安全验证

**第一轮（48 个脚本，2026-09-04）**

1. 无任何活模块 import 归档模块 —— 逐对 grep，零违例；
2. memebench 之外全仓库无引用（含路径字符串）—— 零命中；
3. 归档后 26/26 活模块导入正常；
4. 主运行器 identity 闭包无归档文件混入。

其中 12 个文件是 git 跟踪的（用 `git mv`，有历史），36 个是 untracked（用 `mv`）。

**第二轮（common.py 抽取 + 3 个脚本，2026-09-04）**

1. `common.py` 全部 13 个符号与原文 `inspect.getsource` 逐一比对**完全一致**；
2. 全部活模块导入正常；
3. identity 闭包 81 → 76，四个死文件全部脱离；
4. `pytest --collect-only` 全仓库 **1122 测零收集错误**。

**第三轮（拆混合测试 + 封存 runner/ingest/metrics，2026-09-07）**

1. 拆分前逐条核实两个待剪测试夹带的活断言都另有覆盖（见根目录 README 第四节）；
2. 拆分后原文件 17 测 + 新文件 2 测 = **19 测全过**，原文件零引用死 runner；
3. 封存后活模块 **23/23 导入正常**、identity 闭包仍 76 且零归档文件混入；
4. 活侧受影响测试（planned_propagation / chronological_policy /
   propagation_planner_eval / full100_v3_p2）**126 测全过**；
5. `pytest --collect-only` 全仓库 **1091 测零收集错误**（闸门 4）；
6. 活文件残留归档引用扫描：模块引用与硬编码路径**均为零**；
7. 还原实测：本家族 + 两个跨家族依赖还原后 **36 测全过**，随后清理副本。

## 归档判据的一次修正

第一轮我用的判据是「活代码 import 闭包」，**漏算了测试文件**，后果：

- 17 个测试文件静默 `ModuleNotFoundError`（第一轮归档后我只验证了活模块导入，没跑 pytest）；
- 其中 15 个只覆盖归档代码 ⇒ 已随代码搬进各家族 `tests/`；
- **`synthetic_planner_eval.py` 判错了，已从 archive 还原回根目录** ——
  它被 `tests/test_memebench_propagation_planner_eval.py` 引用，
  而那个测试覆盖的是**活模块** `propagation_planner_eval`（δ 标定）。

教训：判"死"必须同时检查活代码引用**和**测试引用，且归档后必须跑一次
`pytest --collect-only`，仅验证活模块 import 是不够的。