# 10 — 代码架构设计

基于 01-09 设计文档，解决从设计到代码的 5 个关键缺失：项目结构、依赖注入、API 端点、向量库抽象、L0/L1 生成。

---

## 一、项目目录结构

```
contexthub/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── src/
│   └── contexthub/
│       ├── __init__.py
│       ├── main.py                    # FastAPI app 工厂 + lifespan
│       ├── config.py                  # pydantic-settings
│       │
│       ├── models/                    # Pydantic 数据模型
│       │   ├── context.py             # Context, ContextLevel, ContextType, ContextStatus
│       │   ├── request.py             # RequestContext, SearchRequest/Response
│       │   ├── datalake.py            # TableMetadata, Lineage, TableRelationship, QueryTemplate
│       │   ├── skill.py               # SkillVersion, SkillSubscription
│       │   ├── memory.py              # ContextFeedback, MemoryCategory
│       │   ├── access.py              # AccessPolicy
│       │   └── team.py                # TeamMembership
│       │
│       ├── db/                        # 数据库层
│       │   ├── pool.py                # asyncpg 连接池（create_pool / close_pool）
│       │   ├── repository.py          # PgRepository：封装 asyncpg，提供 fetch/execute/transaction
│       │   └── queries/               # 原始 SQL 常量（按领域分文件）
│       │       ├── contexts.py
│       │       ├── dependencies.py
│       │       ├── events.py
│       │       ├── datalake.py
│       │       ├── skills.py
│       │       ├── access.py
│       │       ├── audit.py
│       │       ├── feedback.py
│       │       ├── lifecycle.py
│       │       └── teams.py
│       │
│       ├── store/                     # URI 路由层
│       │   └── context_store.py       # ContextStore: read/write/search/ls/stat
│       │
│       ├── services/                  # 业务服务层
│       │   ├── context_service.py     # CRUD、L0/L1/L2 管理
│       │   ├── memory_service.py      # 提取、去重、热度、共享提升
│       │   ├── skill_service.py       # 版本管理、发布/订阅
│       │   ├── retrieval_service.py   # 向量检索 L0 → PG 读 L1 rerank → 加载 L2
│       │   ├── indexer_service.py     # 异步 L0/L1 生成 + 向量索引更新
│       │   ├── propagation_engine.py  # PG LISTEN/NOTIFY → 查依赖 → 执行规则
│       │   ├── acl_service.py         # check_access / filter_and_mask
│       │   ├── audit_service.py       # 写审计日志
│       │   ├── feedback_service.py    # 反馈采集、质量评分
│       │   ├── lifecycle_service.py   # 状态机、定时归档
│       │   └── catalog_sync_service.py # CatalogConnector → PG 同步
│       │
│       ├── propagation/               # 变更传播规则
│       │   ├── base.py                # PropagationRule ABC, PropagationAction
│       │   ├── skill_version_rule.py  # Level 1: 纯规则
│       │   ├── table_schema_rule.py   # Level 1/2: 自动重新生成
│       │   ├── derived_memory_rule.py # Level 2: 模板替换
│       │   ├── complex_rule.py        # Level 3: LLM 推理
│       │   └── registry.py            # dep_type → Rule 映射
│       │
│       ├── connectors/                # 外部数据源
│       │   ├── base.py                # CatalogConnector ABC
│       │   └── mock_connector.py      # 开发用 Mock
│       │
│       ├── vector/                    # 向量库抽象
│       │   ├── base.py                # VectorStore ABC
│       │   ├── chroma_store.py        # ChromaDB 实现
│       │   └── factory.py             # create_vector_store()
│       │
│       ├── llm/                       # LLM 调用抽象
│       │   ├── base.py                # LLMClient ABC + EmbeddingClient ABC
│       │   ├── openai_client.py       # OpenAI embedding + chat
│       │   └── factory.py             # create_llm_client() / create_embedding_client()
│       │
│       ├── generation/                # L0/L1 内容生成
│       │   ├── base.py                # ContentGenerator + GenerationStrategy ABC
│       │   ├── table_schema.py        # TableSchemaStrategy（LLM）
│       │   ├── skill.py               # SkillStrategy（纯模板）
│       │   ├── memory.py              # MemoryStrategy（模板 + 可选 LLM）
│       │   └── resource.py            # ResourceStrategy（LLM）
│       │
│       └── api/                       # FastAPI 路由
│           ├── deps.py                # Depends 工厂函数
│           ├── middleware.py           # 认证中间件
│           └── routers/
│               ├── contexts.py        # /api/v1/contexts
│               ├── search.py          # /api/v1/search
│               ├── memories.py        # /api/v1/memories
│               ├── skills.py          # /api/v1/skills
│               ├── datalake.py        # /api/v1/datalake
│               ├── tools.py           # /api/v1/tools（LLM tool use: ls/read/grep/stat）
│               └── admin.py           # /api/v1/admin
└── tests/
    ├── conftest.py
    ├── test_context_store.py
    ├── test_retrieval.py
    ├── test_propagation.py
    └── ...
```

