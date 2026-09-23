# S4：依赖裁决、完整计划选择与 RQ1 图交接

本阶段只交付显式装配的核心服务和合成运行。默认 API/SDK、stale-first、传播修复、S8 统计均未实现。所有配置示例来自 tests fixture；没有正式模型、提示词、别名、计划菜单、q/c、价格或 timeout 默认值。

## 装配与输入

公共模块 `contexthub.discovery` 导出 Stage0Config、AdjudicationConfig/Service、DiscoveryConfig/Service、Plan/Item/choose_price/solve_mckp。配置可通过严格 Pydantic `model_validate_json` 加载，未知字段拒绝，完整 canonical hash 写入 CallIdentity 和封存报告。ExtractionConfig 和 SearchPolicy 继续沿用 S3。

```python
# 所有 cfg、api_key、identity 均由调用方显式提供；没有隐式模型调用。
adjudicator = AdjudicationService(cfg.adjudication, api_key=api_key, recorder=ledger)
core = DiscoveryService(cfg, search=SearchService(recorder=ledger),
                        adjudicator=adjudicator, recorder=ledger)
async with repository.session(account_id) as db:
    report = await core.discover(db, shared_extraction_ref, history,
                                identity=identity, prior_extractions=prior_refs)
await adjudicator.close()
```

调用者提交事务后才使用 report.reference()。两臂必须传同一次抽取的 SealRef（ID **和** hash），历史批次也共用 prior_extractions；不能各自抽取后比较文本。`DiscoveryConfig.timeout_seconds` 是本次核心调用总时限，SearchPolicy 与 AdjudicationConfig 的 lambda 必须相同。旧 DependencyDiscoveryService.discover_sources 保留历史行为；新 discover_snapshot 显式且仅委托 S4，不双跑。

## 搜索、证据和确定性规则

先消费所有 cursor，再对整个批次配对，不依赖 context 插入顺序。scope_sources 是待检查槽位，allowed_sources 是已经准入的来源，read_sources 是已经读取的正文。局部 complete 不等于全历史，也不认证图正确；at_max_scope 不等于成功。局部无可比较候选时扩大；裁决提出缺失证据、或实体 SAME 需要扩展候选包时调用 SearchService.expand。该入口保持 artifact/history/policy 绑定、旧 emitted/read 集合和 manifest 边界；范围严格增加，最大范围停止。新证据生成后整轮重新判断，历史 trace 保留，不拼接两代弱结论。

SearchService.read_evidence 仅读取当前已读清单，重新执行 S3 `_admit_source`。一次元数据异常允许一次直接恢复检查；恢复时重新验证租户、manifest、版本、逻辑序号和时间，失败从不授予正文权限。合法恢复的错误码在 evidence_recoveries 保留；持续失败为 pending/运行错误，不伪 complete。S3-R1 原路径和此新增路径均有合成/PG 回归。

Stage0Config 显式提供规则 ID/version、带 left/right 命名组的固定句式、版本化带 scope 的别名表和 mention 定位表达式。只允许明确别名、同 canonical ID、明确不相容类型、明确不同身份；没有内置别名内容或 NER 判定。Mention 绑定原文坐标，重复裸词无法确定位置时弃权；alias 必须有原文支持的 scope。rule hit 保存精确匹配 span/source seal，别名 hit 保存 alias version/canonical ID。冲突和歧义为 UNRESOLVED。

规则属于可配置的可信语义声明，调用者必须确保“类型不相容”等句式确实表达对应含义；本次不认证任何正式规则表质量。SAME 只扩大候选，不合并 ID。实体 DIFFERENT 不删除事实依赖候选或 provenance；事实关系仍独立裁决。

## 双弱、推导、强复核与 pending

AdjudicationService.judge 接收 kind、两端文本、EvidenceSet 和身份。EvidenceSet 由编排层经过历史准入和 EvidenceService 构建；直接调用该低层接口的调用方也须遵守此前置条件，不能把 scope 槽位作为证据包。

