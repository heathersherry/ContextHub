# S5：先失效，再封存维护输入

这是显式启用的 core 合同。S5-R1/R2 修订待独立复验；默认 API/SDK/factory 留 S7。没有生成器、规则执行、verifier、guarded commit、恢复 fresh 或三策略运行。下面的 policy 必须由调用者明确提供；测试值不是正式默认。

## 使用入口与事件责任

```python
executor = MaintenanceExecutor(repository, recorder=ExecutionLedger(repository))
# policy: MaintenancePolicy；ctx: 现有 RequestContext
async with repository.session(ctx.account_id) as db:
    watches = await executor.register_graph(db, sealed_report_ref, policy, ctx)
# 登记提交后，再通过现有 ContextStore/ContextService 修改来源。
# 原有 PropagationEngine 显式注入 maintenance_executor=executor。
# 实际事件消费调用 handle_event；也可显式调用以下准备入口：
frames = await executor.begin_maintenance(event_id, watch_ref)
receipt = await EvidenceBuilder(executor).build_evidence(frames[0], "M1")
```

必须在来源修改前登记图。`register_graph` 只接受实际封存 DiscoveryReport 引用，复用 `read_graph_handoff`、`configuration_payload()`；新 canonical-json-text-v1 和旧配置表示均保持 S4 校验。登记时锁来源行并核对其仍是图依据版本；与来源更新事务串行，过期登记拒绝。不会用 latest 重写图，也不查询 gold/scorer/题型/hop。

`MaintenanceWatch` 是既有 knowledge_artifacts 中的 opt-in 记录：绑定 report/graph ID/hash、准确来源版本、全部载体种子、已知影响集合、policy JSON/hash、读取主体。它缓存经过图遍历得到的保护集合，没有另建边表、内容库、事件系统。同图同来源同 ID 不允许改写 policy；新登记身份须使用新封存报告，不覆盖旧登记。

NodeMapping 仅表示来源载体；确认边按 upstream→downstream 遍历。原 source context/version 若本身为图节点，同样是种子。同一来源的所有命题载体都受保护。pending 的可达端点及其确认后继只进入保守保护集合，不升级为确认边；pending 报告始终阻止 ready。未知漏路不能凭 labels 补出，本合同不认证 P1 未交付的真实目标。

## 事务与读取时刻

011 没有新增表，只增加 watch 来源索引和两个 change_events INSERT 触发器：

1. `zz_s5_bind_event` 对已登记来源的 `modified` 事件绑定不可变 watch 引用。
2. `s5_protect_event` 在同一来源写入/事件事务中，保护 watch 的已知集合，再复用 dependencies/context_relations 的结构闭包；只更新该 account 的对象。登记现有 context_invalidations，并保存 `s5_barrier` trace。无模型、无规则执行。
3. **提交一旦可见，全部这些目标已被读取屏障保护。** worker 尚未启动、只初始化一部分、初始化异常、后续证据尚未完成，均不撤销保护。事件事务回滚时，上游修改/事件/保护一起回滚。
4. worker 先再次读取封存图、验证整个已知集合已非 fresh，再逐目标事务封存快照和工作记录。每目标初始化有显式时限；事务不跨整张图或后续动作。必读清单审计单独提交，后续来源封存失败也不会擦掉这份清单。
5. 证据每次尝试使用独立短事务。重试 backoff 不持有数据库事务；最终 receipt/bundle 封存另一个事务，前后复查版本/原因/权限。

新事件只能由 S5 或旧引擎之一处理。事件含 `s5_watches` 时，PropagationEngine 永远不进入旧规则分支；未注入 executor 时明确停止，不能回退旧规则。`delivery_status=succeeded` 仅表示事件初始化已交付，不是维护成功、更不是知识 fresh。旧未登记事件仍走原路径。

逻辑 stale 使用已有 status/validity_status 和未解决 cause：仅原 active/fresh 转 stale；invalid/superseded/recomputing/unknown 等不降格、不复活。已有原因集合继续保存。正常 ContextStore.read/ls 和 RetrievalService 最终物化同时检查未解决原因；显式 debug read 加非 fresh 标记，检索 debug 保留实际 validity。stat 增加兼容的 validity_status 字段。LifecycleService 不再把已有 invalid 降为 stale，依赖原因仍不能通过读访问恢复。

## 一次事件与逐目标快照

一次事件得到 N 个 `MaintenanceFrame` 引用。每个 frame 组合原样的 S2 `MaintenanceSnapshot` 和 S5 准备信息，二者必须一起交 S6：

