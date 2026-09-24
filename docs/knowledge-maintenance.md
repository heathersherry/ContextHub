# 知识摄入与变化维护（S7，修订待独立复验）

这是显式启用的普通应用入口。HTTP、Python SDK 和未来实验都调用
`contexthub.services.knowledge_factory.create_knowledge_service`，共用 S3–S6。
本阶段仅完成本地合成验证；没有选择正式模型、运行真实模型或启动 S8。
数据库仍使用 migration `011`，无需新表、任务队列或迁移。

## 启用与配置

默认 `KNOWLEDGE_ENABLED=false`，旧 memory/context 行为不变。
启用需要 `KNOWLEDGE_ENABLED=true` 和 `KNOWLEDGE_CONFIG_PATH=/absolute/config.json`。
JSON 必须满足 `KnowledgeConfig`；未知字段（包括 task_type、hop、gold、question、path_labels）拒绝。
以下顶层配置全部必填，嵌套合同也按各自模型严格验证：

| 字段 | 用户提供的内容 |
| --- | --- |
| id、code_version | 配置与可还原代码身份 |
| extraction、extraction_price | 抽取模型、endpoint、prompt、解码、token 上限、显式重试；价格依据或显式 null |
| discovery | full-history/selective 搜索范围、分页/扩展/顺序、lambda_P1；weak/strong 裁决模型、Stage0 规则/别名版本、计划与代价版本、价格、总时限 |
| maintenance | S5 分页、chunk、内容层、期限、显式读取重试策略 |
| profile | 三种 profile 之一、lambda_P2 与计价单位/各动作估计、单动作及规则时限 |
| base、strong、verifier、change_location | 各自模型/endpoint、prompt_template、解码、max_tokens、rules_version、完整 RetryPolicy、price |
| rules | 规则 id/version 有序清单（可显式为空）；必须与注入的可信规则实现完全一致 |
| execution_owner | 当前只允许 manual |
| ingest_timeout_seconds | 整个摄入流程的有限期限 |

价格 `null` 表示未知，不能当免费；沿用 S2 计价限制。
S3 的 prompt 占位符、S6 的 `{input_json}`、schema、hash 与解码约束由原合同验证。
配置 hash 不含 API key；endpoint 不允许用户信息、query 或 fragment 携带秘密。
不要把密钥写入 prompt、配置、示例或日志。

密钥分开用环境变量提供：`KNOWLEDGE_EXTRACTION_API_KEY`、
`KNOWLEDGE_ADJUDICATION_API_KEY`、`KNOWLEDGE_BASE_API_KEY`、
`KNOWLEDGE_STRONG_API_KEY`、`KNOWLEDGE_VERIFIER_API_KEY`、
`KNOWLEDGE_CHANGE_LOCATION_API_KEY`。库内装配可传 `KnowledgeSecrets`。
不会用 `Settings.chat_model`、旧 prompt、旧重试或旧 API key 补齐缺项。
启用后配置/凭据/规则不匹配会在数据库、模型客户端和后台启动之前失败，
错误为 `knowledge_configuration_invalid`；不能带病返回 ready。

`examples/knowledge-fixture.json` **仅是离线 fixture**，不是可用于部署的推荐值。
可用 `KnowledgeConfig.model_json_schema()` 查看完整配置 schema，
用 `KnowledgeConfig.load(path)` 先离线验证。非空规则库由嵌入应用通过
`ApplicationDependencies(rules=...)` 提供，不从任意配置路径动态执行 Python。

应用 lifespan 关闭所有知识客户端、旧 chat/embedding、worker、scheduler 和 pool；
部分初始化失败也关闭已创建资源。独立 factory 使用者必须 `await service.close()`。
普通应用其他功能仍按原 Settings 使用 chat/embedding；离线测试务必显式替换这两个边界，
仅替换知识生成器不足以隔绝索引或旧接口的网络请求。

## 公共接口

沿用 `X-API-Key`、`X-Account-ID`、`X-Agent-ID` 及现有可信请求上下文。
请求体不能覆盖 account/agent。原鉴权机制未重构；部署时可信头来源仍由原机制约束。
所有操作要求当前 ACL；同租户不同 agent 不能凭 UUID 查询/维护另一个 agent 的操作。
当前采用保守的操作创建者限制，即使来源是 team 共享也不转移操作执行权。

| HTTP（同步，成功返回 200） | SDK | 请求 |
| --- | --- | --- |
| POST `/api/v1/knowledge/ingest` | `client.knowledge.ingest(KnowledgeIngestRequest(...))` | request_id UUID、content；scope=agent/team，team 仅 team 必填；mode=content/change；provenance 为可选 context_id/version 列表 |
| POST `/api/v1/knowledge/maintain` | `client.knowledge.maintain(KnowledgeMaintainRequest(...))` | request_id UUID、真实 event_id、已登记 graph_id |
| GET `/api/v1/knowledge/operations/{operation_id}` | `client.knowledge.get_maintenance_result(operation_id)` | 无请求体；只读 |

