# 04 — 多 Agent 协作：记忆/Skill 的隔离、共享与版本管理

## (a) 多层级团队所有权模型

| 范围 | URI 示例 | 可见性 | 写权限 |
|------|----------|--------|--------|
| Private | `ctx://agent/{agent_id}/` | 仅该 Agent | 该 Agent |
| 子团队 | `ctx://team/engineering/backend/` | 该子团队成员 + 上级继承 | 子团队成员 |
| 上级团队 | `ctx://team/engineering/` | 工程部所有成员 | 工程部管理员 |
| 根团队(=全组织) | `ctx://team/` | 所有 Agent | 组织管理员 |

团队结构存储在 PG `team_memberships` 表中（定义见 01-storage-paradigm.md），通过 `owner_space` 前缀匹配实现层级继承。

## (b) Skill 版本管理（简化方案）

Skill 本质是自然语言指令（Markdown），不是代码 API。SemVer 的"兼容性"概念在语义层面无法客观判定。因此不做自动兼容性判断，改为"版本号 + changelog + 手动标记 breaking"。

### PG 表结构

```sql
CREATE TABLE skill_versions (
    skill_uri       TEXT NOT NULL,          -- ctx://team/skills/sql-generator
    version         INT NOT NULL,           -- 递增版本号
    content         TEXT NOT NULL,          -- Skill 定义（Markdown）
    changelog       TEXT,                   -- 变更说明（~50 tokens）
    is_breaking     BOOLEAN DEFAULT FALSE,  -- 发布者手动标记
    status          TEXT DEFAULT 'draft',   -- 'draft' | 'published' | 'deprecated'
    published_by    TEXT,                   -- 发布者 agent_id
    published_at    TIMESTAMPTZ,
    PRIMARY KEY (skill_uri, version)
);

CREATE TABLE skill_subscriptions (
    subscriber_agent_id TEXT NOT NULL,
    skill_uri           TEXT NOT NULL,
    pinned_version      INT,                -- NULL = 跟随 latest
    PRIMARY KEY (subscriber_agent_id, skill_uri)
);
```

### 发布流程

```python
async def publish_skill_version(self, skill_uri: str, content: str,
                                 changelog: str, is_breaking: bool, ctx: RequestContext):
    async with self.pg.transaction():
        # 1. 获取当前最大版本号
        max_ver = await self.pg.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM skill_versions WHERE skill_uri = $1", skill_uri)
        new_ver = max_ver + 1

        # 2. 插入新版本
        await self.pg.execute("""
            INSERT INTO skill_versions (skill_uri, version, content, changelog, is_breaking, status, published_by, published_at)
            VALUES ($1, $2, $3, $4, $5, 'published', $6, NOW())
        """, skill_uri, new_ver, content, changelog, is_breaking, ctx.agent_id)

        # 3. 更新 contexts 表的 L0/L1（当前版本内容）
        await self.pg.execute("""
            UPDATE contexts SET l0_content = $1, l1_content = $2, l2_content = $3,
                version = $4, updated_at = NOW()
            WHERE uri = $5
        """, generate_l0(content), generate_l1(content), content, new_ver, skill_uri)

        # 4. 发出变更事件（同一事务内）
        await self.pg.execute("""
            INSERT INTO change_events (source_uri, change_type, actor, new_version, metadata)
            VALUES ($1, 'version_published', $2, $3, $4)
        """, skill_uri, ctx.agent_id, str(new_ver),
             json.dumps({"is_breaking": is_breaking, "changelog": changelog}))

    # 5. 事务提交后触发传播
    await self.pg.execute("NOTIFY context_changed, $1", skill_uri)
```

传播逻辑：
- `is_breaking=True` → 依赖方被标记 STALE（见 06-change-propagation.md）
- `is_breaking=False` → 仅通知订阅者，不标记 STALE

## (c) 记忆共享与提升

```
Agent 私有记忆 → [提升请求] → 目标团队审核队列 → [审核通过] → 写入目标团队路径
                                                    ↓
                                        [该团队及子团队 Agent 收到通知]
```

### 提升流程（PG 事务保证原子性）

```python
async def promote_memory(self, source_uri: str, target_team: str, ctx: RequestContext):
    async with self.pg.transaction():
        # 1. 读取源记忆
        source = await self.pg.fetchrow("SELECT * FROM contexts WHERE uri = $1", source_uri)

        # 2. 构造目标 URI
        target_uri = f"ctx://team/{target_team}/memories/shared_knowledge/{source['uri'].split('/')[-1]}"

        # 3. 写入目标团队路径
        await self.pg.execute("""
            INSERT INTO contexts (uri, context_type, scope, owner_space, account_id,
                l0_content, l1_content, l2_content)
            VALUES ($1, 'memory', 'team', $2, $3, $4, $5, $6)
        """, target_uri, target_team, ctx.account_id,
             source['l0_content'], source['l1_content'], source['l2_content'])

        # 4. 注册 derived_from 依赖（追踪来源）
        await self.pg.execute("""
            INSERT INTO dependencies (source_uri, target_uri, dep_type)
            VALUES ($1, $2, 'derived_from')
        """, target_uri, source_uri)

        # 5. 发出变更事件
        await self.pg.execute("""
            INSERT INTO change_events (source_uri, change_type, actor, metadata)
            VALUES ($1, 'created', $2, '{"promoted_from": "' || $3 || '"}')
        """, target_uri, ctx.agent_id, source_uri)

        # 6. 审计日志
        await self.pg.execute("""
            INSERT INTO audit_log (actor, action, resource_uri, metadata)
            VALUES ($1, 'promote', $2, $3)
        """, ctx.agent_id, target_uri, json.dumps({"from": source_uri, "to_team": target_team}))
```

示例：后端组 Agent 的一个 SQL pattern 提升到工程部共享
```
ctx://agent/backend-bot/memories/cases/sql-pattern-001
  → 提升到 ctx://team/engineering/memories/shared_knowledge/sql-pattern-001
  → 工程部下所有子团队（backend、data 等）的 Agent 可见
  → dependencies 表记录 derived_from 关系，源记忆变更时可传播通知
```
