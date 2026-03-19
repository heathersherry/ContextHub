# 08 — 系统架构

## 架构图

```
┌─────────────────────────────────────────────────────────┐
│              OpenClaw（单实例，多 Agent 身份）              │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐      │
│  │ 数据查询  │  │ 数据分析  │  │ 其他业务 Agent   │      │
│  │ agent_id │  │ agent_id │  │ agent_id         │      │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘      │
│       └──────────────┼────────────────┘                 │
│                      ▼                                   │
│         ContextHub OpenClaw Plugin                       │
│   （注册 tools + lifecycle hooks，调用 SDK）              │
│                      ▼                                   │
│              ContextHub Python SDK                       │
│          （ctx:// URI + 文件语义接口）                     │
└──────────────────────┬──────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────┐
│                  ContextHub Server (FastAPI)              │
│                                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐     │
│  │ Context    │  │ Memory     │  │ Skill          │     │
│  │ Service    │  │ Service    │  │ Service         │     │
│  └─────┬──────┘  └─────┬──────┘  └──────┬─────────┘     │
│        │               │                │               │
│  ┌─────┴───────────────┴────────────────┴──────────┐    │
│  │              Core Engine                         │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐    │    │
│  │  │ Retrieval│ │ Indexer  │ │ Propagation  │    │    │
│  │  │ Engine   │ │          │ │ Engine       │    │    │
│  │  └──────────┘ └──────────┘ └──────────────┘    │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐    │    │
│  │  │ Auth &   │ │ Audit    │ │ Version      │    │    │
│  │  │ ACL      │ │ Logger   │ │ Manager      │    │    │
│  │  └──────────┘ └──────────┘ └──────────────┘    │    │
│  │  ┌──────────┐ ┌──────────────┐                  │    │
│  │  │ Feedback │ │ Lifecycle    │                  │    │
│  │  │ Collector│ │ Manager      │                  │    │
│  │  └──────────┘ └──────────────┘                  │    │
│  └─────────────────────┬───────────────────────────┘    │
│                        ▼                                 │
│  ┌──────────────────────────────────────────────────┐   │
│  │           ContextStore（URI 路由层）               │   │
│  │   ctx:// URI → PG 读写 + 向量库检索 + ACL 检查    │   │
│  └──────────┬──────────────────┬────────────────────┘   │
└─────────────┼──────────────────┼────────────────────────┘
              ▼                  ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐
│   PostgreSQL     │  │   向量库          │  │ Catalog      │
│                  │  │                  │  │ Connector    │
│ ┌──────────────┐ │  │ Chroma (开发)    │  │              │
│ │ contexts     │ │  │ Milvus (生产)    │  │ Hive/Iceberg/│
│ │ dependencies │ │  │                  │  │ Delta/Mock   │
│ │ change_events│ │  │ L0 embedding     │  │              │
│ │ table_meta.. │ │  │ + 标量过滤       │  │              │
│ │ lineage      │ │  │                  │  │              │
│ │ table_rels.. │ │  │                  │  │              │
│ │ query_templ..│ │  │                  │  │              │
│ │ skill_vers.. │ │  │                  │  │              │
│ │ access_pol.. │ │  │                  │  │              │
│ │ audit_log    │ │  │                  │  │              │
│ │ team_member..│ │  │                  │  │              │
│ │ lifecycle_.. │ │  │                  │  │              │
│ │ context_fb.. │ │  │                  │  │              │
│ └──────────────┘ │  │                  │  │              │
│                  │  │                  │  │              │
│ LISTEN/NOTIFY    │  │                  │  │              │
│ RLS (租户隔离)   │  │                  │  │              │
│ ACID 事务        │  │                  │  │              │
│ 递归 CTE (血缘)  │  │                  │  │              │
└──────────────────┘  └──────────────────┘  └──────────────┘
```

## 与原方案的关键区别

| 维度 | 原方案（三层存储抽象） | 新方案（PG 中心） |
|------|----------------------|-------------------|
| 内容存储 | ContentStore 接口（S3/LocalFS） | PG TEXT 列（TOAST 自动处理） |
| 元数据存储 | 文件名/路径/向量 DB 标量字段 | PG 结构化表 |
| 关系存储 | `.relations.json` / `.deps.json` 文件 | PG `dependencies` / `table_relationships` 表 |
| 事件系统 | append-only JSON / Redis Streams | PG `change_events` 表 + `LISTEN/NOTIFY` |
| 权限 | 应用层路径前缀检查 | PG RLS + `access_policies` 表 |
| 审计 | append-only JSON 文件 | PG `audit_log` 表（事务内写入） |
| 事务保证 | 无（内容和元数据可能不一致） | ACID（内容、元数据、事件在同一事务中） |

