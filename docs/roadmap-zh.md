# ContextHub 路线图

本文档基于 `task-prompt/` 下各 Phase 的实施计划，汇总各阶段的目标、核心产出和当前状态。

> 各 Phase 之间是严格递进关系——后续 Phase 在前序 Phase 的冻结基线上叠加新能力，且不破坏已冻结的语义。

---

## Phase 1 — MVP 核心 ✅

**一句话**：从零构建 ContextHub 的完整 MVP 闭环——数据库 schema、`ctx://` URI 路由、四大核心服务、Python SDK、OpenClaw 插件和首个垂直载体（数据湖表管理），跑通"私有写入 → 晋升共享 → 跨 Agent 复用 → Skill 更新 → 下游 stale/advisory 感知 → 补偿恢复"的横向协作链路。

### 核心产出


| 模块                            | 目标                                                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **基础设施**                      | 项目骨架（FastAPI + asyncpg + Alembic）、PostgreSQL schema（含 RLS + pgvector）、Pydantic 模型、`ScopedRepo` request-scoped DB 执行模型      |
| **ContextStore + ACLService** | `ctx://` URI 路由与读写层、默认可见性（子读父、父不读子）、默认写权限（ownership + team_memberships）、认证中间件                                              |
| **MemoryService**             | 私有记忆写入、记忆晋升（`私有 → 团队 → 组织`）、`derived_from` 血缘追踪                                                                            |
| **SkillService**              | Skill 版本发布（不可变语义）、`is_breaking` 标记、订阅管理、版本解析（latest / floating / pinned / `?version=N`）                                    |
| **RetrievalService**          | pgvector 向量检索 → BM25 精排 → 可见性过滤、L0/L1/L2 按需加载、embedding 降级策略、Tools API（`ls`/`read`/`grep`/`stat`）                          |
| **PropagationEngine**         | Outbox 事件消费（claim → process → finish）、依赖传播（skill_version / table_schema / derived_from）、订阅通知、幂等副作用、LISTEN/NOTIFY 唤醒 + 周期补扫 |
| **Python SDK**                | Typed async client，封装所有 MVP API                                                                                            |
| **OpenClaw Plugin**           | 7 个 Agent 工具（`ls`/`read`/`grep`/`stat`/`store`/`promote`/`skill_publish`）、`assemble` 自动上下文注入、`afterTurn` 自动记忆提取            |
| **数据湖载体**                     | Catalog 同步（MockCatalogConnector）、表元数据 L0/L1 自动生成、`sql-context` 上下文组装、Embedding 对账（ReconcilerService）                       |
| **集成测试**                      | Tier 3 功能正确性测试（P-1P-8、C-1C-5、A-1A-4）、端到端 demo 脚本                                                                           |


### 冻结的关键设计决策

- **单数据库架构**：PostgreSQL 承载元数据 + 内容 + 向量 + 事件，无外部向量库、无消息队列
- **Request-scoped DB 执行模型**：所有 SQL 通过 `ScopedRepo` 执行，`SET LOCAL app.account_id` 绑定租户
- **RLS 租户隔离**：所有面向 Agent 的表启用行级安全
- **文件系统范式**：Agent 使用 `ls`/`read`/`grep`/`stat` 操作上下文，LLM 天然理解

---

## Phase 2 — 显式 ACL 与审计

**一句话**：在 Phase 1 的默认可见性 / 默认写权限基线之上，叠加**读路径**的显式 ACL allow/deny/field mask、分层审计日志，以及基于 ACL share grant 的跨团队窄范围共享，使 ContextHub 具备企业级的细粒度读访问控制与合规审计能力。

### 具体目标

1. **显式 ACL allow/deny**：管理员可为任意 `ctx://` URI 模式设置读路径的 allow/deny 策略，deny 永远优先于 allow（deny-override）
2. **团队层级约束**：父团队的 deny 不可被子团队的 allow 覆盖——从根团队向下遍历，任一层级的 deny 即终止
3. **关键词级脱敏（field mask）**：检索/读取结果中敏感关键词替换为 `[MASKED]`（正则级文本替换，返回时过滤，不在存储层加密）
4. **分层审计日志**：状态变更操作（create/update/delete/promote/publish/policy_change）fail-closed 保证不丢失；观察性操作（read/search/ls/stat）best-effort 保证不拖死主流程
5. **跨团队 ACL share grant**：通过 ACL allow 策略授权窄范围跨团队共享（不复制内容、不创建 proxy context），作为 promote 之外的第二条共享路径
6. **策略管理 API + 审计查询 API**：CRUD endpoints for `access_policies` 和审计日志检索
7. **SDK / Plugin 扩展**：客户端方法支持新 ACL/audit 能力

### 核心产出