- S2 snapshot：target 旧版本、图 ref、图上原始上游版本、cause IDs、allowed source refs、cutoff、policy hash；保留 S2 原格式/hash。
- S5 frame：event/root/execution/watch、变化 old/new、`expected_upstreams`、独立 required 清单、完整历史槽位及其当前预期版本、输入注册集合、关系 provenance、截止点和 deadline。
- **图上的版本是历史拓扑依据。** 当前动作的预期版本由 frame 显式记录：仅本次 root old→new 的直接来源上游替换为 new；命题载体不会自动成为 new/fresh。其他图/来源版本漂移停止，不以 latest 偷换。
- target 与图版本不符、事件不是确切 old→old+1，初始化停止并保留屏障和原因。其余版本/cause/provenance/输入登记/权限变化由 `recheck(db, frame)` 返回原因，不能 ready。已有 snapshot 不修改；重新准备须调用 begin_maintenance 创建新 ID，并保留旧记录。
- `recheck` 是 S6 再取快照和提交前检查的入口之一，**不是 guarded commit**。S6 仍须在实际提交事务重新检查并核验，不得把一次 ready 当提交能力。

WorkState 只使用 pending/unresolved；工作状态从不授权 fresh。必要上游或变化后来源 stale/unresolved 时，可收集明确标记的历史/审计证据，但 receipt 不 ready。S5 不自动等待、不无限轮询；可显式 recheck，始终受冻结 deadline 限制。S6 的调度与新快照恢复路径尚未实现。

## 必读清单、登记输入与证据

原 S2 EvidenceRef 必须有真实 excerpt/hash，无法表示尚不存在的来源。S5 新增 `RequiredInput`，只声明 context/version、内容层、位置、role 和规则身份；它不是可用证据。`MaintenanceFrame.required`、`s5_required_manifest` trace 和 `EvidenceReceipt.required` 都在读取结果之外保留这份清单，不伪造 EvidenceRef。

M1 包括 before/after、target、确认的直接 upstream、mapping/edge 的准确 provenance 位置、当前来源、现有 context_relations、全部已登记 conflict/alternative/rule 输入。规则通过 `RuleInputProvider(id, version, required_inputs)` → `register_rule_inputs` 登记；这里只准备输入，没有 execute 方法。其他已知相关项通过不可变 InputRegistration 登记；未解决 conflict IDs 阻止 ready。没有从文本猜出未登记冲突或新增研究规则。

已知项被局部选择遗漏时，`local_manifest_omits_known_source` 保留清单且不 ready，为 S6 判断扩大 M2 提供结构化原因。明确缺失/无权限、hash/span/version 损坏、持续读取错误是硬停止，不能靠强模型绕过。来源权限按同一个 RequestContext/ACLService 核对；整段原文受字段掩码限制时不允许作为未掩码证据，必需项因此缺失会停止。

`build_evidence` 的兼容调整：返回 **EvidenceReceipt**，其中 `bundle` 为 S2 EvidenceBundle 封存引用。缺项或失败时保留 required、已读槽位/页/块及错误，bundle=null；没有伪造一个 required=成功项 的完整包。只有全部槽位实际读完，才构造并封存合法 S2 ReadEntry/EvidenceRef 包。`complete` 与 `ready` 分开：上游未决时可以 complete=true、ready=false。无完整 bundle 就不能交 M3。

## 全历史、分页与重试

M2 在同一个 frame 上使用全部已准入历史，不按相关性/任务/hop/gold 过滤，包含 filler、空正文以及指定内容层的历史版本。准备时用一个 SQL statement 取得已实际可见的版本集合，固定 `context_versions.created_at <= cutoff_at`、context/version/content_level/hash/length，并按 `(created_at, context_id, version, content_level)` 排序、分配从零开始的 ordinal。完整清单就是逻辑摄入边界；晚提交或之后新增的版本即使时间回填到过去，也不会自动进入本 frame。原文位置按具体内容层的 Unicode `[start,end)`，不拿其他层替换。

准备来源 seal 时复用 S2 的实际 hash 校验，会产生本地正文读取成本；这不算 build 阶段的 read 回执，也不宣称 M1 已实现全历史读取成本节省。建图原始来源 hash 另外核对，不能把同版本损坏正文重新 snapshot 成“合法旧依据”。

每次 build 真正重新读取精确来源：先核 seal、frame membership/ordinal/time/version/content_level，再核 ACL，最后读正文和比对 hash/长度/span。恢复重读走同一准入函数。每页 page_size 个 required 槽位，每项按 chunk_size 个 Unicode code point 分块；页号/槽位/分块序列必须精确一致，重复、乱序、丢页/块均不能 complete。普通下一页只表示继续，完整包没有 next_cursor/truncated/issues。空正文实际读取后可完成其槽位，不制造空 EvidenceRef。