模块边界原则：
- `models/` 纯数据定义，无业务逻辑，无 IO
- `db/` 只做 SQL 执行，不含业务判断
- `store/` 是 URI 路由层，协调 PG + 向量库 + ACL
- `services/` 是业务逻辑层，依赖 `store/` 和 `db/`
- `api/` 只做 HTTP 协议转换，不含业务逻辑

---

## 二、依赖注入与服务装配

### 2.1 config.py

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = {"env_prefix": "CTX_", "env_file": ".env"}

    # PostgreSQL
    pg_dsn: str = "postgresql://ctx:ctx@localhost:5432/contexthub"
    pg_min_pool: int = 5
    pg_max_pool: int = 20

    # 向量库
    vector_backend: str = "chroma"       # "chroma" | "milvus"
    chroma_persist_dir: str = ".chroma_data"

    # LLM
    llm_backend: str = "openai"          # "openai" | "anthropic"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # CatalogConnector
    catalog_backend: str = "mock"        # "mock" | "hive" | "iceberg"

    # 传播引擎
    propagation_enabled: bool = True
```

### 2.2 服务依赖图

```
Settings → asyncpg.Pool → PgRepository
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         VectorStore     LLMClient     CatalogConnector
         EmbeddingClient
              │               │
              ▼               ▼
         ACLService      ContentGenerator
         AuditService    (table_schema/skill/memory/resource strategies)
              │               │
              ▼               ▼
         ContextStore    IndexerService
              │               │
    ┌─────┬──┴──┬─────┐      │
    ▼     ▼     ▼     ▼      ▼
 Context Memory Skill Retrieval  PropagationEngine
 Service Service Service Service  (background task)
```

### 2.3 main.py — App Factory + Lifespan

所有 service 在 lifespan 中一次性装配，通过构造函数注入依赖。不使用 DI 框架。

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()

    # 基础设施
    pool = await create_pool(settings)
    repo = PgRepository(pool)
    vector_store = create_vector_store(settings)
    await vector_store.initialize()
    llm_client = create_llm_client(settings)
    embedding_client = create_embedding_client(settings)
    catalog_connector = create_catalog_connector(settings)

    # 横切关注点
    acl_service = ACLService(repo)
    audit_service = AuditService(repo)

    # 核心层
    content_generator = ContentGenerator(llm_client)
    context_store = ContextStore(repo, vector_store, acl_service, audit_service)
    indexer_service = IndexerService(repo, vector_store, embedding_client, content_generator)
    retrieval_service = RetrievalService(repo, vector_store, embedding_client, acl_service)

    # 业务服务
    context_service = ContextService(repo, context_store, indexer_service)
    memory_service = MemoryService(repo, context_store, indexer_service)
    skill_service = SkillService(repo, context_store, indexer_service)
    catalog_sync_service = CatalogSyncService(repo, catalog_connector, indexer_service, llm_client)

    # 传播引擎（后台 asyncio task）
    rule_registry = PropagationRuleRegistry.default(indexer_service, llm_client)
    propagation_engine = PropagationEngine(settings.pg_dsn, pool, rule_registry)

    # 挂到 app.state，供 Depends 使用
    app.state.context_service = context_service
    app.state.memory_service = memory_service
    app.state.skill_service = skill_service
    app.state.retrieval_service = retrieval_service
    # ... 其余 service 同理

    if settings.propagation_enabled:
        await propagation_engine.start()

    yield

    await propagation_engine.stop()
    await pool.close()
```

