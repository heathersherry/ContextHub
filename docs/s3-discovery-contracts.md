# S3：共享抽取与候选查找

S3 不建立依赖边、不裁决独立性、不实现价格选择器。未接入默认启动/API/SDK；S7 负责装配。没有正式模型、prompt 或搜索参数默认值。

## 抽取一次、封存一次

`ExtractionConfig.load(path)` 读取严格 JSON 配置，必填 `id/model/base_url/temperature/max_tokens/prompt_template/retry`。temperature 可显式 null；retry 使用 S2 RetryPolicy。模板恰含一个 `{sources_json}`，JSON 示例中的字面花括号须双写；不允许转换或格式化该占位符。配置完整 canonical hash 必须与调用身份一致；实际 prompt/request/model/retry 身份由 S2 RecordedCall 保存。API key 单独注入，不进入配置。

```python
config = ExtractionConfig.load(config_path)
ledger = ExecutionLedger(repository)
core = ExtractionService(config, api_key=api_key, recorder=ledger)
async with repository.session(account_id) as db:
    artifact = await core.extract(db, source_refs, identity=extraction_identity)
shared_ref = artifact.reference()  # id AND sha256; commit transaction before reuse
await core.close()
```

`source_refs` 是一个会话/摄入批次的 SourceSnapshot 封存引用 tuple。通过 EvidenceService 读取确切 context/version/content_level 的原文；模型输入包括完整 snapshot 元数据和未经 trim 的原文 JSON。字符串转义不改变 JSON 解码后的 Unicode code point 坐标。

模型响应须是单个 JSON 对象，schema 为：

```json
{"propositions":[{"text":"完整命题，含条件和明确依赖线索", "evidence":[{"source_id":"来源 snapshot UUID", "start":0, "end":12, "excerpt":"与原文区间逐字相同", "provenance":["来源或显式依赖线索原文"]}]}]}
```

这里只说明形状；示意 UUID/区间不可直接执行。schema 禁止未知字段、必须显式提供 propositions；不接受自由文本、markdown fence 或缺字段作为空成功。模型配置的 prompt 负责说明这个 schema 和保留条件/线索的语义要求；本阶段没有选定正式 prompt，也不能保证真实模型不遗漏信息。

服务在 RecordedCall 的响应校验器中解析并校验声明来源、整数 code point `[start,end)` 和 excerpt，因此这些错误的逐次记录保留 provider 已返回的 token，且不触发重试。随后用 EvidenceService.reference 重读版本并构建 EvidenceRef，再由 ArtifactService.seal 校验封存。逐条使用 artifact ID + ordinal 生成命题 UUID；同文本不去重，不 strip 数字、空白、中文或条件，不过滤 provenance。一条命题可包含多处/多个来源证据。

- `complete` + 非空 propositions：抽取成功。
- `complete` + 空 propositions：模型返回显式有效 `{"propositions":[]}`，来源已成功读取。
- `pending` + `failure_reason`：读取/调用/解析/引用/封存校验失败。没有成功命题；attempted sources/config 身份仍保留。错误为分类码，不保存异常原文或密钥。
- 存储/账本本身不可用时会传播异常，不宣称失败 artifact 已持久化；独立账本的 start 可暴露未完成。事务持久性仍由调用者提交决定，不增加高可用/崩溃恢复机制。

新调用只走 S2 RecordedCall 单一重试入口。重复同一 logical call 会在本地开始预约处被拒绝，不能叠加重试。`ConversationExtractionService.extract_snapshot(...)` 是委托新核心的显式兼容入口；旧 `extract(str)` 保留历史行为，不会同时执行新核心。

## 最小 S2 兼容调整

原 ArtifactService 强制解析每个 SealRef，导致“源 seal 缺失/错误 hash”连 pending 失败记录都存不下。现在只有**没有任何命题的 pending PropositionArtifact**可以保存未解析的 attempted source 引用，仍检查来源 kind、租户及失败原因。complete 或带命题的 pending 仍执行原引用/证据校验。此例外不授予可用证据，不修改 schema/hash 算法/迁移；既有 S2 payload 身份保持。

## 统一查找接口

```python
page = await SearchService(recorder=ledger).search(
    db, shared_ref, history_snapshot, policy,
    identity=page_call_identity, cursor=previous_page.cursor,
)
```

两个策略必须传同一个 `shared_ref`；SearchService 没有模型客户端或抽取入口。历史候选是版本化来源，当前批次候选是封存命题，S4 用它们进一步判断关系。候选 ID 对同一封存对象稳定；不是已确认依赖边。

`HistorySnapshot` 显式携带 account、完整 allowed `entries`（SourceSnapshot 引用 + ingestion_ordinal）、`cutoff_at`、`cutoff_ordinal` 及可选 `ExplicitProvenance(proposition_id, source, reason)`。允许来源必须精确匹配 ID/hash/version，并同时满足逻辑摄入序号与真实摄入时间。未来序号即使数据库时间早也不能进入；同会话允许相同序号。错误/越界 manifest 和 provenance 明确返回 issue，不读取越界正文，不默默扩充输入边界。EvidenceRef.provenance 是原样保留的语义线索；可定位的外部来源由上述 typed ExplicitProvenance 提供，不把任意字符串猜成 source ID。

`SearchPolicy.load(path)` 必填 `id/strategy/initial_sources/expansion_factor/page_size/ordering/lambda_p1`。strategy 为 full-history/selective；ordering 为 ingestion_asc/desc，序号相同按 source UUID 同向排序。initial_sources 正整数、expansion_factor 整数≥2、page_size 正整数。lambda_p1 只记录配置身份，S3 从不以它否决强制补读或扩大。