- 第一次 normal；第二次 challenge 不带第一次回答，不复用聊天历史。实体交换两端及证据顺序；依赖挑战列出删除、修改和替代来源反例。实际请求 hash、证据 hash、角色、call ID、输出与错误逐次保留。
- 第一次指出缺失信息时先返回 read_more，不为凑次数发第二次。最大允许历史仍缺关键前提则 pending，不调用强模型猜测。
- Judgment 显式返回 verdict、evidence_sha256、citations、steps、negative_basis、exclusion、conflicts、missing、semantic_basis。GroundedStep 包含原文 premises、向前引用的 previous_steps、原文 conclusion。程序核对每个引用的 source/hash/version/span、已读范围、前向步骤关系及排除依据；无模型 confidence 字段。
- 两次明确相同结论、硬检查通过、无缺失和冲突，才进入可结束计划选择。冲突、UNKNOWN、无原文支持的步骤或无效引用必须 strong。同级调用/schema 错误结束为 pending；只有 S2 RecordedCall 的声明临时错误可重试，最多三次，不嵌套重试。
- INDEPENDENT 需要 explicit_non_use 或 complete_derivation_excludes_upstream 及可核对的原文排除推导；alternative_support 不足。显式 provenance 候选不能被模型判独立后删除；未能确认时保留 pending。强结果仍缺证据则扩大/最终 pending，仍 UNKNOWN 或硬检查失败则 pending。

程序硬检查只证明引用/步骤存在及协议约束，不自动证明任意自然语言逻辑为真。正式语义正确性仍由独立离线路径评测验证。负结论的依据类型同样不是模型自我认证；原文及推导保存在报告供复查。

## 完整计划选择

Plan 的固定协议 `s4-mandatory-v1` 包含必需双弱、扩大和强复核；菜单只控制在结束条件已满足后的可选 strong。每个计划的 q/c、代理版本、成本公式版本和 lambda 都显式配置，没有 gold 或旧 lambda_edge 入口。没有合法菜单时 pending；极高价格不取消硬动作。

`choose_price` 最大化 q−lambda*c，目标并列优先较高 q、再较高 c、最后词典序 ID，因此二档 Δq/Δc=lambda 时升级。`solve_mckp` 是小菜单穷举参考纯函数：每 item 恰一项，预算内最大 q，并列最低总 c 再词典序 item/plan ID；空菜单不可行，空 item 集可行，非法数字/重复身份拒绝。它不是新增实验臂，不声称覆盖所有整数 Pareto 点。

## 节点、图及 S5 读取

`nodes.materialize_propositions` 复用 contexts/context_versions 和现有版本触发器，为 artifact 中每个 proposition 单独生成稳定 UUID 载体，复制原文命题，不按来源/文本/实体名合并。scope/owner 从引用来源继承，跨可见性命题拒绝，内部创建不装配默认用户事件流。实际建载体前须通过搜索准入。

历史来源如有共享的 prior_extractions，用其中已准入命题展开；没有历史抽取时保留明确的 source context/version 节点。原来源到命题的关联属于 NodeMapping，不自动发布依赖边。模型只裁决 upstream→downstream；UNKNOWN 不作为边或可靠负例。循环结论全部移入 pending，不能按插入顺序任选方向。

GraphSnapshot 完全沿用 S2 格式及 canonical hash。新 `DiscoveryReport(kind=discovery_report)` 封存在原 knowledge_artifacts 表，无迁移；保存 execution/code/config（完整配置及 hash）、shared/prior extraction refs、history hash、pages、NodeMapping、全部代际裁决、pending 原因、状态序列和调用 IDs。无效模型引用仅作审计数据，不赋予证据读取权限；封存服务仍校验已接受图、共享抽取和可用映射。

状态序列：awaiting_extraction → awaiting_search → awaiting_adjudication → complete/pending。存储故障和总超时可传播为运行错误，账本未完成 start 显示成本缺口。含 pending 的报告不是成功图，即使其中有部分已确认边。空成功抽取与失败分开，空图评分不会成功。

`read_graph_handoff(db, report_ref)` 返回报告、精确图、`source ContextVersion -> carrier nodes` 种子映射及 downstream 邻接。S5 应先检查 report.status，再从具体变化来源版本映射到种子，沿图中已确认方向遍历；pending 需单独处理，不能当不存在依赖。该 helper 不执行失效或维护。`assemble_graph_snapshot` 在 core 合并批图，冲突版本/边或环会拒绝，绝不直接合并候选。RQ1Result 中各 arm 的最终图绑定 run ID、strategy、replicate ID；本阶段没有生成任何真实 selective 主图。

## RQ1 runner、离线 scorer 与成本

`RQ1Runner.run(MaintenanceInput, account_id, run_id, replicate_id, cutoff_ordinal)` 只接受 S1 的精确严格类型，拒绝含 privileged 大对象；只导入允许前缀，保留 filler。当前初始导入要求 external source_version=1，其他值明确拒绝，不能默默改版本。分批大小显式配置，每批抽取一次供两臂共享，之前批次引用也共享。两臂模型/规则/菜单/重试/价格/分页等相同，只有 search strategy/scope 和扩大过程不同。

