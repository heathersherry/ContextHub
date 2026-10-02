# Memory 研究基础实现

本目录是代码进度和验证入口。[交接记录](PROGRESS.md)保存当前状态，[待决问题](QUESTIONS.md)列出尚未实现的 P1/P2 行为。架构唯一主文件仍是[原方案](/Users/sherrylin/Documents/PythonProjects/ContextHub-research-plan/research/proposal/README-memory-architecture-2026-09-29.md)，不在这里复制一份架构。

## 本轮接口与边界

代码在 `src/contexthub/memory/`：

- `MemoryStore`：五张逐行表的基础读写、内容版本、状态、全部版本遗忘、出处、显式目标绑定和版本依赖边。`add_version` 总是新增 unresolved 行，change 固定第一版。`versions`、`get_session` 是审计接口，不是回答材料接口。
- `Extractor`：完整当前 session、程序编号、角色、条件句提示；解析可保存内容；首次加最多三次格式重跑。未知分类和不存在的整数引用保留；坏条目保留在完整原响应。整个非空批次均不可读时才重跑，合法空数组不重跑。助手接受／指代等语义约束写在同次提示词中，尚无真实模型质量验证。
- `Indexer`：同一模型对每条完整记忆编码一次，查询用相同模型和显式 query_prefix；PG `to_tsvector` + 两个 GIN（全文、实体）索引。状态变化不重嵌入，新增版本只索引新行。
- `Retriever`：SQL 中先限定轨迹和产生顺序、取最大版本，再按用途过滤，最后在允许集合内各取前 k；PG 全文排序 + pgvector 精确余弦扫描 + RRF；P1 实体候选另取并集。同一查询三路使用一致的数据库快照。已知依赖下游单独读取，不与候选争 top-k。
- `MemoryPipeline.ingest`：保存 session → 抽取 → unresolved 入库 → 索引，然后返回新版本引用。没有调用 P1/P2，也没有自动标 fresh。测试显式调用底层状态写接口准备检索夹具；这不代表具备接纳或维护算法。
- `calls.py`：复用 `AttemptRecord`、`RetryPolicy`、`ExecutionLedger` 的读取／校验、原有客户端和费用计算。新实验库仅调整存储定位，不引用产品事件／维护任务。原始响应捕获仅对研究调用启用，保存在同一 `execution_attempts` 表中。

`execution_attempts` 仍按每次请求保存 started/finished 事件；统计请求只取 finished，不能把两种事件都算一次调用。网络请求序号为 `attempt_no`；抽取轮次为 `extraction_attempt`；tokens、cost 和实际耗时在 payload 的原有 usage/cost/wall_seconds 字段，未知费用为 null。新增定位、原响应、解析结果、重试原因是同表列，不另建费用表。

PG 使用五张记忆表，另有共用调用记录表及一张 `research_config` 配置表。配置表锁定库内索引模型、维数、文本检索配置和编码前缀，避免用不同模型的向量直接比较。初始化脚本只接受空的 `contexthub_memory_*` 独立库，不运行产品 Alembic 迁移。

## 实际验证命令

工作目录：`/Users/sherrylin/Documents/PythonProjects/ContextHub`。已创建并初始化的库为 `contexthub_memory_research_20261002`；使用本机现有 PG 服务。连接凭据通过环境变量／PG 凭据文件提供，不写入结果。

```sh
export MEMORY_DATABASE_URL='postgresql://contexthub@localhost:5432/contexthub_memory_research_20261002'
.venv/bin/python scripts/memory_research/validate.py \
  --run-id memory-foundation-20261002-r1 \
  --output docs/memory-research/validation-20261002-r1
```

r1 用于本轮保存验证；复跑应换新的 run_id 和输出目录，不覆盖已有证据。脚本输出源码绝对路径、代码版本、分支、数据库、完整测试配置，运行新模块真实 PG 验证及相关共用代码回归，再保存 XML、文本和 result.json。每个测试用独立 episode 保留数据库记录，不清空旧数据。

仅在另建一个空库时初始化：

```sh
# 先由操作者创建另一个 contexthub_memory_* 数据库，再将 MEMORY_DATABASE_URL 指向它。
PYTHONPATH=src .venv/bin/python scripts/memory_research/init_db.py \
  --config docs/memory-research/validation-config.json
```

配置 [validation-config.json](validation-config.json)仅服务机制验证：三维确定性向量、HTTP 测试响应、英文全文配置、小规模候选数量。新真实模型实验应使用新的配置和独立库；这些数字不是研究选优结果。源码客户端可以接真实模型，但本轮没有网络模型调用，没有费用效果结论。

## 尚未完成

P1/P2、change 的自动应用／applied_session_seq 写入事务、自动传播和修复、历史回答、MEME 新适配入口、答案生成／评测、产品接入均未实现。四轮不可解析或请求异常会抛错并保留已有记录；失败抽取阻止继续下一 session。本轮没有做中断恢复或索引失败后的自动续跑协议。调用方不能忽略 ingest 的异常继续冒充整条轨迹完成。
