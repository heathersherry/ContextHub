# Memory 研究实现

2026-10-07：按主方案首版 `local_base` 实现五模块维护、当前／历史检索及独立 MEME 入口。研究分支为 `codex/memory-research`；设计依据仍是研究仓的 `research/proposal/README-memory-architecture-2026-09-29.md`。

## 模块与执行边界

- `src/contexthub/memory/pipeline.py`：完整 session 抽取、索引、逐焦点 P1、P2 直接维护和已有结论修复；无新材料不重复模型判断，只有检索及已知关系涉及的未决事项参与重查。仅实现 local_base。
- `dependency_discovery.py`：本段全部新记忆、最多 30 条搜索结果、相关已知依赖；逐方向完整判断、事项和版本引用检查。历史版本明确标识为历史候选。具体字段见 `contracts.py`。
- `maintenance_executor.py`：直接维护批次在一个事务中保存版本、出处归并、边释放、失效传播和 change 完成记录；同一变化不会重复应用。修复在模型响应及向量生成后单独提交，保存实际依据版本；再交 P1 判断新版本关系。不新增推导事项。
- `retriever.py`：english 词项 OR、精确向量、等权 RRF；实体从未入选候选按余弦相似度补充。回答用问题原文中出现的已有称呼；当前只读 current 内最新版且已接纳、有效的 fact/rule，历史只读曾接纳且未被判错或遗忘的内容。
- `schema.sql`：新增 temporal_scope、accepted、invalidated、basis_refs、merged_into、maintenance_active 与按顺序记录过程的 memory_steps。原始候选保留用于审计，归并候选不进入常规检索。推导的实际依据与“需要复查”依赖分别保存。
- `prompts/`：抽取、P1、P2、官方回答和评分提示词。官方评分源版本及提示词校验信息见 `official_manifest.json`。P1/P2 无额外格式重跑或第二个核验模型。
- 共用调用层：支持省略输出上限、请求总时限、余额不足不重试、Retry-After 更长等待、拒答／截断停止、完整请求与原始响应记录。费用仍复用 execution_attempts，统计只计 finished 事件。

重复确认只补出处；已判错的版本需要独立重新接纳时另存版本，保留原依据。历史纠错不能覆盖当前信息。遗忘排除整个事项所有版本，以及实际使用其内容生成的推导版本；不会物理删除审计记录。

过程记录保存在 `memory_steps`，覆盖实际模型输入、解析结果、编号归并、检索候选数量／排名／分数、状态和依赖变化、修复依据、回答与评分及失败位置。模型调用通过轨迹、session、阶段以及题号／before-after 关联；题目、标准答案和任务标签不传入维护模块。

## 确定性验证（不运行实验）

新测试库为 `contexthub_memory_dev_20261007`，三维 fixture 向量；原库 `contexthub_memory_research_20261002` 保留。新 schema 用独立空库初始化，不把旧基础库的数据状态猜成“已接纳”，不迁移产品表。

在 ContextHub 根目录执行：

```sh
export MEMORY_DATABASE_URL='postgresql:///contexthub_memory_dev_20261007'
.venv/bin/python scripts/memory_research/validate.py \
  --run-id memory-maintenance-20261007-r1 \
  --output docs/memory-research/validation-20261007-r1
```

复跑必须更换 run_id 和输出目录。脚本执行真实 PG 机制测试及共用客户端回归，输出 XML、终端结果与 result.json；模型 HTTP 响应均为测试数据，不访问真实模型。测试不证明抽取／判边／修复质量或 MEME 效果。

## 未来实验入口（本轮没有执行）

先明确 `runtime-config.json` 中 `document_prefix`、`query_prefix`（允许明确设为空字符串，但不预先替用户决定）。`prices` 与 `paper_prices` 按模型名填写 `PriceBasis`；未提供时费用为空，不伪造单价。实际响应中的模型标识使用既有 `actual_model` 字段，缺失保留原因。

使用新的空 `contexthub_memory_*` 数据库，配置必须是 text-embedding-3-small、1536 维、英文全文；不要复用三维测试库。指定库后初始化：

```sh
PYTHONPATH=src .venv/bin/python scripts/memory_research/init_db.py \
  --config docs/memory-research/runtime-config.json
```

只有决定开始实验时再运行：

```sh
.venv/bin/python -m integrations.memebench.memory_research.cli \
  --config docs/memory-research/runtime-config.json \
  --mode check --output artifacts/memory-research

.venv/bin/python -m integrations.memebench.memory_research.cli \
  --config docs/memory-research/runtime-config.json \
  --mode formal --output artifacts/memory-research
```

入口读取本机 `model_providers.local.json` 的 openlux 配置；不将凭据保存到运行记录。check 仅 pl_001、sw_001，formal 读取固定哈希的 filler32k 全 100 例，分别生成唯一运行编号、从空轨迹开始。全部 session 按顺序摄入，只回答 Cas／Abs／Del，评分复用冻结的官方方法和配对分类。官方评分最多三次，调用层一次，避免重试叠加。

每轮目录保存代码与未提交源码副本、差异、全部提示词、生效配置、数据库名、逐题输出、评分、过程及状态导出和分组成本。失败与未判不从原分母剔除。评分与向量单列，共享维护成本仅计一次；仅报告三类任务，不输出六类 Overall。

产品接入、full_strong/adaptive、物理删除、失败自动恢复仍不在首版范围。真实模型调用、1536 维实验库初始化和正式 MEME 实验均尚未执行。
