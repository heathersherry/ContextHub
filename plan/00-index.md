# ContextHub — 系统概览

面向 toB 场景的企业版上下文管理系统，借鉴 OpenViking 核心 idea 但从零开发。对上通过 OpenClaw（作为 DataAgent）连接用户（数据分析和数据查询），对下连接企业存储后端（数据湖表、湖表元数据、文档、用户记忆和 skills）。
```
        用户
         │
    ┌────┴────┐
    │ OpenClaw │  ← Agent 运行时
    └────┬────┘
         │
    ┌────┴─────────┐
    │ ContextHub   │  ← 上下文管理中间件（应用层）
    └────┬─────────┘
         │
    ┌────┴───┐
    │  PG    │  ← 存储层
    └────────┘
```

关键约束：
- 全新项目，不 fork OpenViking，只借鉴设计理念
- 对外保留 `ctx://` URI 文件语义（Agent 看到的不变），对内以 PG 为核心存储
- PG 统一管理元数据 + 内容（TOAST 处理大文本），pgvector 扩展提供向量检索（同库同事务）
- 利用 PG 原生能力：ACID 事务、LISTEN/NOTIFY（变更传播）、RLS（租户隔离）、递归 CTE（血缘查询）
- DataAgent 采用 OpenClaw → 以 OpenClaw context-engine 插件形式对接 ContextHub SDK（参考 OpenViking 新版 context-engine 架构，详见 13-related-works.md）
- MVP 阶段使用单 OpenClaw 实例 + agent_id 切换验证多 Agent 协作（详见 09-implementation-plan.md）
- 多Agent协作（核心）和 数据湖表管理（首个垂直场景）两条线并行推进

---

## 设计文档索引

| 文件 | 主题 | 关键内容 |
|------|------|----------|
| [00a-canonical-invariants.md](00a-canonical-invariants.md) | **权威不变式** | 租户唯一性、类型系统、可见性继承、两层访问模型、状态机、版本不可变性。**后续所有文档的约束基准。** |
| [01-storage-paradigm.md](01-storage-paradigm.md) | 统一存储范式 | URI 路由层、PG 核心表结构、向量索引层、可见性与权限 |
| [02-information-model.md](02-information-model.md) | 信息模型 | L0/L1/L2 三层模型（PG 列存储）、记忆分类、热度评分 |
| [03-datalake-management.md](03-datalake-management.md) | 数据湖表管理 | L2 拆解为结构化表、CatalogConnector、Text-to-SQL 上下文组装（PG JOIN） |
| [04-multi-agent-collaboration.md](04-multi-agent-collaboration.md) | 多 Agent 协作 | 团队所有权模型、Skill 版本管理（PG 表）、记忆共享与提升（PG 事务） |
| [05-access-control-audit.md](05-access-control-audit.md) | 权限与审计 | **明确后置 backlog owner**：显式 ACL、字段脱敏、审计日志与窄范围共享 |
| [06-change-propagation.md](06-change-propagation.md) | 变更传播 | PG LISTEN/NOTIFY、dependencies 表、PropagationRule 三级响应 |
| [07-feedback-lifecycle.md](07-feedback-lifecycle.md) | 反馈与生命周期 | **明确后置 backlog owner**：反馈闭环、质量信号与生命周期治理 |
| [08-architecture.md](08-architecture.md) | 系统架构 | PG 中心架构图、ContextStore URI 路由层、数据流 |
| [09-implementation-plan.md](09-implementation-plan.md) | 实施计划 | MVP 场景、SDK、Benchmark、Phase 1-3、PG 中心技术选型 |
| [10-code-architecture.md](10-code-architecture.md) | 代码架构 | 项目目录结构、依赖注入、API 端点、VectorStore 抽象、L0/L1 生成 |
| [11-long-document-retrieval.md](11-long-document-retrieval.md) | 长文档检索策略 | **明确后置 backlog owner**：长文档高级检索扩展的触发条件与设计种子 |
| [12-evolution-notes.md](12-evolution-notes.md) | 保留 ADR | 对象存储、消息队列、ReBAC/Zanzibar 等替代架构的拒绝原因与重开条件 |
| [13-related-works.md](13-related-works.md) | 相关工作分析 | OpenClaw 插件体系、lossless-claw DAG 无损压缩、OpenViking 记忆适配器、ContextEngine 接口、架构决策参考 |
| [14-adr-backlog-register.md](14-adr-backlog-register.md) | ADR / Backlog Register | Session 7 的统一分流结果：后置项、ADR、rejected ideas、重开入口 |

## 依赖关系

```
00a-canonical-invariants ──→ 所有文档（权威约束基准）

01-storage-paradigm ──→ 02-information-model ──→ 03-datalake-management
        │                       │                        │
        │                       └──→ 11-long-document-retrieval
        │                                                │
        └──→ 04-multi-agent-collaboration                │
                    │                                    │
                    ├──→ 05-access-control-audit          │
                    │                                    │
                    └──→ 06-change-propagation ←─────────┘
                                │        ↑
                                │        └── 11-long-document-retrieval
                                └──→ 07-feedback-lifecycle
                                            │
                    08-architecture ←────────┘
                            │
                            └──→ 09-implementation-plan
                                        │
                                        ├──→ 10-code-architecture
                                        │
                                        ├──→ 12-evolution-notes（保留 ADR，依赖 01 + 05 + 06）
                                        │
                                        ├──→ 13-related-works（独立参考文档，依赖 08）
                                        │
                                        └──→ 14-adr-backlog-register（汇总 05 / 07 / 09 / 11 / 12 / 13 的后置项与 rejected ideas）
```

## 建议阅读顺序

所有线路都应先读 `00a-canonical-invariants`（权威约束），再按编号顺序阅读。如果只关注某条线：
- 线 A（数据湖）：01 → 02 → 03 → 08 → 09
- 线 B（多 Agent）：01 → 02 → 04 → 05 → 06 → 07 → 08 → 09
- 线 C（长文档检索）：01 → 02 → 11 → 06 → 09
- 只看当前主线时，读到 `10-code-architecture.md` 即可；所有后置项、ADR 和 rejected ideas 统一看 `14-adr-backlog-register.md`
