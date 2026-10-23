# 提示词目录

这里都是当前使用的提示词，没有已停用的旧版。默认 LLM 流程与可选 Jev 流程的提示词见下表；Jev P2 路径只判断用途，具体记忆处理交给 LLM。

规则和普通内容抽取都必须提供 `topics` 字符串列表。无论原文语言，统一用简短英文描述事项，尽量不依赖当前值；如 `residence`、`deployment platform`，而非城市名或 `location` 这种实体类型。遗忘/更正请求描述被处理的事项，规则覆盖条件及结果侧。多条独立事实继续拆分，不因多个 topics 合成一条。topics 是程序检索提示，不是实体身份、更新或删除依据；无需在后续模型输入里重复。

任一主题词对的余弦相似度 **> `--topic-similarity-threshold`（默认0.8）** 可额外保留候选；原有正文/实体/关键词路径继续有效。new、correction、默认直接处理、forget、P1 和修复补查复用同一个主题匹配实现。问答查询没有新增主题抽取调用，仍使用原回答检索；维护传播仍沿已确认的规则绑定关系进行。

抽取保留原文的团队、项目等主体；`we/our` 不能改成个人的 `the user/their`。只用当前会话解析指代，无法确定时保留群体指代，不凭 `team/group` 称呼差异制造不同身份。普通信息、默认直接处理、更正和遗忘的维护提示会结合现有候选的名称、归属、范围与具体事项连续性判断是否同一主体；不把这些称呼自动视为同义词，也不因候选只有一个就确认身份。不同具名团队/项目仍须区分。

候选关键词证据排除泛主体词 `user/person/team/group`；这些词单独不能通过“中等向量相似度＋关键词”放行，也不能作为裸实体名称在该筛选器里放行。正文强相似、明确实体/编号和 topics 的现有条件继续有效。new 与 forget 的语义补查复用此筛选器，不能把筛选命中当成主体确认。

问答材料按问题文本分流：当前事实只检索有效的当前 fact，在排序前排除 rule；明确的政策/规则/条件/假设问法可读取有效规则，以 `conditional_rules` 单独呈现。历史问题沿用历史检索。分流无额外模型调用，也不读取 MEME 题型；官方回答模板保持原样。

`answer_guidance.txt` 现在保存 MEME / MD-Flat 的完整官方回答模板，包含 `{context}` 和 `{question}`，与 `evaluation/answer.txt` 逐字一致。回答入口只格式化一次，不再附加完整性、属性范围或条件规则指导；实际使用模板受官方指纹校验。材料构造和检索保持不变。此前指导实验分别保存在 `outputs/answer-guidance-replay-20261023/` 与 `outputs/answer-scope-pilot-20261023/`，本次全量复用材料实验在 `outputs/official-answer-replay-20261023/`：100 例完成，官方计分 431/694，Agg 30/100，值班/文档 25 题均未整题通过；没有证明纯官方模板能解决聚合遗漏。统一回答模板有助于比较，但不是 MD-Flat 同材料、同维护模型、同渠道的完整对照。

| 文件 | 用途 |
| --- | --- |
| [answer_guidance.txt](answer_guidance.txt) | 当前使用的完整官方回答模板，只插入材料和问题 |
| [extractor.txt](extractor.txt) | 第一次抽取：规则 |
| [extractor_rule_jev.json](extractor_rule_jev.json) | `--rule-gate-backend jev`：只看角色和含 if 的完整句子；公共说明放一次，排除 not_rule，将 rule／unclear 交给后续确认 |
| [extractor_rules_normalize.txt](extractor_rules_normalize.txt) | 读取完整当前会话，确认候选是否为用户确立或采纳的规则，再补全指代并整理；可以全部拒绝，每条输出必须引用转交的候选编号 |
| [extractor_facts.txt](extractor_facts.txt) | 第二次抽取：拆分普通内容，提供可保存正文、topics 和 current／historical 时间归属；移除条件规则，保留请求意图和逐字引文。Jev new 空候选直接按此结果保存 |
| [p2_direct.txt](p2_direct.txt) | 直接处理：新增、重复、更新、删除等 |
| [p2_intent_jev.json](p2_intent_jev.json) | `--p2-direct-backend jev`：不看旧候选，只判断 content 的 new／forget／correction／hold；rule 直接保存 |
| [p2_new.txt](p2_new.txt) | Jev 判断 new 后，LLM 比较旧 fact，生成新增、确认、更新、历史或失效处理清单 |
| [p2_correction.txt](p2_correction.txt) | Jev 判断 correction 后，LLM 对多条旧 fact／rule 生成更正、替换或撤回处理清单 |
| [p2_forget_scope.txt](p2_forget_scope.txt) | 遗忘范围判断：检查全版本明确实体命中＋经现有 0.50／0.35 或 topics 匹配保留的语义补查，按请求选删除事项；人物身份／关系遗忘覆盖明确同人的共同活动线索，命中不自动删除，Jev 路径无需先选主目标 |
| [p1_conditions.txt](p1_conditions.txt) | 在规则侧分别选择条件来源事项和受影响事项，输出两组引用 |
| [p1_jev.txt](p1_jev.txt) | `--p1-backend jev`：逐候选分别判断 condition／affected 两种角色，同一规则合并请求 |
| [p2_conditions.txt](p2_conditions.txt) | 沿已绑定关系检查完整来源状态与目标，返回失效事实及规则相关的变化编号 |
| [p2_repair.txt](p2_repair.txt) | 用规则与事实依据修复旧事实 |
| [output_retry.txt](output_retry.txt) | 输出校验失败后的纠正提示 |

## evaluation：问答和评分

[evaluation/](evaluation/) 中的文件仍在使用，不是废弃提示词：

- `answer.txt`：官方回答模板的基准副本；当前运行读取同内容的 `answer_guidance.txt`。
- 七份 `judge_*_prompt.txt`：官方评分模板，供评分或官方一致性校验使用。
- `official_manifest.json`：官方版本及内容校验指纹。

历史实验的提示词保留在各自的 `artifacts/memory-research/` 和 `docs/memory-research/validation-*/` 快照内，不是当前运行入口。当前已停用的 `p1.txt`、`p2.txt` 已从工作区删除，无需恢复或再归档到这里。

运行时会将整个目录复制为该次运行的快照；加载路径和运行清单均支持 `evaluation/` 子目录。Jev 用途判断的原审阅草稿已移入运行目录；旧多轮 Jev 判断不再使用。