### 2.4 api/deps.py — Depends 链

```python
async def get_request_context(
    x_account_id: str = Header(...),
    x_agent_id: str = Header(...),
) -> RequestContext:
    return RequestContext(account_id=x_account_id, agent_id=x_agent_id)

def get_context_service(request: Request) -> ContextService:
    return request.app.state.context_service

# 其余 service 同理：get_memory_service, get_skill_service, ...
```

Endpoint 使用方式：
```python
@router.post("/contexts", status_code=201)
async def create_context(
    body: CreateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    svc: ContextService = Depends(get_context_service),
):
    return await svc.create(body, ctx)
```

### 2.5 PropagationEngine 后台任务

- 用独立的 asyncpg 连接做 `LISTEN`（不能用连接池，LISTEN 需要长连接）
- `_on_notify` 回调中用 debounce（2 秒窗口）合并同一 URI 的多次通知
- `process_event` 内部捕获异常，单个事件失败不影响整体
- lifespan shutdown 时 `cancel()` task + 关闭 LISTEN 连接

```python
class PropagationEngine:
    async def start(self):
        self._listen_conn = await asyncpg.connect(self._dsn)
        await self._listen_conn.add_listener("context_changed", self._on_notify)

    def _on_notify(self, conn, pid, channel, payload):
        # debounce: 2 秒内同一 URI 只处理一次
        source_uri = payload
        if source_uri in self._pending:
            self._pending[source_uri].cancel()
        loop = asyncio.get_running_loop()
        self._pending[source_uri] = loop.call_later(
            2.0, lambda: asyncio.create_task(self._safe_process(source_uri))
        )

    async def process_event(self, source_uri):
        # 1. 读取未处理的 change_event
        # 2. 查询 dependencies WHERE target_uri = source_uri
        # 3. 对每个依赖方执行 registry.get(dep_type).evaluate()
        # 4. 执行 action: mark_stale / auto_update / notify
        # 5. 标记事件已处理

    async def stop(self):
        if self._listen_conn:
            await self._listen_conn.close()
```

---

## 三、FastAPI API 端点设计

### 完整路由表

| 方法 | 路径 | 功能 | Router |
|------|------|------|--------|
| **上下文 CRUD** |
| `POST` | `/api/v1/contexts` | 创建上下文 | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}` | 读取（query: `level=L0\|L1\|L2`） | contexts.py |
| `PATCH` | `/api/v1/contexts/{uri:path}` | 更新内容 | contexts.py |
| `DELETE` | `/api/v1/contexts/{uri:path}` | 标记删除 | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/stat` | 元信息（不含内容） | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/children` | 列出子项（ls 语义） | contexts.py |
| `GET` | `/api/v1/contexts/{uri:path}/deps` | 查看依赖关系 | contexts.py |
| **语义检索** |
| `POST` | `/api/v1/search` | 向量检索 + Rerank | search.py |
| `POST` | `/api/v1/search/sql-context` | Text-to-SQL 上下文组装 | search.py |
| **记忆** |
| `POST` | `/api/v1/memories` | 添加记忆 | memories.py |
| `GET` | `/api/v1/memories` | 列出记忆（query: `category`, `scope`） | memories.py |
| `POST` | `/api/v1/memories/promote` | 提升为团队共享 | memories.py |
| **技能** |
| `POST` | `/api/v1/skills/versions` | 发布新版本 | skills.py |
| `GET` | `/api/v1/skills/{uri:path}/versions` | 版本历史 | skills.py |
| `POST` | `/api/v1/skills/subscribe` | 订阅技能 | skills.py |
| **反馈** |
| `POST` | `/api/v1/feedback` | 记录上下文反馈 | search.py |
| **数据湖** |
| `POST` | `/api/v1/datalake/sync` | 触发 catalog 同步 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}` | 列出表 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}` | 表完整上下文 | datalake.py |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}/lineage` | 血缘查询 | datalake.py |
| **LLM Tool Use** |
| `POST` | `/api/v1/tools/ls` | 列目录 | tools.py |
| `POST` | `/api/v1/tools/read` | 读内容 | tools.py |
| `POST` | `/api/v1/tools/grep` | 语义搜索 | tools.py |
| `POST` | `/api/v1/tools/stat` | 元信息 | tools.py |
| **管理** |
| `GET` | `/api/v1/admin/quality-report` | 低质量上下文报告 | admin.py |
| `POST` | `/api/v1/admin/lifecycle/run` | 手动触发生命周期 | admin.py |
| `GET` | `/api/v1/admin/propagation/status` | 传播引擎状态 | admin.py |

