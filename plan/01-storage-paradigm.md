# 01 — 统一存储范式：对外文件语义，对内智能存储

## 核心理念

借鉴 OpenViking 的"一切皆文件"范式作为 **对外接口**——Agent 通过 `ctx://` URI 读写上下文，感知到的是文件语义。但对内不再用文件系统存储，而是按数据特性路由到合适的存储引擎：

```
对外接口：ctx:// URI + 文件语义（read/write/list/search）
对内存储：PG（元数据 + 内容） + 向量库（embedding） + CatalogConnector（外部数据源）
```

### 为什么不用文件系统

ContextHub 的核心价值在变更传播、多 Agent 协作、权限治理——这些需要事务一致性、关系查询、事件通知。文件系统是"哑"存储，这些能力全部要在应用层重建。PG 原生提供：

| 能力 | PG 机制 | 文件系统替代方案 |
|------|---------|-----------------|
| 变更传播 | `LISTEN/NOTIFY` + 触发器 | 自建事件队列 + 轮询 |
| 依赖关系查询 | `JOIN` + 递归 CTE | 解析 `.deps.json` 文件 |
| 事务一致性 | ACID 事务 | 无（内容和元数据可能不一致） |
| 权限隔离 | 行级安全策略（RLS） | 应用层路径前缀检查 |
| 版本管理 | 行版本 + 历史表 | 文件复制 + 命名约定 |
| 审计日志 | 触发器自动记录 | 手动写 append-only 文件 |

### 设计原则

- **URI 是逻辑地址，不是物理路径**：`ctx://datalake/prod/orders` 不对应磁盘上的某个目录，而是 PG `contexts` 表中的一行
- **元数据和内容同库**：L0/L1/L2 内容存在 PG TEXT 列中（TOAST 自动处理大文本），与元数据在同一个事务中更新
- **结构化数据拆表存**：数据湖表的 DDL、血缘、查询模板等不塞进一个 TEXT blob，而是拆解为独立的 PG 表（详见 03-datalake-management.md）

## URI 命名空间

URI 作为 Agent 的统一寻址接口保持不变：

```
ctx://
├── datalake/{catalog}/{db}/{table}/      # 数据湖表
├── resources/{project}/                  # 文档资源
├── team/                                 # 共享空间（根 = 全组织）
│   ├── memories/                         # 全组织共享记忆
│   │   ├── business_rules/
│   │   └── data_dictionary/
│   ├── skills/                           # 全组织共享 skills
│   ├── engineering/                      # 工程部（子团队）
│   │   ├── memories/
│   │   ├── skills/
│   │   ├── backend/                      # 后端组（子子团队）
│   │   └── data/                         # 数据组
│   └── sales/                            # 销售部
├── agent/{agent_id}/                     # Agent 私有空间
│   ├── memories/
│   │   ├── cases/
│   │   └── patterns/
│   └── skills/
└── user/{user_id}/                       # 用户空间
    └── memories/
        ├── profile/
        ├── preferences/
        ├── entities/
        └── events/
```

## PG 核心表结构

### contexts 表（通用上下文）

```sql
CREATE TABLE contexts (
    uri             TEXT PRIMARY KEY,       -- ctx://datalake/prod/orders
    context_type    TEXT NOT NULL,           -- 'table_schema' | 'skill' | 'memory' | 'resource'
    scope           TEXT NOT NULL,           -- 'datalake' | 'team' | 'agent' | 'user'
    owner_space     TEXT,                    -- 团队路径如 'engineering/backend'，或 agent_id
    account_id      TEXT NOT NULL,           -- 租户隔离

    -- L0/L1/L2 内容（TOAST 自动处理大文本）
    l0_content      TEXT,                    -- ~100 tokens 摘要
    l1_content      TEXT,                    -- ~2k tokens 概览
    l2_content      TEXT,                    -- 完整内容（非 datalake 类型使用）

    -- 元数据
    status          TEXT DEFAULT 'active',   -- active | stale | archived | deleted
    version         INT DEFAULT 1,
    tags            TEXT[],
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,

    -- 热度与质量
    active_count    INT DEFAULT 0,
    adopted_count   INT DEFAULT 0,
    ignored_count   INT DEFAULT 0
);

-- 租户隔离（RLS）
ALTER TABLE contexts ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contexts
    USING (account_id = current_setting('app.account_id'));

-- 常用索引
CREATE INDEX idx_contexts_scope ON contexts (scope, context_type);
CREATE INDEX idx_contexts_owner ON contexts (account_id, owner_space);
CREATE INDEX idx_contexts_status ON contexts (status) WHERE status != 'deleted';
```