## DataAgent 层说明

MVP 阶段采用单 OpenClaw 实例作为 DataAgent 运行时。通过 ContextHub OpenClaw Plugin 对接 ContextHub Server，模式与 OpenViking 的 openclaw-memory-plugin 一致：

- Plugin 注册 tools（`contexthub_search`、`contexthub_store`、`contexthub_promote` 等）供 Agent 调用
- Plugin 通过 lifecycle hooks（`before_agent_start`、`agent_end`）实现自动上下文注入和反馈采集
- 多 Agent 协作通过 SDK 调用时切换 `agent_id` 参数实现，ContextHub Server 端按 `agent_id` 做隔离和协作
- 协作逻辑（传播、ACL、记忆晋升）全部在 Server 端闭环，不依赖多个 Agent 运行时实例

## 核心模块职责

| 模块 | 职责 | 依赖的 PG 表 |
|------|------|-------------|
| Context Service | 上下文 CRUD、L0/L1/L2 管理 | `contexts` |
| Memory Service | 记忆提取、去重、热度管理、共享/提升 | `contexts`, `dependencies` |
| Skill Service | Skill 定义、版本管理、发布/订阅 | `contexts`, `skill_versions`, `skill_subscriptions` |
| Retrieval Engine | 向量检索 L0 → PG 读 L1 精排 → 按需加载 L2 | 向量库 + `contexts` |
| Indexer | 内容变更时异步生成 L0/L1、更新向量索引 | `contexts` → 向量库 |
| Propagation Engine | 监听 NOTIFY → 查依赖方 → 执行规则 | `change_events`, `dependencies` |
| Auth & ACL | 认证、RBAC、资源级权限、字段脱敏 | `access_policies`, `team_memberships` |
| Audit Logger | 操作审计、上下文溯源 | `audit_log` |
| Version Manager | Skill 版本管理 | `skill_versions` |
| Feedback Collector | 隐式反馈采集、质量评分计算 | `context_feedback`, `contexts` |
| Lifecycle Manager | 状态机、定期归档/清理、湖表同步删除 | `contexts`, `lifecycle_policies` |
| ContextStore | URI 路由层：ctx:// → PG 读写 + ACL 检查 | 所有表 |
| CatalogConnector | 数据目录抽象（Hive/Iceberg/Delta/Mock） | `table_metadata`, `lineage` |

## 数据流

### 写入流程

```
Agent 写入 ctx://agent/bot/memories/cases/sql-001
    │
    ▼
ContextStore.write()
    │
    ├─ 1. ACL 检查（查 access_policies 表）
    │
    ├─ 2. PG 事务 {
    │      INSERT/UPDATE contexts 表
    │      INSERT dependencies 表（自动注册依赖）
    │      INSERT change_events 表
    │      INSERT audit_log 表
    │  }
    │
    ├─ 3. PG NOTIFY 'context_changed'
    │
    └─ 4. 异步：Indexer 生成 L0 embedding → 写入向量库
```

### 检索流程

```
Agent 搜索 "月度销售额统计"
    │
    ▼
ContextStore.search()
    │
    ├─ 1. 向量库：L0 embedding 语义匹配 + 标量过滤 → top-K URI
    │
    ├─ 2. PG：批量读取 L1 内容 → Rerank
    │
    ├─ 3. ACL 过滤 + 字段脱敏
    │
    └─ 4. 返回结果（按需可继续加载 L2）
```

### 变更传播流程

```
CatalogConnector 检测到 orders 表 schema 变更
    │
    ▼
PG 事务 {
    UPDATE table_metadata SET ddl = ...
    UPDATE contexts SET version = version + 1
    INSERT change_events
}
    │
    ▼
PG NOTIFY 'context_changed'
    │
    ▼
Propagation Engine 被唤醒
    │
    ├─ SELECT FROM dependencies WHERE target_uri = 'ctx://datalake/.../orders'
    │
    ├─ 对每个依赖方执行 PropagationRule
    │     └─ TableSchemaRule → auto_update（重新生成 L0/L1）
    │     └─ SkillVersionRule → mark_stale（如果是 breaking）
    │
    └─ UPDATE contexts SET status = 'stale' WHERE uri IN (...)
```
