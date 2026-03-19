# 06 — 事件驱动的变更传播

## 问题本质

上下文之间存在依赖关系。当一个上下文变动时，依赖它的其他上下文可能过时。核心问题：
1. 如何把"变动"及时 broadcast 到相关组件？
2. 如何根据变动做出自身修改（尽量不依赖模型推理，节省 token）？

## PG 原生能力如何支撑变更传播

| 传播环节 | PG 机制 | 替代的原方案 |
|----------|---------|-------------|
| 变更事件持久化 | `change_events` 表（ACID 写入） | append-only JSON 文件 |
| 事件通知 | `LISTEN/NOTIFY` | 自建事件队列 + 轮询 |
| 依赖查询 | `dependencies` 表 + SQL JOIN | 遍历 `.deps.json` 文件 + 向量 DB 标量过滤 |
| STALE 标记 | `UPDATE contexts SET status = 'stale'` | 在文件头部追加注释 |
| 事务保证 | 内容更新 + 变更事件在同一事务中 | 无（可能不一致） |

## 哪些变动需要传播？

| 变动源 | 影响目标 | 耦合度 | 传播方式 |
|--------|----------|--------|----------|
| Skill 版本更新 | 基于旧版本积累的 cases/patterns | 强 | 标记过时 + 规则匹配 |
| 湖表 schema 变更 | 该表的 L0/L1 + 引用该表的查询模板 | 强 | 自动重新生成 |
| 共享 memory 被纠正 | 从该 memory 派生的 Agent 私有 memory | 中 | 通知 + 人工/Agent 确认 |
| 团队 Skill 更新 | 引用该 Skill 的其他团队 Skill | 中 | 通知订阅者 |
| 统计信息更新 | 无 | 无 | ❌ 不传播（见 03 精确传播设计） |
| 用户偏好变更 | Skill 定义 / 团队共享 memory | 无 | ❌ 不传播 |

## (1) 变更事件模型

变更事件存储在 PG `change_events` 表中（定义见 01-storage-paradigm.md），与业务操作在同一个事务中写入：

```python
# 任何产生变更的操作都在事务中同时写入 change_event
async with self.pg.transaction():
    await self.update_context(uri, new_content)
    await self.pg.execute("""
        INSERT INTO change_events (source_uri, change_type, actor, diff_summary, new_version, metadata)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, uri, change_type, actor, diff_summary, new_version, metadata)
# 事务提交后通知传播引擎
await self.pg.execute("NOTIFY context_changed, $1", uri)
```

## (2) 依赖关系注册

依赖存储在 PG `dependencies` 表中（替代 `.deps.json` 文件）：

```sql
-- 示例：Agent 的一个 SQL case 依赖某个 Skill 和某张表
INSERT INTO dependencies (source_uri, target_uri, dep_type, pinned_version) VALUES
  ('ctx://agent/query-bot/memories/cases/sql-pattern-001',
   'ctx://team/skills/sql-generator', 'skill_version', '2'),
  ('ctx://agent/query-bot/memories/cases/sql-pattern-001',
   'ctx://datalake/prod/orders', 'table_schema', NULL);
```

依赖类型：
- `skill_version`：依赖某个 Skill 的特定版本
- `table_schema`：依赖某张表的 schema
- `derived_from`：从某个共享 memory 派生

依赖何时注册：
- Agent 创建 case/pattern 时，自动记录当时使用了哪些 Skill 和表 → 写入 `dependencies` 表
- 记忆提升（promote）时，在目标记忆中记录 `derived_from` 源
- 写入时的附加操作，不需要额外的模型调用

## (3) 变更传播流程

```
变动发生（如 Skill v2.0.0 发布）
    │
    ▼
change_events 表写入（同一事务，ACID 保证）
    │
    ▼
PG NOTIFY 'context_changed' → 传播引擎被唤醒
    │
    ▼
传播引擎查询：谁依赖了这个 URI？
    │  方法：SELECT * FROM dependencies WHERE target_uri = $1（精确匹配，有索引，极快）
    │
    ▼
对每个依赖方，根据 dep_type 执行对应的 PropagationRule
```

## (4) PropagationRule：三级响应策略

核心思想：分级响应，大多数情况不需要模型推理。

```python
class PropagationRule(ABC):
    async def evaluate(self, event: ChangeEvent, dependent_uri: str) -> PropagationAction

class PropagationAction:
    action: str          # mark_stale | auto_update | notify | no_action
    reason: str
    auto_update_fn: Callable | None
```

### Level 1: 纯规则，零 token（~70% 场景）

```python
class SkillVersionRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        if event.metadata.get("is_breaking"):
            return PropagationAction(action="mark_stale",
                reason=f"依赖的 Skill {event.source_uri} 发布了破坏性变更 v{event.new_version}")
        else:
            return PropagationAction(action="notify",
                reason=f"Skill 更新到 v{event.new_version}，非破坏性变更")

class TableSchemaRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        return PropagationAction(action="auto_update",
            reason=f"表 {event.source_uri} schema 已变更",
            auto_update_fn=lambda: regenerate_l0_l1(dependent_uri))
```

`mark_stale` 实现（PG 原子操作）：
```sql
UPDATE contexts SET status = 'stale', updated_at = NOW() WHERE uri = $1;
-- 同时记录 stale 原因到 change_events
INSERT INTO change_events (source_uri, change_type, actor, diff_summary)
VALUES ($1, 'marked_stale', 'propagation_engine',
        '依赖的 Skill sql-generator 已从 v1 升级到 v2 (breaking)');
```

### Level 2: 模板替换，极少 token（~20% 场景）