- full-history 遍历全部允许版本化来源；不调用可选 index。
- selective 从排序后的 initial_sources 开始，加上整批命题来源和所有显式 provenance 来源。范围扩大按 `min(total,max(size+1,size*factor))`，上限就是允许 manifest。
- 当前内置查找使用确定性局部窗口，在窗口内保守返回全部来源；没有自行选定 embedding/model 或相似度阈值。可选 CandidateIndex 仅辅助枚举该范围，不授予过滤掉未读来源的权力；省成本来自范围，不来自无依据的候选排除。真实语义排序效果未验证。
- index 缺项（包括声称 complete 却漏项）、重复、越界、truncated、incomplete 或异常都触发扩大。普通 cursor 表示下一页，不是故障。最大范围的 index 异常再进行**一次直接来源表补读**，仍失败则终止未完成。
- 每个范围的序列是整个 extraction batch（原 ordinal 顺序）然后 source 候选；page_size 限制两者的总数。扩大时重试失败项，已返回命题 ID/已读 source 保留于 cursor，避免重复。S4 必须消费全部页再对完整批次配对；任何逐条 context 写入顺序都不参与查找。同批候选不会自动纳入未来会话。

## CandidatePage 完成状态

返回 artifact ID/hash、history/policy hash、候选 ID/文本/证据/来源 context-version/provenance、allowed/scope/read_sources、排序、cursor、page_no、complete/truncated、issues、expansion_reasons、at_max_scope。DTO 可 JSON 序列化并保存；本阶段返回页面供后续调用者消费，账本保存页面 hash 与实际工作量，不新增候选表。

| 返回 | 语义与调用者动作 |
| --- | --- |
| cursor 非空，truncated=false，无 issues | 正常分页；继续读取，不能当作范围遗漏 |
| cursor 非空，有 issues/扩大原因 | 尚在扩大或补读；必须继续，不能让价格停止 |
| cursor=null，complete=true | 当前声明 scope 已读完；selective 可以是局部完成，`at_max_scope` 区分全允许范围。不是依赖图正确或完整的认证 |
| cursor=null，complete=false | 最大允许范围或直接补读后仍未完成；保留 issues，交 S4 pending，不无限循环 |

扩大的旧原因保留，当前范围恢复成功可以 complete=true；旧失败对应的实际账本记录仍存在。cursor 绑定 artifact/history/policy 的完整 hash，跨范围/版本误用拒绝；输入边界由 manifest 决定，不查询“当前最新历史”。不以防人为伪造 cursor 为设计目标。

## 账本与 S4 接入

模型物理尝试沿用 S2 input/output token、状态、单一重试、provider 身份和 unknown usage；未提供价格，本地操作无模型，用量/费用明确 missing/unknown，不记作零 token API 调用。S2 金额仍不认证为精确实际账单。

抽取本地操作 call ID = uuid5(model call ID, `extraction-local`)；搜索每页由调用者提供新 CallIdentity（同 execution，config hash=完整 policy hash）。LocalOperation 同样保存 started/finished、输入/输出 hash、时间和错误。MeteredDB 记录包括引用复核/封存校验在内实际 fetchrow/fetch 的行数与返回值 UTF-8 JSON 字节、SQL 检查和 INSERT 次数；不是数据库网络字节，也不估算外部检索服务成本。每个 search 调用另记 retrieval_pages=1。API token 只从模型尝试汇总，不能把本地 start/finish 当成模型调用。

S4 接收原 shared_ref 和完整 CandidatePage 序列，保持 artifact 身份，消费问题状态、条件、原文证据和 provenance，再实现正式依赖裁决/价格策略/图封存。S3 无 dependencies/GraphSnapshot 写入，不恢复 fresh，不新增 S4–S8 行为。

## S3-R1：元数据恢复的统一准入校验

首次验收发现元数据读取失败后直接重读正文会绕过 history.cutoff_at；原通过用例不覆盖这个组合。修订后的 `_admit_source` 在 manifest 检查、候选正文恢复、同批证据解析和外部 index 输入前，统一核对显式来源引用/租户、逻辑摄入序号、SourceSnapshot 类型及当前 history 时间截止。只有通过的不可变 snapshot 进入本页缓存；EvidenceService 继续复核实际版本/原文/hash。未知元数据不作为读权限，外部 index 不接收未准入的来源或批次文本。

scope_sources 是按显式 manifest 与逻辑 ordinal 固定的待检查槽位，可能包含未验证或因时间越界而被拒绝的来源；它不授予正文读取权限。allowed_sources 仅包含本页已通过完整准入校验的来源，read_sources 表示实际读过正文。scope_size/offset 不随元数据暂时失败或恢复改变，避免跨页漏项。at_max_scope 表示已覆盖全部 manifest 待检查槽位；存在拒绝或失败项时仍不能 complete。

首次元数据错误保存具体错误码、source ref 和 `source_metadata_read_failed`；当前范围结束后强制扩大。上文最大范围的一次直接补读也适用于元数据读取失败，包括 full-history；复用 bypass_index 标记最后一轮，不增加无限重试，不能由 lambda 取消。恢复成功可在扩大/复查后完成；持续失败或恢复后发现时间越界则明确未完成，保留失败及扩大原因。正常下一页仍不是截断。此修订没有改变 S2 DTO/存储/账本/迁移，也没有生成正式依赖边。
