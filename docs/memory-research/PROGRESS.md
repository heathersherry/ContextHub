# Memory 研究交接

更新：2026-10-02。主方案见 [架构 README](/Users/sherrylin/Documents/PythonProjects/ContextHub-research-plan/research/proposal/README-memory-architecture-2026-09-29.md)；本文件只保存实现进度。

## 工作现场与提交

- 代码目录：`/Users/sherrylin/Documents/PythonProjects/ContextHub`。
- 当前研究分支：`codex/memory-research`，没有修改或合入 main，没有 push。
- 原分支 `codex/s0-active-only` 的旧 HEAD 为 `7017b81^`；本轮先将 102 个相关源码／配置／测试文件保存为基线提交 `7017b81`，再从该提交创建研究分支。包含之前未跟踪的逐 session 实现及共用客户端。
- 未把 `.env`、`secrets/`、本地模型配置、实验数据库或整个 artifacts 目录提交。原有 `.smoke_full_s2/`、`.smoke_observe/`、`artifacts/`、`docs/patha-current-flow/`、`docs/patha-preparation-2026-09-25/`、`docs/patha-session-maintenance/` 继续保留在工作区，未纳入本轮提交。

## 已完成

独立 PG 库 `contexthub_memory_research_20261002`、新五张记忆表与共用调用记录、Extractor、Indexer、基础 Retriever、仅到索引为止的 ingest 流程。详细接口和命令见 [README](README.md)。P1/P2 没有实现，没有假装完整五模块流程已跑通。

状态变化和版本写入是低层存储能力。测试中显式准备 fresh/stale/derived 状态，不是程序已能正确决定这些状态。

## 验证

开发首轮：新模块 14 项通过，其中 11 项在真实 PG 上验证；另 3 项为解析／融合等纯程序检查。相关共用客户端、请求重试、向量与费用记录回归 59 项通过。结果暂保存在 `validation-dev1.xml`、`shared-regression-dev1.xml`，只代表开发现场；最终提交代码的可复现验证将另写独立结果目录。

覆盖：session 角色和编号、抽取／出处／change 初始值、空批次和部分异常、四轮格式失败中止、网络重试分离与完整响应、总时限取消、持久化重连、模型配置一致性、轨迹隔离、先版本后状态、范围内 top-k、全文／精确向量／RRF、实体补集、状态变更不重嵌入、所有版本遗忘、依赖旧新版保留、时间仅用于回答的同分排序。

模型请求由 HTTP 测试响应替代；向量是三维确定性数据。真实验证的是 PG 存储与检索机制，不是抽取质量、语义召回率、模型成本或 MEME 效果。费用未提供价格时保持未知。库内原始聊天与抽取输出只供审计，回答检索只读 memories。

## 未完成与下一步

[QUESTIONS.md](QUESTIONS.md)列出 P1/P2 的 12 个具体待决问题。本轮没有选消歧算法、直接事实放行规则、传播／修复策略、升级阈值或具体研究模型。下一轮先讨论 P1 输出合同与 P2 放行／核验条件，再逐块实现；MEME 新入口及 before/after 回答与评分随后接入。当前 `validate.py` 是机制验证入口，不是 MEME 实验入口。

当前版本不提供历史有效性判定，也不支持失败后的自动恢复／跳过。不能把截至某 session 产生的最大版本当作当时事实有效性的证明。
