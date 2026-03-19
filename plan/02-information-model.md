# 02 — L0/L1/L2 信息模型与记忆分类

## L0/L1/L2 三层信息模型

概念不变，存储方式从文件改为 PG 列：

| 层级 | Token 量 | 用途 | 存储位置 | 数据湖表示例 |
|------|----------|------|----------|-------------|
| L0 Abstract | ~100 | 向量检索、快速过滤 | PG `l0_content` 列 + 向量库 embedding | 表名 + 一句话描述 |
| L1 Overview | ~2k | Rerank、内容导航 | PG `l1_content` 列 | schema + 字段说明 + 样例数据 |
| L2 Detail | 不限 | 按需加载 | PG `l2_content` 列（通用）或结构化子表（datalake） | 完整 DDL + 血缘 + 查询模板 |

### L2 的两种存储模式

- **通用上下文**（技能、记忆、文档资源）：L2 内容存在 `contexts.l2_content` TEXT 列中，TOAST 自动处理
- **数据湖表**：L2 拆解为结构化子表（`table_metadata`、`lineage`、`table_relationships`、`query_templates`），因为各部分更新频率不同，且需要独立查询（详见 03-datalake-management.md）

### 向量化策略

只有 L0 摘要被向量化并存入向量库。L1/L2 不入向量库——检索命中后直接从 PG 读取。

原因：
- 向量检索的目的是"找到相关上下文"，L0 的 ~100 tokens 摘要足够
- L1/L2 用于精排和详情展示，从 PG 按 URI 直接读取比从向量库取更快、更一致
- 减少向量库的存储和索引维护成本

## 记忆分类

借鉴 OpenViking 的 6 类记忆并扩展：

| 范围 | 类别 | 说明 | 更新策略 |
|------|------|------|----------|
| 用户级 | profile | 用户基本信息 | 可追加 |
| 用户级 | preferences | 用户偏好 | 可追加 |
| 用户级 | entities | 实体记忆（人、项目） | 可追加 |
| 用户级 | events | 事件记录 | 不可变 |
| Agent级 | cases | 学到的案例 | 不可变 |
| Agent级 | patterns | 学到的模式 | 可追加 |
| 团队级（任意层级） | shared_knowledge | 该层级团队共享的业务知识 | 可追加，需审核 |
| 团队级（根 = 全组织） | business_rules | 全组织业务规则（`ctx://team/memories/`） | 管理员维护 |
| 团队级（根 = 全组织） | data_dictionary | 全组织数据字典（`ctx://team/memories/`） | 管理员维护 |

记忆在 PG 中的存储：每条记忆是 `contexts` 表的一行，`context_type = 'memory'`，通过 `scope`（user/agent/team）和 `owner_space` 区分归属。记忆类别（cases/patterns/profile 等）编码在 URI 路径中，如 `ctx://agent/{id}/memories/cases/sql-pattern-001`。

## 层级检索

采用两阶段检索：

1. **向量检索**（向量库）：用 L0 embedding 做语义匹配，标量过滤（account_id、scope、context_type、owner_space），返回 top-K URI
2. **精排 + 加载**（PG）：批量读取候选的 L1 内容做 Rerank，按需加载 L2 或关联结构化数据

跨上下文的关联检索通过 PG `dependencies` 和 `table_relationships` 表实现（替代 `.relations.json` 文件遍历）。

## 热度评分

```
score = sigmoid(log1p(active_count)) * exponential_decay(updated_at)
```

`active_count` 和 `updated_at` 直接存在 `contexts` 表中，每次访问时原子更新：

```sql
UPDATE contexts SET active_count = active_count + 1, last_accessed_at = NOW() WHERE uri = $1;
```

用于冷热记忆管理和生命周期决策（见 07-feedback-lifecycle.md）。
