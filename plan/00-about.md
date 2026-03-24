# ContextHub — 企业版上下文管理系统设计

## 动机与核心问题

### 多 Agent 协作的结构性失败

当企业部署多个 AI Agent 协作处理同一批实体（客户、数据表、业务流程）时，这些 Agent 没有共享记忆、没有共同治理层。研究表明 **41-87% 的多 Agent 系统在生产中失败，其中 79% 的失败根源是协调问题而非技术 bug**[1]。对 200+ 条执行轨迹的分析发现，**36.9% 的多 Agent 失败来自 inter-agent misalignment**——Agent 忽略、重复或矛盾彼此的工作[2]。更好的模型不会修复这些问题，因为失败是结构性的。

### 企业级的 5 个结构性挑战

Governed Memory[3] 将此定义为"记忆治理缺口"（Memory Governance Gap），识别出 5 个个人版 Agent 记忆系统不需要面对的企业级挑战：

1. **跨工作流的Agent上下文孤岛**：B2B采购场景中，各agent维护独立的执行上下文，无法访问其他agent在同一实体上产生的中间结果。举例来说，enrichment agent协助CTO评估三个供应商并取得了评估结果，但outbound agent看不到评估结论和CTO的采购意愿，仍给三个供应商发送通用邮件。
2. **跨团队策略上下文变更难以传播**：各Agent的行为策略分散存储于各自的私有存储形式（markdown或外部文档）中， 没有版本控制，没有single source of truth。当某个上游策略（如数据处理合规规则）发生变更时，没有传播机制把更新同步到所有依赖该策略的Agent配置，导致整体行为不一致。举例来说，当法务部门更新数据处理合规政策后，没有任何机制能自动把这个变更传播给销售团队和客服团队。每个团队各自维护自己的"真相源"。
3. **非结构化记忆无法被下游消费**：自由文本记忆可以被检索并塞进 prompt，但无法被按阶段过滤、按价值排序、同步到 CRM（Customer Relationship Management，客户关系管理系统）、或跨实体聚合。
4. **自主执行中的上下文冗余**：Agent在多步自主循环中每步重复注入相同的合规策略，浪费 context window 和 token。
5. **无反馈环的静默质量退化**：Agent写入业务系统时所依赖的字段schema会因模型迭代或系统升级而悄然失效；由于缺乏主动监控与反馈机制，组织往往在数月后才发现 CRM 等系统的字段数据长期处于错误状态。

### 现有方案的不足

行业正在收敛到层级式记忆架构（Global → Group/Role → Private），CrewAI、MemOS 和学术研究[4]独立到达了同一模式[1]。但现有框架的企业级能力远不够：

- **Mem0**[2]：提供平坦的 user/agent/app/run 级隔离，但没有团队层级、没有变更传播、没有版本管理、仅 SaaS
- **CrewAI/LangGraph**：记忆系统面向单一框架内的协调，无法跨框架、跨团队、跨时间管理组织级知识
- **OpenAI Agents SDK**：无内置记忆、无 ACL、无租户隔离——完全交由实现者自建[1]
- **OpenViking**：具备核心的上下文管理理念（一切皆文件 + 记忆管线 + 向量检索），但定位于个人版——不支持多 Agent 隔离、团队层级、ACL、变更传播
- **Governed Memory (Personize.ai)**[3]：最接近的方案，有治理路由和实体隔离，但聚焦于 CRM 实体（contacts/companies/deals），非通用 Agent 上下文管理

安全是最大的缺口——**大多数框架没有内置的记忆访问控制**。企业部署需要租户隔离、来源追踪和最小权限访问，而当前工具基本留给实现者自行解决[1]。

### 全上下文视角：超越 Memory 的贡献定位

现有文献将"企业级 Agent 上下文管理"几乎等同于"记忆管理"。Governed Memory[3]、Collaborative Memory[4]、MemOS 均以记忆为核心抽象。但企业 Agent 系统需要管理的上下文实际涵盖四类：

| 上下文类型 | 典型内容 | 多版本/多用户问题的文献覆盖 |
|---|---|---|
| **Memory** | 对话记忆、实体状态、工作记忆 | 相对最多，但多用户协作版本管理仍稀缺[4] |
| **Skill** | 工具定义、prompt 模板、Agent 配置 | **几乎空白** |
| **Resource（RAG 文档）** | 政策文档、合规规则、产品知识库 | 仅覆盖"检索最新版本"，不覆盖变更传播 |
| **结构化元数据** | 数据库 schema、数据湖表 catalog | 无 AI Agent 语境下的研究 |

**文献空白的具体证据：**