### dependencies 表（替代 .deps.json）

```sql
CREATE TABLE dependencies (
    id              SERIAL PRIMARY KEY,
    source_uri      TEXT NOT NULL REFERENCES contexts(uri),  -- 依赖方
    target_uri      TEXT NOT NULL,                           -- 被依赖方
    dep_type        TEXT NOT NULL,           -- 'skill_version' | 'table_schema' | 'derived_from'
    pinned_version  TEXT,                    -- 依赖的特定版本（如 Skill v2）
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_uri, target_uri, dep_type)
);

CREATE INDEX idx_deps_target ON dependencies (target_uri);  -- 变更传播时按 target 查依赖方
```

### change_events 表（替代 Event Log）

```sql
CREATE TABLE change_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    source_uri      TEXT NOT NULL,
    change_type     TEXT NOT NULL,           -- 'created' | 'modified' | 'deleted' | 'version_published'
    actor           TEXT NOT NULL,           -- agent_id | 'system' | 'catalog_sync'
    diff_summary    TEXT,                    -- ~50 tokens
    previous_version TEXT,
    new_version     TEXT,
    metadata        JSONB,
    processed       BOOLEAN DEFAULT FALSE    -- 传播引擎是否已处理
);

CREATE INDEX idx_events_unprocessed ON change_events (timestamp) WHERE NOT processed;
```

### audit_log 表

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,           -- 'read' | 'write' | 'delete' | 'search' | 'promote'
    resource_uri    TEXT,
    context_used    TEXT[],                  -- 本次操作引用了哪些上下文
    result          TEXT NOT NULL,           -- 'success' | 'denied' | 'error'
    metadata        JSONB
);
```

### access_policies 表

```sql
CREATE TABLE access_policies (
    id              SERIAL PRIMARY KEY,
    resource_uri_pattern TEXT NOT NULL,      -- 如 'ctx://datalake/prod/*'
    principal       TEXT NOT NULL,           -- agent_id | team_path | role
    effect          TEXT NOT NULL,           -- 'allow' | 'deny'
    actions         TEXT[] NOT NULL,         -- {'read', 'write', 'admin'}
    conditions      JSONB,                  -- 附加条件
    field_masks     TEXT[],                 -- 需要脱敏的字段路径
    priority        INT DEFAULT 0,
    account_id      TEXT NOT NULL
);
```

## 向量索引层

L0 摘要被向量化后存入向量数据库，用于语义检索。PG 是 source of truth，向量库是检索加速层。

| 数据 | 存储位置 | 说明 |
|------|----------|------|
| URI、元数据、L0/L1/L2 内容、状态、版本 | PG | 权威数据源，支持事务 |
| L0 embedding + 标量过滤字段 | 向量库 | 检索加速，可从 PG 重建 |

### 向量 DB 记录字段

```
向量 DB 记录 = {
    id:            md5(account_id:uri)
    uri:           "ctx://datalake/prod/orders"
    vector:        [0.12, -0.34, ...]      # L0 摘要的 dense embedding
    sparse_vector: {...}                   # 可选
    context_type:  "table_schema"          # table_schema | memory | skill | resource
    level:         0                       # 向量库只存 L0
    parent_uri:    "ctx://datalake/prod/"
    account_id:    "acme"                  # 租户隔离
    owner_space:   "engineering/backend"   # 团队路径（用于权限过滤）
    name:          "orders"
    abstract:      "orders 表 - 存储所有订单交易记录..."
    tags:          "datalake,orders,交易"
    active_count:  42                      # 热度
    updated_at:    "2026-03-18T..."
}
```

**与 OpenViking 的区别：** OpenViking 将 L0/L1/L2 三个层级都入向量库（通过 `level` 字段区分）。ContextHub 只将 L0 入向量库——L1/L2 内容在 PG 中，通过 URI 直接读取。原因：向量检索的目的是找到相关上下文（L0 足够），精排和详情加载直接查 PG（更快、更一致）。

### 检索流程

```
用户问题："上个月销售额是多少？"

