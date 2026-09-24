# S6：候选、独立核验和安全提交

2026-09-24。显式 core 能力，S6-R1/R2 修订待独立复验。未装配默认 API/SDK（S7），没有 S8 runner/统计或真实模型效果认证。S2–S5 文档中的旧阶段状态保持历史原文；当前前置为 S5-review §6.1 独立通过及代码 929ba7b。

## 入口和配置

复用 S5 `register_graph` → 正常来源修改/事件事务 → `begin_maintenance`。必须先有提交可见的全目标 stale 屏障。S6 主入口为 `MaintenanceRunner(executor, config, base=..., strong=..., verifier=..., rules=...).run(frame_ref)`。不接受 benchmark/评分对象，不 import 实验模块。

构造方式：

```python
checks = HardChecks(executor, rules)
base = CandidateGenerator(base_config, api_key=key, recorder=executor.recorder)
strong = CandidateGenerator(strong_config, api_key=key, recorder=executor.recorder)
verifier = FixedVerifier(verifier_config, checks=checks,
                         api_key=key, recorder=executor.recorder)
runner = MaintenanceRunner(executor, profile_config,
                           base=base, strong=strong, verifier=verifier, rules=rules)
state = await runner.run(frame_ref)
# 调用方 finally 中分别 await base.close()/strong.close()/verifier.close()。
```

以上只描述装配，不提供正式配置或授权发请求。所有模型、endpoint、prompt_template（唯一 `{input_json}`）、解码、max_tokens、规则版本、RetryPolicy、价格（可显式 null）、动作和规则 timeout 均显式提供。ProfileConfig 是严格 DTO，可用 `model_validate_json` 加载；没有正式默认模型、prompt、规则库、价格或 lambda。API key 不进入配置/hash/日志。fixture 的参数仅为合成测试。

`PlannerConfig` 明确 lambda 的 `targets/currency` 单位、currency、成本估计算法/版本与价格 hash。当前动作仅提交一个目标，因此支持显式 `single-target-count-v1`：U_max=1；不是成功概率，也不是把所有后继恢复数计入本动作。M2/M3 先满足程序准备条件，再计算 `1-lambda_p2*estimated_cost>=0`，等号执行。未知估计成本返回 cost_unknown，不能补零。M1 和两个固定对照不经过自适应价格门。

## S5、状态和输入绑定

`ready/complete` 从不授予提交权限。每次动作、核验前和提交事务内均重读 frame/snapshot/receipt/bundle/work，调用 S5 `validate_maintenance_receipt` 从冻结清单独立检查实际页块布局，再核当前版本、上游、原因、权限、关系、登记和 deadline。M3 先经过 `reuse_m2_for_m3`，再做同一准备检查；不重新搜索或分块。

工作状态：pending 经 CAS 领取为 running；成功只在 guarded commit 内成为 updated/unchanged；停止成为 unresolved。普通 `ArtifactService.update_work` 仍禁止成功 proof，终态不能复活。S6 用 event/target 的事务 advisory lock 串行登记领取，禁止新准备身份重播已用菜单；同 event/target 不能延长最早 frame 的 deadline。合法等待后的有限接续另有明确 lineage。

局部选择漏掉已知来源时，M1 不生成，adaptive 可考虑 M2。已知必需来源缺失/不可访问、上游未决、已登记未解决冲突、漂移或超时直接停止。没有候选、确定 schema 错误、候选输出引用不在包内及语义 FAIL/UNKNOWN 属于本级未通过，可按有限菜单考虑下一动作。verifier 无合法结果（包括临时错误耗尽、解析错误）直接停止，不借升级生成器绕开。

## 候选和规则

保留 S2 Candidate 原 schema/hash，新建组合封存记录：

| 记录 | 内容 |
| --- | --- |
| maintenance_action / ActionBinding | frame/receipt/snapshot/bundle、execution、工作 revision、profile/action/config hash、原目标完整内容、原因 ID/准确来源版本/reason hash |
| maintenance_proposal / Proposal | S2 candidate 引用、action 引用、完整 ProposalData、model/rule 身份及规则输入 hash |
| maintenance_verdict / Verdict | proposal、S2 hard_check/verification 引用、三项判断的理由和引用、verifier config hash |
| maintenance_continuation / Continuation | 旧 frame、新 frame、实际成功上游证明 |

ProposalData 显式给 l0/l1/l2（包括 null/空串），每个非空层由连续、不重叠的 Claim span 覆盖全文，每项有完整原文、证据和推导说明。S2 `proposed_content` 保存这三个字段的确定 JSON 文本，硬检查与实际写入逐字段绑定；不能核验一段文字后另写其他层。