- **Skill 版本管理**：ToolBench/ToolLLM[8] 等 tool learning 研究大规模解决了工具选择问题，但完全不涉及 breaking change 检测、pinned/latest 订阅语义或订阅者通知。MCP（Anthropic, 2024）[9] 在协议层面规范了工具发现与调用接口，但无版本语义——没有 is_breaking 标记、没有订阅者 stale 通知、没有 advisory 机制。目前 AI Agent 文献中没有处理"skill 发布者标记 breaking change → 订阅者被标记 stale → 收到 advisory"这一完整生命周期的工作。
- **RAG 文档变更传播**：Temporal RAG / Corrective RAG[10] 等工作解决了"给我当前有效的文档"的检索问题，但不解决"我更新了，谁依赖了我，谁需要被通知"——即版本变更沿依赖图向下传播这一治理问题。
- **多用户 Skill/Resource 协作**：Collaborative Memory[4] 的双向二分图权限模型仅覆盖 memory，未扩展到 skill 和 resource；隐私保护 RAG 领域的工作[11] 关注租户间隔离，而非协作共享与版本一致性。

**ContextHub 的设计贡献因此超出了"记忆治理工程化"的范畴**。以 Skill 版本管理 + breaking change 传播 + 订阅者通知为代表的上下文治理机制，在现有文献中没有对应的端到端研究；对 Memory、Skill、Resource（RAG 文档）和结构化元数据（数据库 schema / 数据湖 catalog）的统一版本治理，作为一个整体问题，也尚无 SOTA 可参照——这是真实的学术空白，而非对已有研究的工程复现。

### ContextHub 的定位

ContextHub 是面向 toB 多 Agent 协作的**企业版上下文管理中间件**。它提供共享记忆、可见性边界、版本治理和变更传播的统一状态层；将 OpenViking 的核心理念（URI 文件语义 + L0/L1/L2 分层模型 + 记忆管线）扩展为支持企业级协作的层级式架构。MVP 核心闭环能力：

1. **多层级团队所有权模型**：默认可见性 + 默认写权限 + 团队层级继承（显式 ACL allow/deny/mask 属于明确后置 backlog，见 `14-adr-backlog-register.md`）
2. **依赖图驱动的变更传播**：三级规则（纯规则/模板替换/LLM 推理）节省 99% token（不是"法律更新了但 14 个 Agent 不知道"）
3. **Skill 版本管理 + breaking change 传播**：发布者手动标记 is_breaking，订阅者按 pinned/latest 语义读取，并在需要时被标记 stale 或收到 advisory
4. **记忆晋升机制**：私有→团队→组织，derived_from 追踪来源，源记忆变更时传播通知
5. **可私有化部署**：PG 中心架构，适合 toB 企业的 on-premise 需求（不是 Mem0/Governed Memory 的 SaaS-only）

MVP 不把显式 ACL、审计日志、长期反馈/生命周期收益当成已验证能力；这些能力均已在 `14-adr-backlog-register.md` 中完成分流。

数据湖表管理（L2 结构化子表 + CatalogConnector + Text-to-SQL 上下文组装）是其上的第一个垂直验证载体，用来构造可复现的检索、共享和 schema 变更场景，不是产品本体。

### 参考文献

- [1] [AI Agent Memory Architectures for Multi-Agent Systems](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026-03. 综合分析共享/隔离/层级式记忆模式、安全缺口、存储收敛趋势
- [2] [How to Design Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026-03. 36.9% 失败来自 inter-agent misalignment (引用 [Cemri et al., 2025](https://arxiv.org/abs/2503.13657))
- [3] [Governed Memory: A Production Architecture for Multi-Agent Workflows](https://arxiv.org/abs/2603.17787) — Taheri, 2026-03. 5 个结构性挑战定义、双记忆模型、治理路由、实体隔离
- [4] [Collaborative Memory: Multi-User Memory Sharing with Dynamic Access Control](https://arxiv.org/abs/2505.18279) — 动态访问控制的协作记忆，双向二分图权限模型
- [5] [Context Engineering for Commercial Agent Systems](https://www.jeremydaly.com/context-engineering-for-commercial-agent-systems) — Jeremy Daly, 2026-02. 商业多租户 Agent 系统的上下文工程实践
- [6] [Why Multi-Agent Systems Need Memory Engineering](https://www.mongodb.com/company/blog/technical/why-multi-agent-systems-need-memory-engineering) — MongoDB/O'Reilly, 2026. Context poisoning/distraction/confusion/clash 四类记忆问题
- [7] [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源（个人版上下文管理）
- [8] [ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs](https://arxiv.org/abs/2307.16789) — Qin et al., 2023. 大规模 tool learning 研究，解决工具选择与调用，但不涉及版本管理或 breaking change 语义
- [9] [Model Context Protocol (MCP)](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024-11. 协议层面规范工具/资源发现与调用，无版本语义（无 is_breaking、无订阅者通知）
- [10] [Corrective Retrieval Augmented Generation (CRAG)](https://arxiv.org/abs/2401.15884) — Yan et al., 2024. 代表性 Temporal/Corrective RAG 工作，关注检索文档的时效性与可信度，不处理版本变更沿依赖图传播的治理问题
- [11] [Privacy-Preserving Retrieval-Augmented Generation with Differential Privacy](https://arxiv.org/abs/2412.04697) — 代表性隐私保护 RAG 工作，关注租户间知识隔离，不覆盖协作共享与版本一致性