1. 意图分析 → TypedQuery: {query: "月度销售额统计", context_type: "table_schema", scope: "datalake"}
2. 向量检索（只搜 L0 embedding）+ 标量过滤（context_type, owner_space, account_id）→ top-K URI
3. 从 PG 读取候选的 L1 内容 → Rerank
4. 按需从 PG 加载 L2 / 关联的结构化数据（DDL、血缘、查询模板）
```

## 关系：用 PG 表存储

关系存储在 PG 的 `dependencies` 和 `table_relationships` 表中（替代 `.relations.json` 和 `.deps.json` 文件）。

| 关系类型 | PG 表 | 查询方式 |
|----------|-------|----------|
| 上下文依赖（Skill 版本、表 schema） | `dependencies` | `SELECT * FROM dependencies WHERE target_uri = $1` |
| 表间 JOIN 关系 | `table_relationships`（见 03） | SQL JOIN |
| 数据血缘 | `lineage`（见 03） | 递归 CTE 遍历 |
| Skill 订阅 | `skill_subscriptions`（见 04） | 标量查询 |

**优势：**
- 变更传播时查依赖方：一条 SQL，不需要遍历文件系统
- 血缘图遍历：PG 递归 CTE 原生支持多跳查询
- 事务保证：依赖注册和内容更新在同一个事务中，不会出现"内容更新了但依赖没注册"的不一致

## 可见性与权限规则

### 可见性（逻辑继承，PG 查询实现）

```
Agent 所属团队路径: team/engineering/backend

该 Agent 可见的上下文（从私有到全局）:
  1. ctx://agent/{self}/              ← 私有空间
  2. ctx://user/{user_id}/            ← 所服务用户的记忆
  3. ctx://team/engineering/backend/  ← 所属团队
  4. ctx://team/engineering/          ← 上级团队（自动继承）
  5. ctx://team/                      ← 根团队 = 全组织（自动继承）
  6. ctx://datalake/                  ← 数据湖（受 ACL 控制）
  7. ctx://resources/                 ← 文档资源（受 ACL 控制）
```

实现方式：PG 查询时通过 `owner_space` 前缀匹配实现层级继承：

```sql
-- Agent 属于 engineering/backend，可见自己团队及所有上级团队的内容
SELECT * FROM contexts
WHERE account_id = $1
  AND (
    owner_space = 'engineering/backend'           -- 本团队
    OR owner_space = 'engineering'                 -- 上级
    OR owner_space = '' OR owner_space IS NULL     -- 根团队
    OR scope IN ('datalake', 'resources')          -- 公共资源（受 ACL 进一步控制）
    OR (scope = 'agent' AND owner_space = $agent_id)  -- 私有空间
  );
```

### 写权限

| 范围 | 谁可以写 |
|------|----------|
| `ctx://agent/{id}/` | 该 Agent 自己 |
| `ctx://team/.../` 某层级 | 该层级的成员（或管理员） |
| `ctx://team/` 根 | 组织管理员 |

### 跨团队共享

- 方案 1：提升到共同祖先 `ctx://team/` — 简单但范围过大
- 方案 2：通过 `dependencies` 表建立跨团队引用 — 精准但需要权限（更合理）

### Agent 多团队归属