| 产出                  | 说明                                                               |
| ------------------- | ---------------------------------------------------------------- |
| `access_policies` 表 | 显式 ACL 策略存储（uri_pattern, effect, actions, field_masks, priority） |
| `audit_log` 表       | 审计日志存储（fail-closed + best-effort 分层）                             |
| ACL 读路径评估引擎         | 两层访问模型：默认基线（Phase 1）→ 显式 ACL 覆盖（Phase 2）                         |
| MaskingService      | 检索/读取结果的关键词脱敏                                                    |
| AuditService        | `log_strict` / `log_best_effort` 分层审计                            |


### 设计约束

- **写路径不变**：Phase 2 只覆盖 `read` action，写权限继续沿用 Phase 1 的 ownership + team_memberships 模型
- **ACL 是覆盖层**：对默认基线的覆盖，不是取代基线的全局白名单系统；无匹配策略时回退到默认访问基线

---

## Phase 3 — 反馈闭环、生命周期管理与长文档检索

**一句话**：引入**质量反馈信号**驱动检索排序优化，实现**自动生命周期状态转换**治理上下文膨胀，并补全**长文档结构化检索**能力以支持 MB 级章节化文档的精确定位。

### 具体目标

#### 反馈闭环

1. **显式反馈采集**：Agent 通过 `contexthub_feedback` tool 报告上下文的采纳/忽略结果（adopted / ignored / corrected / irrelevant），反馈信号回写到 `contexts` 表
2. **质量评分融入检索排序**：`quality_score = adopted_count / (adopted_count + ignored_count + 1)`，高采纳率上下文排名靠前，高忽略率噪音上下文被降权
3. **低质量上下文管理**：通过报告识别和管理噪音上下文

#### 生命周期管理

1. **自动状态转换**：上下文按生命周期策略自动进行 `active → stale → archived → deleted` 状态转换
2. **stale 恢复**：stale 上下文被直接访问（read）时自动恢复为 active
3. **变更传播与生命周期统一**：变更传播触发的 stale 标记与生命周期策略触发的 stale 标记共享同一状态机
4. **归档管理**：archived 上下文从向量索引中移除（清除 `l0_embedding`），从搜索结果中排除，但仍可通过 URI 直接读取

#### 长文档检索

1. **长文档入库**：文件系统存储 + PG 元数据的混合架构，支持 PDF + 纯文本 + Markdown
2. **树导航检索**：`TreeRetriever` — PG 树节点 → LLM 逐层推理 → 文件系统读取，实现章节级精确定位
3. **关键词检索**：`KeywordRetriever` — ripgrep 搜索 + Monte Carlo 采样

### 核心产出


| 产出                                    | 说明                                              |
| ------------------------------------- | ----------------------------------------------- |
| `context_feedback` 表                  | 反馈记录存储                                          |
| `lifecycle_policies` 表                | 生命周期策略配置                                        |
| `document_sections` 表                 | 长文档树结构                                          |
| FeedbackService                       | 反馈采集 + quality_score 计算 + RetrievalService 排序集成 |
| LifecycleService + LifecycleScheduler | 生命周期状态自动转换、stale 恢复、归档清理                        |
| LongDocumentIngester                  | 长文档入库（文件系统 + PG 元数据）                            |
| TreeRetriever / KeywordRetriever      | 长文档检索双路径                                        |
| RetrievalRouter                       | 检索策略路由，可插拔策略分发                                  |
| `contexthub_feedback` tool            | OpenClaw Plugin 新增工具                            |


---

## Phase 4 — ECMB 量化评估

**一句话**：构建可复现的量化评估框架（Enterprise Context Management Benchmark），通过 **Tier 1 硬指标**和 **Tier 2 A/B 消融实验**，量化 ContextHub 各核心设计决策的收益强度，将 MVP 从"能跑通"提升到"能量化证明收益有多大"。

### 具体目标

1. **实验 1 — L0/L1/L2 vs 平坦 RAG**：证明分层检索相比标准 chunk+vector RAG，Token 消耗显著降低（预期 60–80%），SQL 执行准确率（EX）持平或略高
2. **实验 2 — 有/无结构化关系**：证明结构化上下文组装（JOIN 关系 + 查询模板 + 业务术语）相比纯语义检索，在多表 JOIN 查询上 EX 显著提升
3. **实验 3 — 有/无变更传播**：证明变更传播引擎在 schema 变更后能维持下游 SQL 质量，而无传播时质量显著下降
4. **实验 4 — 有/无共享记忆**：证明跨 Agent 共享记忆（知识继承）相比冷启动，analysis-agent 的 EX 更高、首次正确回答所需轮次更少
5. **统计显著性**：所有对比 p < 0.05 或标注不足时诚实说明
6. **竞品对比**：与 Mem0 / CrewAI / Governed Memory 的功能维度对比表
7. **诚实标注**：每个实验的局限性、信号重叠和不可量化的能力