没有公开提交 PASS、proof、清原因、设置 fresh 或自选 affected targets 的入口。
所有请求严格拒绝多余字段。SDK 只调用 HTTP，不导入 server core、不连数据库、不需要研究仓/MEME。
SDK 不给有副作用的操作添加隐式重试。原方法、异常映射、If-Match 和 async 关闭方式保留。

`KnowledgeResult` 返回 operation/request/execution/config 身份，operation/status/reason，
content_saved、graph_status、maintenance_ready、source（ID/URI/当前 version/fresh），
graph/report/watch/event IDs、event_delivery、cutoff_at、location_status，
nodes 及 works（work/frame/target/proof ID、状态/原因、当前版本/fresh）。
不返回原始内部 artifact、证据正文、prompt、一次性许可或异常堆栈。
图节点/工作按当前 ACL 过滤；原来源权限撤回时整个操作拒绝读取。

错误语义：401 未认证；403 来源/范围/操作 agent 无权；404 未知或跨租户来源、
事件、登记图、操作；409 请求 ID 内容冲突或来源版本冲突；428 context PATCH 缺 If-Match；
422 schema 错误/越权字段；503 `knowledge_disabled`。配置错误阻止应用启动，不返回成功 HTTP。
运行错误/超时可能在已保存操作内以 pending/error 和稳定 reason 返回；基础设施完全不可用时
可返回 500/连接失败，不承诺产生最终日志。不要从请求失败推断事务全程回滚。

## 完整使用步骤

```python
from uuid import uuid4
from contexthub_sdk import ContextHubClient, KnowledgeIngestRequest, KnowledgeMaintainRequest

async with ContextHubClient(url=server_url, api_key=api_key,
                            account_id=account, agent_id=agent) as client:
    ingestion = await client.knowledge.ingest(KnowledgeIngestRequest(
        request_id=uuid4(), content=original_text))
    # 保存这些稳定 ID。content_saved 单独成立不表示依赖已建好。
    if ingestion.maintenance_ready:
        await client.context.update(ingestion.source.uri,
            expected_version=ingestion.source.version, l2_content=replacement_text)
        state = await client.knowledge.get_maintenance_result(ingestion.operation_id)
        # 示例仅有一次修改；多事件时选择实际修改对应的事件，勿盲选别人的事件。
        result = await client.knowledge.maintain(KnowledgeMaintainRequest(
            request_id=uuid4(), event_id=state.event_ids[-1], graph_id=ingestion.graph_id))
        current = await client.knowledge.get_maintenance_result(result.operation_id)
```

服务先验证范围/来源 ACL、保存版本化原文，封存截止点与 provenance，再共享抽取一次和 S4 完整裁决。
P1 历史范围是当前 account、同 scope/owner 下可完整读取的公共 knowledge 原始来源，
加显式 provenance；候选原文/版本/顺序被封存。同批命题一起参与发现；跨批次复用原抽取 seal，
而非按插入顺序漏掉关系。P2 保留 S5 合同：维护截止点之前全部允许读取的账户历史。
图登记前重查版本；watch 的短事务提交后才宣告 maintenance_ready。
登记期间漂移返回 pending，不绕过过期图检查。

修改已有来源继续用原 context PATCH + expected_version/If-Match。S5 先提交全部目标失效屏障，
然后手动 maintain 调 `begin_maintenance` 和 S6 runner。后台 worker 仅交付新流程事件，
不生成候选，不回退旧执行器。默认无需为手动维护启动 worker；交付成功也不表示已恢复。
S6 提交生成的子事件归原前沿负责，单独 maintain 返回 409 `knowledge_child_event_owned_by_root`。
同一 event+graph 的操作 ID 固定；并发或换 request_id 只查询已有领取，不补充菜单或期限。
摄入同 request_id 同内容返回已有结果，改内容为 409。不要用新 request_id 试图补救不确定提交。

同步摄入受 ingest_timeout_seconds 限制；维护沿用 S5 frame 截止时间和 S6 动作/规则/重试限制。
HTTP 客户端超时不会证明服务端没提交，保留 ID 后只读查询已知工作/证明。
操作完成记录写入失败时，可见 running/execution_incomplete，同时数据库工作可能已经 updated；
查询会如实返回工作和当前 fresh，不启动恢复或重跑。没有跨进程恢复/高可用承诺。

S7-R1 修订明确处理应用和数据库仍可用时的协程取消：退出请求前，以受保护的
有限清理事务保存 `status=error/reason=request_cancelled`，把该请求拥有的
pending/running 工作按原 S6 stop_work 变为 unresolved；已 updated/unchanged 的
工作、proof 和当前内容保持。已保存的 complete 记录也不改写。重试和查询都不重跑。
内部 owner_call_id 区分真正领取者与重复查询请求，取消后者不会终止前者。
服务关闭先取消并等待这些清理，再关闭客户端；可回复时返回 503 knowledge_closing。