`reuse_m2_for_m3(receipt_ref)` 只返回 **同一个** M2 bundle SealRef，不读历史、不重新分块；metadata/hash 会重读校验。此方法只交接输入，不执行模型，也不授予 ready/fresh。complete 只证明声明范围完成，不证明现实中没有其他事实。

RetryPolicy 沿用 S2 的显式 max_attempts/timeout/backoff；本地错误表仅允许 connection/timeout/serialization。同一 source logical call 在 ExecutionLedger 中按 attempt_no 记录，最多三次，无外层再次重试；确定缺失、权限、版本/hash/span 错误不重试。初始化失败本身停止，不由 S5 叠加重试。新一次显式准备/构包是新工作身份、旧失败保留。

## 日志、回退和限制

复用 execution_attempts、maintenance_work_items、propagation_trace。记录 watch/report/graph/frame/snapshot/bundle ID/hash、execution/event/root/account、old/new、target/upstream/cause、affected、屏障时间、required/allowed/read、页/块、complete/ready、读取尝试/错误/停止码、配置/输入输出 hash、真实时间和本地读取/校验/写入工作。账本只存错误分类，不存异常原文或密钥；无模型请求时 usage/价格仍是 missing/unknown，不记零成本。S2 缓存计价限制未改变。

011→010 只移除 S5 触发器/索引，不删除已有原因、内容、产物或账本，不恢复 fresh。回退前应停止对已登记来源的新写入及新 S5 工作；否则旧代码不会为后续新事件执行新屏障。历史封存对象保留；回退旧代码后不认识 S5 kind，不能改 hash 或覆盖产物来兼容。001–010 文件、S1 冻结产物和旧 S4 报告格式未修改。

验收优先看独立连接的提交窗口、部分初始化失败、pending 保守保护、cause/权限/版本漂移、缺失 required、历史边界/原文层级、合法重试恢复、M3 原引用、普通角色 RLS 和旧入口。该阶段不证明真实模型质量、正式 P1 路径覆盖或生产吞吐/高可用；S6–S8 和模型实验仍未开始。


## S5-R1/R2 修订合同（2026-09-23）

独立验收发现的两个阻断只在本节范围修订；不改变研究协议、S2/S4 封存格式、canonical hash、011 迁移或阶段边界。

- **R1：完成进度必须绑定实际包。** `ArtifactService.validate_maintenance_receipt(db, receipt)` 从封存 frame/watch 重新取得 required/history 和分页分块配置。`validate_receipt_bundle` 独立展开每个 required 槽位应有的来源 seal（绑定 ID/version/hash/内容层）、span、role、全局 ordinal、page 和项内 chunk，并逐项检查 bundle.required 与 bundle.read。也检查 maintenance/scope、ordering/page_size/chunk_size、complete/truncated/issues/next_cursor。两份同样受损的列表相等，不能证明完整。原 S2 seal 仍负责正文、excerpt/hash 与来源的有效性校验。
- **R1：封存与使用共用校验。** receipt 封存时执行上述检查；失败会回滚同一事务中的 bundle/receipt，保留维护快照、stale 屏障和失败账本，不自动重试校验错误。修复受控故障后，在版本/原因/权限/时限仍合法时可重新构包。`reuse_m2_for_m3` 对已持久化的 receipt 再执行同一检查，拒绝修订前漏检的坏包；合法旧包也可复用完全相同的引用，无正文重读或重分块。`ArtifactService.get` 仍是 hash/身份校验的历史对象读取，不单独认证跨对象完整性；旧失败产物可审计保留，不改其 complete 字段或 hash。未来消费者须走验证后的交接，不把历史字段当当前放行凭据。
- **R2：正文与状态同一 SQL 快照。** L2 物化 SELECT 同时返回 version、正文、status、validity_status 和未解决原因。精确版本匹配后，将这些状态应用到响应，不能沿用前一次候选核查的 fresh。debug 可读旧正文，但开放原因使 fresh 显示为 stale；invalid/superseded 等已有状态保留。普通读取仍使用原 active/fresh/无原因过滤，无关内容仍可读。此修订不增加长期锁或永久全局屏蔽。

回退只需撤销本轮七文件差异，无 schema downgrade。回退会重新暴露两个已复现阻断，不能据此接 S6。复验入口为研究仓 S5-implementation §7；S5-review 由独立验收方维护，本修订不代其关闭问题。