unchanged 要求与原三层内容逐字一致，但一致本身不放行：仍要求全部内容结论和各原因的当前来源支持，经过同一独立 verifier。只引用目标旧内容或变化前证据不足以形成放行候选。updated 需改变现有 semantic_identity，避免绕过旧底座的同语义拒绝。

规则实现 `DeterministicRule`：id/version、`required_inputs(target)`、`applies(target, original)`、`execute(original, frozen_inputs, causes)`。先用 S5 `register_rule_inputs` 登记；缺失/错版本/漏读不能执行。按传入固定顺序选第一个适用规则，没有命中转语义菜单。execute 在期限内对同一不可变输入运行两次并比较结果，记录两次本地检查；不一致停止。规则必须由部署者提供无模型、无外部副作用的确定函数；重复一致不认证任意第三方函数永远确定。无正式规则库。

规则在无数据库句柄的线程中运行，超时丢弃结果；Python 不能强杀线程，因此接口要求无副作用。规则没有提交权，只能输出 GenerationResponse（proposal 或明确 unresolved）。规则实际执行使用本地 call 身份，usage/request/model 明确缺失，不伪造模型 token。候选仍执行硬检查、固定 verifier 和 guarded commit。

## 程序检查与固定 verifier

硬检查产生原 S2 HardCheckResult，版本 `s6-hard-checks-v1`，必需项目精确为 binding、manifest、current_state、sources、conclusions、causes、rule。缺项、FAIL/UNKNOWN 均不可提交。来源通过原 EvidenceService 核真实 ID/version/hash/层/span/ACL；使用的当前证据不得是 stale 中间值或未解决原因来源，历史比较证据明确区分。冲突登记在 S5 frame 中持续阻塞，不接受模型自报清除。

verifier 使用独立 RecordedCall，输入白名单仅为候选 ProposalData、冻结 bundle、允许来源/版本与当前适用元数据、硬检查记录。没有策略档位、生成器自评、gold、问题或标签。三种 profile、M1/M2/M3 共享同一个 FixedVerifier 配置和 schema，独立 call ID 与 generator 不相同。

三项分别为 applicability、complete_support、conflicts_resolved。每项 PASS/FAIL/UNKNOWN、理由、证据引用、claim_indexes、cause_ids 都严格解析。引用必须来自本包；PASS 必须有可核对引用；complete_support PASS 覆盖全部结论，conflicts_resolved PASS 覆盖全部待解决原因。不能用综合分数抵消失败。

合法三 PASS 生成一次性的进程内提交许可，绑定该 verdict/proposal/check/action。`GuardedCommit.commit(verdict_ref)` 消费许可并再次检查全部条件。普通封存的 PASS、复制 verdict、归档 proof 或旧证明无法生成许可。许可不是持久恢复队列；进程丢失许可时保留 non-fresh，不承诺崩溃恢复。

## guarded commit 的真实事务边界

生成、verifier HTTP 和 backoff 都在数据库事务外。提交另开短事务，按固定顺序对 contexts、context_versions、context_invalidations、context_relations、dependencies、access_policies、teams、team_memberships、knowledge_artifacts、maintenance_work_items、change_events 取得 SHARE ROW EXCLUSIVE 锁，之后重新执行程序检查，再写入。

选择表锁的原因：既有手动更新、旧系统更新、生命周期、ACL 和 011 原因插入路径没有统一 advisory-lock 协议。表锁与它们的普通 DML 冲突，也保护原因/登记/ACL 的新增行，避免只锁已有行遗漏 phantom。检查时的 READ COMMITTED 语句读取锁取得后最新已提交状态。先完成的外部变更导致拒绝；在锁之后开始的变更等待本次提交，随后正常安装自己的屏障。锁等待同样受 deadline 约束，最后再以数据库 clock_timestamp 核期限。死锁/写入错误不自动重试，回滚并保留 non-fresh。

这是偏保守的全表短锁，会串行其他租户写入；不代表生产吞吐、低延迟或高可用设计。没有要求既有写入路径采用新的锁协议，也没有放松旧读取/手动更新条件。

updated 调用 `ContextService.apply_system_content_change`：真实内容版本+1、原 semantic identity、context_versions trigger、modified event、011 下游保护全部复用。unchanged 只由内部 `_apply_verified_unchanged` 原语在已持锁和核验的提交路径使用：原内容/semantic identity/版本不变，保存证明和工作终态，不制造 modified event；普通读取使用 contexts 的当前状态，旧 context_versions 是历史内容快照。

原因必须逐项有 CauseSupport、准确来源版本和 verifier 依据。当前仅处理 S5 `s5:source_changed` 原因，且属于本次 root event 或同一 execution 中实际上游提交事件；其他原因不因“集合没变”而清除。提交前原因集合和 source_version/reason hash 必须与绑定一致。所有原因均有依据解除后才能 fresh；新增或未知原因阻塞。