### 认证方式

通过 HTTP Headers 传递身份信息（MVP 阶段简化）：
- `X-API-Key`: API 密钥
- `X-Account-Id`: 租户 ID
- `X-Agent-Id`: Agent 标识

---

## 四、VectorStore 抽象接口

### 核心数据类型

```python
@dataclass(frozen=True)
class VectorRecord:
    uri: str
    account_id: str
    vector: list[float]          # L0 dense embedding
    context_type: str            # table_schema | memory | skill | resource
    parent_uri: str
    owner_space: str
    name: str
    abstract: str                # L0 摘要文本
    tags: str                    # 逗号分隔
    active_count: int = 0
    updated_at: datetime

    @property
    def id(self) -> str:
        """确定性 ID：md5(account_id:uri)，保证幂等写入"""
        return hashlib.md5(f"{self.account_id}:{self.uri}".encode()).hexdigest()

@dataclass
class VectorSearchParams:
    query_vector: list[float]
    account_id: str              # 必须：租户隔离
    top_k: int = 20
    context_type: str | None = None
    scope_owner_spaces: list[str] | None = None  # 可见性已展开的 owner_space 列表
    min_active_count: int | None = None

@dataclass
class VectorSearchResult:
    uri: str
    score: float
    abstract: str
    context_type: str
    owner_space: str
```

### VectorStore ABC

```python
class VectorStore(ABC):
    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def upsert(self, record: VectorRecord) -> None: ...

    @abstractmethod
    async def upsert_batch(self, records: list[VectorRecord]) -> None: ...

    @abstractmethod
    async def search(self, params: VectorSearchParams) -> list[VectorSearchResult]: ...

    @abstractmethod
    async def delete(self, uri: str, account_id: str) -> None: ...

    @abstractmethod
    async def delete_batch(self, uris: list[str], account_id: str) -> None: ...

    @abstractmethod
    async def get_count(self, account_id: str) -> int: ...

    # 非抽象方法：从 PG 全量重建向量索引（逻辑对所有后端一致）
    async def rebuild_from_pg(self, pool, account_id, embed_fn) -> int: ...

    @abstractmethod
    async def close(self) -> None: ...
```

### EmbeddingClient（独立于 LLMClient）

```python
class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dim(self) -> int: ...
```

### 交互流程

```
写入: IndexerService → EmbeddingClient.embed(l0) → VectorRecord → VectorStore.upsert()
检索: RetrievalService → EmbeddingClient.embed(query) → VectorStore.search() → top-K URI → PG 读 L1 rerank
删除: LifecycleService → VectorStore.delete() / delete_batch()
重建: 运维命令 → VectorStore.rebuild_from_pg()
```

---

## 五、L0/L1 生成实现

### 核心设计：ContentGenerator + Strategy 模式

```python
class ContentGenerator:
    """统一入口，根据 context_type 分派到对应策略"""
    def __init__(self, llm: LLMClient):
        self._strategies = {
            "table_schema": TableSchemaStrategy(llm),
            "skill":        SkillStrategy(),          # 无 LLM 依赖
            "memory":       MemoryStrategy(llm),
            "resource":     ResourceStrategy(llm),
        }

    async def generate(self, context_type: str, raw_content: str, metadata: dict) -> GeneratedContent:
        return await self._strategies[context_type].generate(raw_content, metadata)

@dataclass
class GeneratedContent:
    l0: str                     # ~100 tokens
    l1: str                     # ~2k tokens
    llm_tokens_used: int        # 纯模板为 0
```

