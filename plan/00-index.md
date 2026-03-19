# ContextHub — 企业版上下文管理系统设计

面向 toB 场景的企业版上下文管理系统，借鉴 OpenViking 核心 idea 但从零开发。对上通过 OpenClaw（作为 DataAgent）连接用户（数据分析和数据查询），对下连接企业存储后端（数据湖表、湖表元数据、文档、用户记忆和 skills）。

关键约束：
- 全新项目，不 fork OpenViking，只借鉴设计理念
- 对外保留 `ctx://` URI 文件语义（Agent 看到的不变），对内以 PG 为核心存储
- PG 统一管理元数据 + 内容（TOAST 处理大文本），向量库作为检索加速层
- 利用 PG 原生能力：ACID 事务、LISTEN/NOTIFY（变更传播）、RLS（租户隔离）、递归 CTE（血缘查询）
- DataAgent 采用 OpenClaw → 以 OpenClaw plugin 形式对接 ContextHub SDK（参考 OpenViking 的 openclaw-memory-plugin 模式）
- MVP 阶段使用单 OpenClaw 实例 + agent_id 切换验证多 Agent 协作（详见 09-implementation-plan.md）
- 数据湖表管理 和 多 Agent 协作 两条线并行推进

---

## 设计文档索引

| 文件 | 主题 | 关键内容 |
|------|------|----------|
| [01-storage-paradigm.md](01-storage-paradigm.md) | 统一存储范式 | URI 路由层、PG 核心表结构、向量索引层、可见性与权限 |
| [02-information-model.md](02-information-model.md) | 信息模型 | L0/L1/L2 三层模型（PG 列存储）、记忆分类、热度评分 |
| [03-datalake-management.md](03-datalake-management.md) | 数据湖表管理 | L2 拆解为结构化表、CatalogConnector、Text-to-SQL 上下文组装（PG JOIN） |
| [04-multi-agent-collaboration.md](04-multi-agent-collaboration.md) | 多 Agent 协作 | 团队所有权模型、Skill 版本管理（PG 表）、记忆共享与提升（PG 事务） |
| [05-access-control-audit.md](05-access-control-audit.md) | 权限与审计 | ACL 策略（PG 表 + RLS）、deny-override、字段脱敏、审计日志（PG 事务内写入） |
| [06-change-propagation.md](06-change-propagation.md) | 变更传播 | PG LISTEN/NOTIFY、dependencies 表、PropagationRule 三级响应 |
| [07-feedback-lifecycle.md](07-feedback-lifecycle.md) | 反馈与生命周期 | 隐式反馈（PG 表）、质量评分、状态机（PG status 列）、归档策略 |
| [08-architecture.md](08-architecture.md) | 系统架构 | PG 中心架构图、ContextStore URI 路由层、数据流 |
| [09-implementation-plan.md](09-implementation-plan.md) | 实施计划 | MVP 场景、SDK、Benchmark、Phase 1-3、PG 中心技术选型 |
| [10-code-architecture.md](10-code-architecture.md) | 代码架构 | 项目目录结构、依赖注入、API 端点、VectorStore 抽象、L0/L1 生成 |

## 依赖关系

```
01-storage-paradigm ──→ 02-information-model ──→ 03-datalake-management
        │                                              │
        └──→ 04-multi-agent-collaboration              │
                    │                                   │
                    ├──→ 05-access-control-audit        │
                    │                                   │
                    └──→ 06-change-propagation ←────────┘
                                │
                                └──→ 07-feedback-lifecycle
                                            │
                    08-architecture ←────────┘
                            │
                            └──→ 09-implementation-plan
                                        │
                                        └──→ 10-code-architecture
```

## 建议阅读顺序

实现时按编号顺序阅读即可。如果只关注某条线：
- 线 A（数据湖）：01 → 02 → 03 → 08 → 09
- 线 B（多 Agent）：01 → 02 → 04 → 05 → 06 → 07 → 08 → 09