proof、工作 revision CAS、内容/原因/状态在一个事务中保存。GuardedProof 使用 **proof schema v2**，明确保存实际 expected_upstreams（区别于 S2 snapshot 内历史图版本）、frame/receipt/proposal/verdict、execution、原因记录、工作 revision 和 updated 的真实 commit_event_id。旧 v1 proof 仍按原规则归档，authority 始终 audit_only。

无需迁移：010 的 context_refresh_proofs 没有 schema_version 列约束，v2 只存在这张既有 JSON proof 表；四种新组合记录各自 schema v1，保存于原 knowledge_artifacts。001–011 完全不变，不改旧封存 payload/hash。回退旧代码前停止 S6 工作，保留新产物/账本；旧代码不理解 proof v2，不应重写它。

业务回滚后另写拒绝 trace/工作 unresolved，保存预期和实际版本、原因及分类错误。ExecutionLedger 独立事务保存实际调用/失败成本，因此不随业务回滚消失。提交后账本写入故障不谎报为已回滚，数据库中的 proof/工作终态是提交事实；不承诺数据库不可用时仍能补全所有日志。

## 三种策略和有限接续

共同顺序：全部目标 stale（S5）→ 必要上游检查 → 同一规则步骤 → 语义菜单。local/base-only 只有 M1；full/strong-only 直接 M2 定义的全历史和同一个 strong；adaptive 依次 M1/base、M2/同 base、M3/同 M2 bundle＋strong，成功立即退出。

`executor.continue_after_commits(old_frame_ref, proof_refs)` 仅用于等待上游、尚未进行生成/核验的下游工作。每个原 frame 最多一次替换，新 frame 不能再滚动替换。proof 必须被真实 updated/unchanged 工作引用、同 execution/root/watch、来自原图祖先；其目标当前仍是证明中的准确 fresh 版本且无原因。普通归档证明不够。

检查旧 history 中全部当前版本，只有这些已证实祖先提交可解释变化；新增原因仅允许真实上游提交事件，新增历史仅允许其准确新内容版本。其他外部变化拒绝，不偷选 latest。复用 S5 `_prepare` 建立新的 snapshot/frame/required/source seals，记录 Continuation，历史图/hash 和旧 frame 原样保留。旧工作停在 unresolved；新工作有新 snapshot ID，但沿用原 execution 和不延长的原 deadline。

此入口支持合成小型一跳/两跳前沿由调用者显式安排。没有自动无限等待、深链/DAG 调度、生产恢复或默认 worker 装配。上游未决时下游不生成；模型动作已经耗尽的旧工作不能借接续重跑。

## 日志、验证和未证明事项

S2 ExecutionLedger/RecordedCall 是唯一模型重试所有者：同输入最多 3 次，确定 schema/parse/语义失败不重试。外层 asyncio 期限包含调用、重试/backoff、本地规则、读取和提交；等于 deadline 即到期。共享 verifier 错误耗尽不升级生成器。

逐 attempt 保留 call/config/prompt/input/output、usage/价格状态、时间/错误及本地工作。S6 action/checked/verified/decision/started/stopped/commit/continuation trace 连接 account/event/root/execution、frame/snapshot/receipt/bundle/proposal/check/verdict/proof、预期实际版本和原因、价格计算。S5 读取沿用其原账本，通过 execution/work/frame 与 S6 profile 关联，不重复累计。未知费用/usage 不补零，本地规则不冒充模型请求；S2 缓存计价限制保持非阻断，不认证精确实际账单。

验证入口为 `tests/test_maintenance_{candidate,hard_checks,verifier,planner,profiles,rules}.py` 和 `tests/test_integration_maintenance_commit.py`。后者硬性要求本次新建的 S6 普通角色数据库（127.0.0.1:55487，contexthub_s2_s6），不回退默认 db_pool/clean_db。精确命令、版本/hash、各轮结果及失败历史见研究仓 S6-implementation 与本轮证据目录。

本阶段只证明合成数据和 HTTP 替身下程序机制，不证明真实模型语义正确、真实 P1 路径覆盖、MEME 答案或成本优势。P0、20/80、294、pl_030 保持；未来任何真实模型/API、smoke、调参或正式实验仍需单独授权与冻结配置。S4-R3 原件损失未恢复。S6 通过也不自动放行 S7/S8 或这些运行。

## 2026-09-24 S6-R1/R2 修订

原交付未通过独立验收：归档 proof 曾被误作额外原因授权，verifier 的数据库工作未完整进入 W_local。原失败及原版本身份保留在研究仓 S6-review；本节仅描述修订，不能代验收方关闭问题。