```python
class DerivedMemoryRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        if event.change_type == "modified":
            old_text = extract_changed_text(event)
            new_text = extract_new_text(event)
            if old_text and can_simple_replace(dependent_uri, old_text):
                return PropagationAction(action="auto_update",
                    auto_update_fn=lambda: simple_text_replace(dependent_uri, old_text, new_text))
            else:
                return PropagationAction(action="notify", reason="源 memory 已修改，请人工确认")
```

### Level 3: 模型推理（~10% 场景）

```python
class ComplexPropagationRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        # 从 PG 读取依赖方的 L0 摘要
        l0 = await self.pg.fetchval("SELECT l0_content FROM contexts WHERE uri = $1", dependent_uri)
        prompt = f"""
        变动：{event.diff_summary}
        受影响的上下文摘要：{l0}
        问题：这个变动是否使上述上下文过时？回答 yes/no 及原因（一句话）。
        """
        result = await llm_call(prompt)  # ~200 input + ~30 output tokens
        if result.startswith("yes"):
            return PropagationAction(action="mark_stale", reason=result)
        return PropagationAction(action="no_action", reason=result)
```

## (5) 完整传播流程示例

```
场景：sql-generator Skill 发布新版本 v3（breaking，新增 window function 支持，废弃子查询替代方案）

1. Skill Service 发布新版本（事务内）
   → INSERT INTO skill_versions (...)
   → UPDATE contexts SET version = 3 WHERE uri = 'ctx://team/skills/sql-generator'
   → INSERT INTO change_events (source_uri='ctx://team/skills/sql-generator',
       change_type='version_published', new_version='3',
       metadata='{"is_breaking": true}',
       diff_summary='新增 window function 支持，废弃子查询替代方案')
   → COMMIT

2. PG NOTIFY 'context_changed' → 传播引擎被唤醒

3. 传播引擎查询依赖方
   → SELECT source_uri FROM dependencies
     WHERE target_uri = 'ctx://team/skills/sql-generator'
   → 找到 3 个 cases

4. 对每个依赖方执行 SkillVersionRule
   → is_breaking=True → action="mark_stale"
   → UPDATE contexts SET status = 'stale' WHERE uri IN (case1, case2, case3)

5. 通知相关 Agent
   → 查询 cases 的 owner_space → 找到对应 agent_id
   → 写入通知（可通过 PG NOTIFY 或应用层通知队列）

6. Agent 下次使用时
   → 检索到 case，看到 status='stale'
   → Agent 决定：用新版 Skill 重新生成，或忽略旧 case
   → 如果重新生成，更新 dependencies 中的 pinned_version，恢复 status='active'
```

## (6) 传播引擎实现

```python
class PropagationEngine:
    """监听 PG NOTIFY，处理变更事件"""

    async def start(self):
        await self.pg.execute("LISTEN context_changed")
        # 事件循环
        async for notification in self.pg.notifications():
            await self.process_event(notification.payload)

    async def process_event(self, source_uri: str):
        # 1. 读取未处理的变更事件
        event = await self.pg.fetchrow("""
            SELECT * FROM change_events
            WHERE source_uri = $1 AND NOT processed
            ORDER BY timestamp DESC LIMIT 1
        """, source_uri)
        if not event:
            return

        # 2. 查询所有依赖方
        dependents = await self.pg.fetch("""
            SELECT source_uri, dep_type, pinned_version
            FROM dependencies WHERE target_uri = $1
        """, source_uri)

        # 3. 对每个依赖方执行对应规则
        for dep in dependents:
            rule = self.get_rule(dep['dep_type'])
            action = await rule.evaluate(event, dep['source_uri'])
            await self.execute_action(action, dep['source_uri'])

        # 4. 标记事件已处理
        await self.pg.execute(
            "UPDATE change_events SET processed = TRUE WHERE event_id = $1",
            event['event_id'])
```

## (7) Token 消耗估算

| 响应级别 | 占比 | 每次 token 消耗 | 说明 |
|----------|------|----------------|------|
| Level 1: 纯规则 | ~70% | 0 | is_breaking 判断、PG UPDATE status |
| Level 2: 模板替换 | ~20% | 0 | 字符串替换 |
| Level 3: 模型推理 | ~10% | ~230 | 仅读 diff_summary + L0 |

一次 Skill 升级影响 10 个依赖方：总共 ~230 token（vs 全部模型推理 ~20000 token，节省 99%）。

## (8) 依赖完整性校验（兜底机制）

`dependencies` 表依赖写入方自觉注册。如果漏了，传播系统会静默失败。

离线扫描方案（定期运行，如每天一次）：

```python
class DependencyIntegrityChecker:
    """扫描上下文内容中的实际引用，与 dependencies 表声明对比"""

    async def check(self) -> list[IntegrityIssue]:
        issues = []
        # 从 PG 批量读取所有上下文内容
        contexts = await self.pg.fetch("SELECT uri, l0_content, l1_content, l2_content FROM contexts WHERE status = 'active'")
        for ctx in contexts:
            actual_refs = extract_uri_references(ctx['l1_content'], ctx['l2_content'])
            declared_deps = await self.pg.fetch(
                "SELECT target_uri FROM dependencies WHERE source_uri = $1", ctx['uri'])
            declared_set = {d['target_uri'] for d in declared_deps}
            undeclared = actual_refs - declared_set
            if undeclared:
                issues.append(IntegrityIssue(
                    uri=ctx['uri'], type="undeclared_dependency",
                    missing_deps=undeclared,
                    suggestion="自动补充 dependencies 表或通知 Agent 确认"
                ))
        return issues
```

处理策略：
- 高置信度遗漏 → 自动补充 `dependencies` 表
- 低置信度遗漏 → 生成报告，管理员确认
- 扫描结果写入 `audit_log` 表