清理最多使用配置中的 maintenance.timeout_seconds，只做终止与记录，不生成、
核验、提交、续期或恢复 fresh。它抵御重复 task.cancel 和 ASGI 的取消作用域；
没有新增持久任务系统或后台维护责任。强杀进程、数据库不可用或清理本身超时仍可能
留下 execution_incomplete，不能承诺故障恢复；旧版本已经遗留的操作不会自动接管。
公共本地操作账本记录 request_cancelled；被中断的内层 started 记录保留为不完整成本，
不会伪造未知 token/费用为零，也不会给未执行完的动作补一个成功结果。


## 只有变化原文时

`mode="change"` 仍先走同一摄入/封存/发现流程，再用显式配置的 change_location 模型
在已声明、可读的真实来源版本中定位。它必须返回唯一候选、完整旧来源原文依据、
新原文中的 replacement 片段、主体/属性及时间变化依据。引用由程序逐字核对，
候选版本、定位记录与逐次费用入账。只有目标可写且已存在已提交的有效 watch，
才通过同一个 ContextService.update(expected_version) 产生真实 old→new 事件。

这是有意有限的“单来源完整替换”：不支持猜测多目标、合并独立事实、仅凭同名覆盖、
部分旧来源覆盖或无明确新值的更新。重复候选原文强制 pending；定位不唯一/证据不足弃权；
unrelated 只摄入新事实。原型 ChangeDetectionService 的 superseded-ID 事件没有准确新版本，
不能作为 S5 事件直接复用；旧 MemoryService 原入口保留，新增 ingest_knowledge 委托此服务。

程序核对的是结构、版本和真实引文，语义定位仍依赖配置模型；合成测试不证明通用自然语言准确率。
用户本来知道 source ID/version 时，应直接 PATCH，不必让定位模型重新猜测。
S7-L1 范围限制继续保留：同条原始消息的多个来源变化或部分来源更新尚未支持，
不能把本接口称为全部原始变化对话接通。后续适配不得用 gold/root_change/路径定位，
也不得擅自改写或拆分原文来绕过此限制；需要扩展时先明确协议与授权。
运行时禁止由 MEME root_change、gold、任务/hop、问题或路径 labels 注入目标。

## 状态、profile 与边界

- content_saved 只说明原文持久化。graph_status=pending 或 maintenance_ready=false 不能声称完整启用。
  maintenance_ready 记录摄入时 watch 登记成功；来源之后改变，必须另看当前节点 fresh。
- status=complete 表示本次编排结束；works 仍可能 unresolved。pending/running 是操作/工作状态，
  event_delivery=succeeded 仅表示事件交付。历史 updated/unchanged 不保证目标今天仍 fresh。
- updated 和 unchanged 都要硬检查及同一个固定 verifier 的逐项 PASS；规则和模型只提候选。
  只有核心原受控调用内的一次性许可可提交并解除本次原因。证据不足、上游未决、版本漂移、
  超时、无合法动作、lambda 排除升级会 unresolved 并保持 non-fresh；普通读取拒绝该目标。
- `local/base-only` 固定 M1，`full/strong-only` 固定全历史+strong，`adaptive` 有限 M1→M2→M3。
  同配置 M1/M2 是同一 base，M3/强对照同一 strong，三种 profile 共用固定 verifier、prompt/hash/schema/解码。
- 公共前沿先上游后下游，至多三个 carrier 层（源命题载体加两跳），只有真实成功 proof 能
  continue_after_commits 生成新 frame。上游未决时下游不生成。更深/环路停止 public_frontier_limit；
  未扩展深链/汇合 DAG 调度。S6 保守表锁、进程内许可、可信规则线程限制保持。

操作身份关联 account/agent、event/root/execution、配置、report/watch/frame/work/proof。
模型调用/token/失败/重试沿用唯一 RecordedCall 账本；S7 仅记录自己的本地 SQL/操作，
不重复记模型成本。S6 verifier 最早读取前的账本初始化未移动。
查询不发模型、不构包、不写账本、不修复 fresh。接续会保留旧 frame 的
`unresolved/superseded_by_continuation` 历史；同一目标的新 frame 可已 updated，
所以需结合 frame/work 身份与当前 fresh 读取，不应把历史工作条数当目标数。

## 无真实模型的合成示例

先由运维建立**全新的** UTF8 专属测试数据库、migration 011 与非 superuser/非 BYPASSRLS 角色。
示例拒绝默认端口/其他库名，不创建或清空库；不要指向业务库或历史冻结实例。

```sh
PYTHONPATH=src:sdk/src:. .venv/bin/python -m examples.knowledge_synthetic \
  --dsn postgresql://s2_s7_app@127.0.0.1:55557/contexthub_s2_s7
```

该地址是本次已停止保留的证据实例，复验应另建专属实例或经授权启动它。
示例显式 Settings(_env_file=None)、NoOpEmbeddingClient、拒绝调用的旧 chat、MockTransport，
关闭 scheduler/worker；真实 SDK 连 ASGI 应用，真实数据库和 S3–S6 执行。
fixture provider 只按输入原文返回预设合成响应，不读取离线 gold/任务标签。
这验证入口合同和状态转换，不能说明真实模型质量、正式实验获准或生产就绪。