R1：`require_committed_upstream` 是原因解除和有限接续共同使用的消费检查。只接受真实成功工作所引用的**准确 proof 引用（含 hash）**，检查其终态/revision 与候选结果、准确提交目标版本一致，并绑定同 execution、root event、event、watch、图及原图祖先。updated 的 change_event 还必须匹配目标/version、modified 类型及 s6_proposal/execution/root 元数据；源当前必须是该准确 fresh 版本且无未解原因。硬检查中的非 root 原因先联结实际成功 work 与 proof，再做上述检查；只在归档表存在的副本不能授权。普通 archive 仍只负责审计保存，不被升级成权限接口。

这些条件在 guarded commit 和接续已有的 COMMIT_LOCKS 事务内再次核对，包括 change_events 和 maintenance_work_items 的并发保护；没有新增“事务外最后读一次”。旧 graph/hash、work 终态和原 deadline 不变。真实无关图的成功 proof 也不因同租户/同 execution 而获得跨图授权。

R2：每次 verifier 的本地工作有独立 `verifier-local` CallIdentity，关联实际 frame 的 execution/event/work、profile/action/config。解析调用身份必需的初始封存读取先缓存在本次局部 Counter，再转移到唯一 LocalOperation；不读取全局累计量、不做全局相减。前后两个数据库阶段均使用 MeteredDB，记录真实读取行/逻辑 UTF-8 JSON 字节、硬检查、解析和封存工作。HTTP 重试仍只属于原 RecordedCall，没有增加第二个重试入口。

`writes` 计已执行并返回的 INSERT，包括后来回滚的写入；`audit_records` 计尝试封存的核验记录。FAIL/UNKNOWN、临时错误耗尽以及第一/第二份核验记录封存后异常，均保存本地工作结果。账本在业务事务之外写入，异常代码分类记录，不写敏感异常正文；只有封存事务和本地账本成功完成后才生成一次性提交许可。HTTP attempt 不重复计这些本地工作，外层 action 的 MeteredDB 不包裹子阶段 SQL，因此不会重复累计 W_local。action/verifier-local 的 wall_seconds 是包含子阶段的时长，不能相加冒充端到端 T_wall。

持久回归 `tests/test_integration_s6_revision.py` 覆盖原归档漏洞及真实无关事件、精确读取行/字节和写入对账、updated/unchanged/FAIL/UNKNOWN/transport/封存回滚，以及独立连接在 verifier 返回后改变真实上游事件时的安全拒绝。新测试复用原 S6 专属数据库 fixture；复验脚本只重定向明确的隔离库身份，不接默认库。没有迁移、正式配置或协议变更。

## 2026-09-24 S6-R3 修订（待独立复验）

上节 R2 的初始 Counter 转移方案存在读取失败边界：第三次封存读取失败时，前两次已返回的行/字节没有持久归属。独立验收 §6.1 已记录此问题；本节替代上述初始化方式，保留原失败历史。

`FixedVerifier.verify(proposal_ref, check_ref, *, frame, binding)` 现在要求调用方传入已持有的 frame/action 封存对象。MaintenanceRunner 和直接调用的合同测试均已接通。这是尚未装配默认 API/SDK 的 S6 内部接口补充，没有修改任何 DTO、旧封存 hash 或迁移。身份提示只确定本次本地账本归属；账户/frame 引用先核对，数据库内仍重新读取 proposal/action/frame 并逐对象比对。传入对象不能代替封存读取、硬检查、固定 verifier 或事务提交授权。

唯一 `verifier-local` started 记录在首次业务读取之前通过独立账本事务保存。初始读取、封存解析、身份匹配及后续核验均在同一 try/except BaseException 内，所有业务 SQL 使用同一个 MeteredDB owner；断线/解析失败/取消后，在可用的独立账本连接保存 finished/error 和实际已发生的行数/逻辑 UTF-8 JSON 字节。不重读来重建身份、不重跑生成、不延长 deadline；HTTP attempt 和外层 action 不重复计这些本地指标。账本自身完全不可用或进程被杀后的恢复不在本次承诺中；没有声称实现崩溃恢复。若 started 写入失败，不开始业务读取；finished 未成功，不发放提交许可。

回归 `tests/test_integration_s6_bootstrap.py` 覆盖第一至第四次读取、前三次解析、初始化超时、同账户错误 action 身份以及普通角色独立物理连接终止 verifier backend。真实 S4→S5→S6 路径中，失败仍 unresolved/stale、原内容 v1、原因保留、零 verifier HTTP/零升级/零 proof；同时重跑合法 updated/unchanged 与既有安全合同。准确版本、执行证据、旧版反证及复验入口见研究仓 S6-implementation §8。