当前 runner 明确限定 synthetic，必须注入 httpx.MockTransport；真实运行放行入口和正式 manifest 留后续获准任务，不用这个入口提前 smoke。核心服务本身可显式配置实际客户端，但本轮没有发真实请求。S1 forward-lock、20/80、294、pl_030 注记及 v1/v2 产物未改。

`score_rq1(contract, paths, graphs, reviews)` 逐冻结 event-target 输出一行，缺图/漏抽取/断路/反向/pending/timeout/error 均保留分母。路径和 contract hash 必须有效，全路径每一参考步骤、实际节点版本、图/边 hash、原文引用及后续方向均核对，多条有效路径任一完整命中即可。`load_graph_evidence` 从真实封存服务导出图、映射、SourceSnapshot 和原文。

**离线语义映射方法：** 独立人员对照冻结 necessary_path_candidates 的有序实体/步骤、原始 dialogue_evidence 与实际命题/边，填写版本化 PathReview/StepReview（reviewer、rationale、PASS/FAIL/UNKNOWN）。节点序列逐位置对应参考路径；每步引用实际边 hash、参考步骤 hash 及支持原文。机器再核对引用属于实际边、来源 seal/version/content hash、原始 message turn 与冻结 witness。无法确认就 UNKNOWN/缺映射，不自动造认证。这里不把模型 DEPENDS、相似度或任意可达性当语义通过，也不改已有 labels。S4 scorer 代码身份单独登记；它不是 S8 总 scorer/统计或正式 manifest。

成本由 S2 逐次账本聚合，无累计计数器相减。每个 run 的只追加观察器只向底层账本转发一次；共享 extraction/ingest execution 每臂归属一份，整批 actual_batch_cost 只算一次。保存所有失败/retry、已知 token 与未知次数、模型费用已知小计/未知次数、币种、端到端 wall 和本地 work。嵌套 MeteredDB 只由最内层计量一次，避免重复累计 SQL 工作。

未定价本地工作仍是 unpriced_local_work，不称零成本；完整 billed_total 为 unknown/null，模型已知小计独立保留。未返回 usage/价格也是 unknown。每臂 wall 为其共享工作加独占工作，整批 wall 为真实端到端。S2 缓存 token 缺失时的既有计价限制继续适用，模型金额不能认证为精确实际账单。模型调用有完整 pricing/hash；没有新增正式价格或基础设施计价公式。

## 验证与限制

使用合成文本、MockTransport 和本轮全新 PostgreSQL 17/pgvector 实例。涉及数据库的测试只使用隔离 s2_repo；不调用旧 db_pool/clean_db，不清空业务数据。准确命令、最终数字、实例身份和源码清单见研究仓 S4-implementation.md。

真实模型抽取/判断质量、正式路径覆盖/成本优势、生产负载均未证明；没有选择正式参数。通用规则、双弱一致及确定性引用检查都不是语义正确性证书。交付状态为待独立验收，不进入 S5。

## S4-R1/R2 修订契约（2026-09-23，待独立复验）

新 `DiscoveryReport.configuration` 使用显式 `{encoding: "canonical-json-text-v1", payload: <canonical JSON 字符串>}`，避免 PostgreSQL jsonb 将科学计数 float 改写成整数、使报告 hash 漂移。`report.configuration_payload()` 解码并检查原 `config_sha256`，保留原配置值、数值类型和配置身份；读取配置请使用此方法，再按需 `DiscoveryConfig.model_validate(...)`。历史裸 dict 报告仍原样读取，不重写 payload/hash。新增错误码为 `configuration_encoding_unknown`、`configuration_payload_invalid`、`configuration_hash_mismatch`。没有修改全局 canonical_hash、S2 DTO、封存校验或数据库 schema；新报告须使用新运行/产物 ID，不能覆盖旧 ID。

`patha-rq1-s4-v2` scorer 将 frozen witness 的 source、turn_index、role 和完整 turn 内容绑定到原始 JSON 中对应 **content 字符串的位置**，按 EvidenceRef 的 Unicode 字符坐标核对引用。允许引用完整来源、完整 turn 或 turn 内短句/多个片段；至少一条该步实际引用须与对应 content 有非空交集，并处于完整 JSON 转义边界。相同文字出现在其他 turn、metadata、错来源或错版本时仍不能通过；不以重序列化后的字符串搜索代替位置核对。普通合成纯文本来源继续只接受 turn 0 的完整来源 witness。

这些是原协议的存储与引用粒度修复，不是语义判断门槛放宽：逐步离线 PASS、实际边/参考步骤 hash、原文/span/hash、方向、版本、完整路径和 pending 检查全部保留。坐标相交本身不认证语义；无可支持的离线语义复核仍不计覆盖。冻结 v1/v2、分母及研究参数均不改变。