### 各类型生成策略

| context_type | L0 生成 | L1 生成 | LLM 调用 | Token 消耗 |
|---|---|---|---|---|
| `table_schema` | LLM：DDL+字段注释 → 一句话业务描述 | 模板拼 schema 表格 + LLM 生成查询模式建议 | 是 | ~500 |
| `skill` | 模板：提取 Markdown 标题 + 首句 | 模板：全文截断到 ~2k tokens | 否 | 0 |
| `memory` | 模板：前 ~100 tokens 截取 | 短内容直接返回；超长才用 LLM 压缩 | 罕见 | 通常 0 |
| `resource` | LLM：文档前 2000 字 → 一句话摘要 | LLM：生成结构化概览（主题/要点/适用场景） | 是 | ~1500 |

### LLMClient ABC

```python
@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str

class LLMClient(ABC):
    @abstractmethod
    async def complete(self, prompt: str, max_tokens: int = 512, temperature: float = 0.0) -> LLMResponse: ...

    @abstractmethod
    async def complete_batch(self, prompts: list[str], max_tokens: int = 512, max_concurrency: int = 10) -> list[LLMResponse]: ...
```

### TableSchemaStrategy 关键设计

L1 生成分两步，最大化确定性内容、最小化 LLM 调用：
1. 模板拼接 schema 表格 + 分区信息 + 统计信息 + 样例数据（零 token）
2. LLM 只生成"常用查询模式"段落（~200 input + ~200 output tokens）

### SkillStrategy 关键设计

完全不调用 LLM。Skill 本身是 Markdown 指令，截取比摘要更保真：
- L0: 提取 `# 标题` + 正文首句
- L1: 全文截断到 ~3000 字符，在段落边界截断

### MemoryStrategy 关键设计

大多数记忆 < 2k tokens，直接返回全文（零 LLM）：
- L0: 前 ~100 tokens 截取
- L1: 内容 <= 3000 字符直接返回；超长才用 LLM 压缩（罕见）

---

## 六、实施顺序

按 Phase 实施，每个 Phase 内的文件创建顺序：

### Phase 1（骨架，1-2 周）
1. `pyproject.toml` + `alembic.ini`
2. `config.py` + `models/` 全部
3. `db/pool.py` + `db/repository.py` + `db/queries/`
4. `alembic/versions/001_initial_schema.py`（所有核心表）
5. `vector/base.py` + `vector/chroma_store.py` + `vector/factory.py`
6. `llm/base.py` + `llm/openai_client.py` + `llm/factory.py`
7. `generation/` 全部
8. `store/context_store.py`
9. `services/acl_service.py` + `services/audit_service.py`
10. `services/indexer_service.py`
11. `api/deps.py` + `api/middleware.py` + `api/routers/contexts.py`
12. `main.py`

### Phase 2A（数据湖，3-4 周）
13. `connectors/base.py` + `connectors/mock_connector.py`
14. `services/catalog_sync_service.py`
15. `services/retrieval_service.py`
16. `api/routers/datalake.py` + `api/routers/search.py`

### Phase 2B（多 Agent，3-4 周，与 2A 并行）
17. `services/memory_service.py` + `services/skill_service.py`
18. `api/routers/memories.py` + `api/routers/skills.py`

### Phase 2C（变更传播，2A+2B 之后）
19. `propagation/` 全部
20. `services/propagation_engine.py`

### Phase 2D（最小权限）
21. `services/acl_service.py` 补充 owner_space 可见性检查

### Phase 3（集成与评估，2 周）
22. 两条线集成 + MVP 场景验证
23. ECMB benchmark

---

## 七、验证方式

1. Phase 1 完成后：`uvicorn contexthub.main:app` 启动，`POST /api/v1/contexts` 创建上下文，`GET` 读取
2. Phase 2A 完成后：`POST /api/v1/datalake/sync` 同步 Mock 数据，`POST /api/v1/search` 语义检索
3. Phase 2C 完成后：修改湖表 schema → 观察依赖方被标记 stale
4. 全部完成后：跑通 MVP 场景（自然语言 → 上下文检索 → SQL 生成）