### 核心产出


| 产出                                     | 说明                                                      |
| -------------------------------------- | ------------------------------------------------------- |
| 测试数据集                                  | BIRD 子集 + 企业增强（JOIN 关系、血缘、查询模板、业务术语、变更场景、多 Agent 工作流数据） |
| BenchmarkRunner                        | 可复现实验执行框架 + ExperimentConfig                            |
| FlatRAGBaseline                        | 公平对照组——行业标准 RAG（chunk → embed → top-K → prompt）         |
| SQLGenerationPipeline                  | LLM 调用 + 上下文注入 + SQL 执行 + 结果比较                          |
| MetricsCollector + StatisticalAnalyzer | EX、Token per Query、Table Precision@5、延迟、统计显著性分析         |
| 评估报告                                   | 量化结果、统计分析、对比表、局限性标注                                     |


### 设计约束

- **EX（Execution Accuracy）** 为主指标：比较生成 SQL 在数据库上的执行结果而非 SQL 文本本身
- **Baseline 必须公平**：使用相同 LLM、相同 embedding 模型、相同 prompt 骨架、相同数据源
- **实验隔离**：每次运行前从 seed 脚本重建数据库状态，temperature=0 确保可复现

---

## Phase 5 — 生产加固

**一句话**：将 ContextHub 从"单实例可用"提升到"**多实例可部署、多框架可接入、真实数据源可对接**"的生产就绪状态。

### 具体目标

#### 多实例安全

1. **PropagationEngine SKIP LOCKED**：多个 ContextHub 实例并发处理 `change_events`，每个实例通过 `SELECT FOR UPDATE SKIP LOCKED` 领取不同事件子集，无重复、无遗漏
2. **LifecycleScheduler / ReconcilerService 排他执行**：PG advisory lock 实现多实例下的 sweep 去重，防止重复状态转换
3. **LISTEN/NOTIFY 多实例兼容**：任意一个实例收到通知即可触发处理

#### 多框架接入

1. **MCP Server**：暴露 ContextHub 全部 Agent 工具（ls/read/grep/stat/store/promote/skill_publish/feedback）和资源发现能力，符合 MCP 协议规范，支持 stdio + HTTP SSE 传输，打通非 OpenClaw Agent 框架接入通道

#### 真实数据源对接

1. **真实 Catalog 连接器**：连接器框架强化 + 至少一种真实连接器（如 Hive Metastore / Snowflake Information Schema）+ 健康检查与重试

#### 运维可观测性

1. **生产级可观测性**：结构化日志、Prometheus 指标导出、分层健康检查（liveness / readiness）

#### 技术债务收敛

1. **pool.py codec 收敛**：集成 `codecs.py`，消除废弃 API 风险
2. **性能回归基线**：复用 ECMB Benchmark 框架建立 CI 自动化性能回归检测

### 核心产出


| 产出                    | 说明                                           |
| --------------------- | -------------------------------------------- |
| SKIP LOCKED 事件领取      | PropagationEngine 多实例无重复处理                   |
| PG advisory lock 排他调度 | LifecycleScheduler / ReconcilerService 多实例安全 |
| MCP Server            | MCP 协议实现 + tools/resources 映射 + 双传输层         |
| 真实 Catalog 连接器        | 连接器框架 + 端到端 catalog sync                     |
| 可观测性套件                | 结构化日志 + Prometheus metrics + 健康检查            |
| 多实例集成测试               | MI-1 ~ MI-6 多实例正确性测试                         |


---

## 各 Phase 之间的排除项总览

以下能力已在各 Phase 中被明确排除，保留为后续增强方向：


| 能力                               | 当前状态                                                        |
| -------------------------------- | ----------------------------------------------------------- |
| 写路径 ACL overlay                  | 写路径语义分裂，当前无统一模型，持续沿用 Phase 1 的 ownership + team_memberships |
| 隐式反馈推断（afterTurn 自动判断）           | 需调研 OpenClaw afterTurn 能力                                   |
| 反馈驱动自动 promote                   | 需先积累足够反馈数据验证 quality_score 可靠性                              |
| path ACL 升级到 ReBAC/Zanzibar      | 当前 path ACL overlay 已覆盖已知需求                                 |
| LISTEN/NOTIFY 迁移到消息队列            | PG outbox + SKIP LOCKED 已足够生产级多实例                           |
| 长文档跨文档关联检索                       | Phase 3 先做单文档内章节定位                                          |
| Kubernetes Operator / Helm Chart | Phase 5 提供多实例能力但不绑定编排平台                                     |