```sql
CREATE TABLE team_memberships (
    agent_id    TEXT NOT NULL,
    team_path   TEXT NOT NULL,          -- 如 'engineering/backend'
    role        TEXT DEFAULT 'member',  -- 'member' | 'admin'
    access      TEXT DEFAULT 'read_write', -- 'read_write' | 'read_only'
    is_primary  BOOLEAN DEFAULT FALSE,  -- 主团队（写入共享记忆的默认目标）
    PRIMARY KEY (agent_id, team_path)
);
```

- 主团队（primary）：Agent 写入共享记忆的默认目标
- 附属团队（secondary）：只读访问其他团队的共享上下文
- 类似 Unix 的主组 + 附属组

## ctx:// URI 路由层

Agent 看到的是 `ctx://` URI 和文件语义操作，ContextHub 内部将其路由到 PG：

```python
class ContextStore:
    """对外暴露文件语义，对内路由到 PG"""

    async def read(self, uri: str, level: ContextLevel, ctx: RequestContext) -> str:
        # 1. 权限检查（PG access_policies 表）
        await self.acl.check_access(uri, ctx, action='read')
        # 2. 从 PG 读取对应层级的内容
        row = await self.db.fetchrow(
            "SELECT l0_content, l1_content, l2_content FROM contexts WHERE uri = $1", uri)
        content = row[f'l{level.value}_content']
        # 3. 字段脱敏（如有）
        return await self.acl.apply_field_masks(content, uri, ctx)

    async def write(self, uri: str, level: ContextLevel, content: str, ctx: RequestContext):
        async with self.db.transaction():
            # 1. 权限检查
            await self.acl.check_access(uri, ctx, action='write')
            # 2. 写入 PG（内容 + 元数据在同一个事务中）
            await self.db.execute(
                f"UPDATE contexts SET l{level.value}_content = $1, version = version + 1, "
                f"updated_at = NOW() WHERE uri = $2", content, uri)
            # 3. 发出变更事件（同一事务内）
            await self.db.execute(
                "INSERT INTO change_events (source_uri, change_type, actor) VALUES ($1, 'modified', $2)",
                uri, ctx.agent_id)
        # 4. 事务提交后，PG NOTIFY 触发传播引擎（异步）
        await self.db.execute("NOTIFY context_changed, $1", uri)

    async def search(self, query: str, ctx: RequestContext, **filters) -> list[Context]:
        # 1. 向量库检索 L0 → top-K URI
        uris = await self.vector_store.search(query, account_id=ctx.account_id, **filters)
        # 2. 从 PG 批量读取 L1 内容 → Rerank
        rows = await self.db.fetch(
            "SELECT * FROM contexts WHERE uri = ANY($1) AND status = 'active'", uris)
        # 3. 权限过滤 + 字段脱敏
        return await self.acl.filter_and_mask(rows, ctx)
```

## LLM Tool 接口层：文件语义的 tool use 包装

### 问题

LLM（Claude、GPT 等）在 agentic 场景下习惯文件系统式的探索——ls 看目录、cat 读文件、grep 搜内容。这是训练数据中最常见的交互模式。ContextHub 底层用 PG，但暴露给 LLM 的工具接口需要符合这个直觉。

### 两种消费模式

| 模式 | 调用方 | 接口 | 适用场景 |
|------|--------|------|----------|
| SDK 调用 | Agent 编排代码（Python） | `ctx.search()` / `ctx.read()` | 编排代码知道要什么，直接检索 |
| Tool use 探索 | LLM 自主决策 | `ls` / `read` / `grep` / `search` | LLM 需要自主浏览上下文空间 |

两条路径后面都是同一个 ContextStore → PG，不矛盾。

### Tool 定义（注册为 LLM function calling 工具）

```python
class ContextTools:
    """暴露给 LLM 的 tool use 接口，模拟文件语义，底层全部走 PG"""

    def __init__(self, store: ContextStore):
        self.store = store

    async def ls(self, path: str) -> list[str]:
        """列出路径下的内容（模拟目录浏览）

        示例：
          ls("ctx://datalake/hive/prod/") → ["orders", "users", "products"]
          ls("ctx://team/")               → ["memories/", "skills/", "engineering/", "sales/"]
          ls("ctx://agent/bot-1/")        → ["memories/", "skills/"]
        """
        prefix = path.rstrip('/') + '/'
        rows = await self.store.db.fetch("""
            SELECT DISTINCT
                split_part(substring(uri FROM length($1) + 1), '/', 1) AS child
            FROM contexts
            WHERE uri LIKE $1 || '%'
              AND uri != $1
              AND account_id = $2
              AND status = 'active'
        """, prefix, self.store.current_ctx.account_id)
        return [r['child'] for r in rows]

    async def read(self, uri: str, level: str = "L1") -> str:
        """读取上下文内容

        level:
          L0 = 一句话摘要（~100 tokens）
          L1 = 概览（schema、字段说明，~2k tokens）
          L2 = 完整详情（DDL、血缘、查询模板）
        """
        lvl = ContextLevel[level]
        return await self.store.read(uri, lvl, self.store.current_ctx)

    async def grep(self, query: str, scope: str = None) -> list[dict]:
        """语义搜索（向量检索 + 精排）

        scope: 'datalake' | 'team' | 'agent' | 'user' | None(全部)
        返回按相关性排序的上下文列表，每项包含 uri + L0 摘要
        """
        results = await self.store.search(query, self.store.current_ctx, scope=scope)
        return [{"uri": r.uri, "abstract": r.l0_content, "type": r.context_type} for r in results]

    async def stat(self, uri: str) -> dict:
        """查看上下文的元信息（不读内容）"""
        row = await self.store.db.fetchrow("""
            SELECT uri, context_type, scope, owner_space, status, version,
                   active_count, updated_at
            FROM contexts WHERE uri = $1
        """, uri)
        return dict(row) if row else {"error": "Not found"}
```

### LLM 交互示例

```
User: "帮我看看有哪些数据湖表跟销售相关"

LLM → tool_call: ls("ctx://datalake/hive/prod/")
     ← ["orders", "users", "products", "sales_daily", "refunds"]

LLM → tool_call: read("ctx://datalake/hive/prod/orders", level="L0")
     ← "orders 表 - 存储所有订单交易记录，包含订单金额、状态、时间等"

LLM → tool_call: read("ctx://datalake/hive/prod/sales_daily", level="L0")
     ← "sales_daily 表 - 按天汇总的销售数据，包含日期、渠道、金额"

LLM → tool_call: read("ctx://datalake/hive/prod/orders", level="L1")
     ← "## Schema\n| 字段 | 类型 | 说明 |\n| order_id | BIGINT | 订单ID..."

LLM: "有两张相关表：orders（订单明细，可按时间范围和状态筛选）
      和 sales_daily（日汇总，适合趋势分析）。建议用 sales_daily 做月度统计。"
```

LLM 觉得自己在浏览文件系统，实际上每一步都是 PG 查询。

### 注册为 function calling 工具

```python
# FastAPI 端点 + OpenAI/Claude function calling schema
CONTEXT_TOOLS_SCHEMA = [
    {
        "name": "ls",
        "description": "列出 ctx:// 路径下的子项。类似文件系统的 ls 命令。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ctx:// 路径，如 ctx://datalake/hive/prod/"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read",
        "description": "读取上下文内容。level=L0 看摘要，L1 看概览，L2 看完整详情。",
        "parameters": {
            "type": "object",
            "properties": {
                "uri":   {"type": "string", "description": "上下文 URI，如 ctx://datalake/hive/prod/orders"},
                "level": {"type": "string", "enum": ["L0", "L1", "L2"], "default": "L1"}
            },
            "required": ["uri"]
        }
    },
    {
        "name": "grep",
        "description": "语义搜索上下文。输入自然语言查询，返回最相关的上下文列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询，如 '月度销售额统计'"},
                "scope": {"type": "string", "enum": ["datalake", "team", "agent", "user"],
                          "description": "限定搜索范围（可选）"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "stat",
        "description": "查看上下文的元信息：类型、状态、版本、热度等。不返回内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "上下文 URI"}
            },
            "required": ["uri"]
        }
    }
]
```
