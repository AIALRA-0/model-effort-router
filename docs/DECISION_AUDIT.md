# 遥测优先独立发布决策审计

审计日期：2026-08-25

审计结论：`proceed with safeguards`。保留模型路由器和逐轮采集，并在 `0.3.0` 增加只读历史审计与可回滚前瞻试运行。在达到复核门槛前，不发布模型胜率、固定消耗倍率或普适最佳档位。

## 1 决定改变了什么

原方案同时推进规则路由、手工结果分析、用量估算和研究设计。当前方案把顺序改为：先建立可靠的开始与结束记录，再积累真实运行，之后生成整机去标识快照，最后才判断是否值得开展配对或随机交叉实验。

这不是否定 `quality_first`。它仍然是高风险任务的可选预设；`guarded_high` 成为一般长流程的建议起点。用户仍可覆盖建议，覆盖行为只记录固定原因类别。

## 2 已核验的前一版本

前一版本压缩包的 SHA-256 为 `337D54E594AE86F06C1C8B5B7A8D645FE2EC5E47AAE661B51FE7E6C54CE0EF1B`。对应拉取请求包含 19 个提交、19 个新增文件和 2,224 行新增内容，审计时状态为开放且可合并[1]。

表 1 前一版本结论与核验结果

<div align="center">

| 前一版本表述 | 核验结果 | 对当前决定的影响 |
| --- | --- | --- |
| 已有确定性路由器 | 已确认 | 保留 `recommend.py` 和既有回归测试 |
| 已有结果汇总 | 已确认，但依赖手工准备 JSONL | 保留为兼容工具，不把它当作自动观测闭环 |
| 已经可以从真实执行收集统计 | 未确认 | 新增授权、开始、结束、状态、快照和删除生命周期 |
| README 所列目录与发布自动化均存在 | 不成立 | 旧 README 列出的 CI、发布脚本和两份研究资料不在拉取请求的 19 个文件中 |
| 已经具备整机分析条件 | 不成立 | 新增跨项目、活跃日、可比较路线和每路线样本门槛 |

</div>

注：表中的“已确认”只说明文件或功能存在，不代表模型效果已经得到真实项目数据支持。

## 3 外部证据改变了哪些判断

OpenAI 当前文档把 GPT-5.6 描述为不同能力与延迟取向的模型家族，并建议从中等推理档位开始，再根据评测结果调整[2]。这支持“以任务和验证证据升级”，但不能直接推出本机项目中 Terra 或 Luna 的质量折扣。

模型路由已经有多个公开实现。`codex-model-router` 提供阶段与风险驱动的 Sol、Terra、Luna 路由[3]；`GPT5.6-SOLTELU-Model-Inverter` 也以单位 token 质量为目标路由相同模型家族[4]。因此，本项目不能声称首创模型路由。

研究证据支持动态选择的方向，但也强调任务依赖。BEST-Route 把测试时计算量和模型路由联合优化[5]；针对代理任务的过度思考研究在 4,018 条轨迹中观察到延长推理可能伴随分析停滞、越界行动和提前放弃[6]。这些研究不能替代本机真实项目测量，但足以反驳“推理档位越高必然越安全”。

OpenTelemetry 的生成式人工智能字段同时包含 token 用量和可能携带敏感内容的消息字段，并明确把隐私风险字段放入需要用户选择的等级[7][8]。当前采集器采用更窄边界：只保留固定分类和计数，主动排除消息、系统指令、工具参数、路径和代码。

## 4 当前公开项目的差异边界

本项目不把“自动选 Sol、Terra、Luna”作为原创点。当前可核验的组合差异是：

- 采集默认关闭，并且在每次真实任务开始与结束时形成同一运行记录。
- 推荐路线与实际路线分开保存，避免把“建议切换”误写成“已经切换”。
- 未知 token 和工具调用量保持 `null`，同时保存测量来源。
- 原始记录不保存自由文本、路径、文件名、提示词、回复、代码、差异或日志。
- 整机快照删除项目和机器伪标识，并对少于 5 次的分组抑制质量比率。
- 复核门槛只触发人工分析，不冒充统计显著性或因果识别。

以上差异是当前代码与测试可以核验的实现范围，不是全球唯一性声明。

## 5 关键假设和反证条件

表 2 当前关键假设

<div align="center">

| 假设 | 当前状态 | 反证条件 | 失败后的处理 |
| --- | --- | --- | --- |
| Skill 会在足够多的真实任务中被调用 | 未知 | 长期存在大量未记录任务，记录样本只代表特殊任务 | 把结论限定为 Skill 触发样本，评估宿主级事件接入 |
| 固定分类足以解释主要质量差异 | 假设 | 同类任务内部差异仍大于模型路线差异 | 增加受控枚举或设计配对实验，不增加自由文本 |
| 用户能可靠判断 accepted、回归和范围违规 | 部分支持 | 不同任务或不同评审者口径明显不一致 | 增加验收器来源和复核流程 |
| 宿主会提供可比较的 token 数据 | 未知 | 大量记录为 `unavailable`，或不同宿主口径不一致 | 只分析质量与返工；token 比较保持不可用 |
| 伪标识和小组抑制足以支持公开快照 | 部分支持 | 稀有组合仍可被背景知识重新识别 | 提高最小分组、合并类别或停止公开导出 |

</div>

## 6 最强反对意见

当前采集属于观察性数据，而且只有 Skill 被实际调用时才产生记录。高风险任务更可能使用 Sol，高验证任务更可能使用 Terra 或 Luna；直接比较成功率会把任务选择偏差误当成模型能力差异。整机快照只能描述“在这台机器、这段时间、这些被记录任务中发生了什么”。

因此，当前版本不会自动给出因果排名。达到复核门槛后，如果差异足以改变使用策略，再选择范围稳定、验收器明确的任务做配对或随机交叉实验。

## 7 复核门槛和行动

默认门槛为 50 次完成运行、3 个去标识项目、14 个活跃观察日、至少 2 条各有 10 次记录的实际路线。门槛全部通过后，执行以下行动：

第一步，生成整机去标识快照，并检查缺失率、分类分布和小组抑制情况。

第二步，区分宿主实测、转录计数、估计和不可用的用量来源。

第三步，只对任务分类和验证强度足够接近的路线做描述性比较。

第四步，如果描述性差异会改变路由政策，再单独批准控制实验。

以下事实会提前触发复核：出现任何敏感内容写入记录、删除命令指向过宽目录、公开快照含稳定标识、任务完成却长期无法结束记录，或宿主模型身份无法读回却被标记为实际切换。

## 8 当前剩余未知项

- Sol、Terra、Luna 在用户真实项目中的可持续迭代差异仍然未知。
- medium、high、xhigh 对返工和跑偏的影响仍然未知。
- 本地 Codex JSONL 已验证能够提供累计 input、cached input、output 和 reasoning token，但不同版本与 ChatGPT 对话的字段覆盖仍不一致。
- 50 次运行是否足以支持任何具体比较仍然未知；它只代表第一次人工复核时间点。
- 当前审计已经取得公开先例和用户决策所有者指令，但尚未取得独立人员对本仓库隐私实现的代码审查。

## 9 推荐

推荐发布 `0.3.0`，继续使用仓库外私有目录保存历史派生数据，并从下一次完整项目迭代开始积累可比较结果。历史审计只提供描述性基线，路由政策由前瞻质量门校准。

不推荐现在编写研究论文、公布模型排名、把 `quality_first` 设为所有任务的唯一默认值，或为了取得 token 数而收集提示词和回复正文。

如果首批真实运行显示记录关闭率高、结束记录大量缺失、验收口径不一致或隐私边界难以维持，应暂停比较，先修正采集流程。

## 10 参考资料

[1] ExampleOrg-0, “Add model-effort router skill,” GitHub pull request #1, 2026. [Online]. Available: https://github.com/ExampleOrg-0/Codex-Switcher-Web/pull/1

[2] OpenAI, “Using GPT-5.6,” OpenAI Developer Documentation, 2026. [Online]. Available: https://developers.openai.com/api/docs/guides/latest-model

[3] capitalparser, “codex-model-router,” GitHub, 2026. [Online]. Available: https://github.com/capitalparser/codex-model-router

[4] AlexAI-MCP, “GPT5.6-SOLTELU-Model-Inverter,” GitHub, 2026. [Online]. Available: https://github.com/AlexAI-MCP/GPT5.6-SOLTELU-Model-Inverter

[5] J. Hu et al., “BEST-Route: Adaptive LLM Routing with Test-Time Optimal Compute,” arXiv:2506.22716, 2025. [Online]. Available: https://arxiv.org/abs/2506.22716

[6] A. Cuadron et al., “The Danger of Overthinking: Examining the Reasoning-Action Dilemma in Agentic Tasks,” arXiv:2502.08235, 2025. [Online]. Available: https://arxiv.org/abs/2502.08235

[7] OpenTelemetry Authors, “Gen AI attributes,” OpenTelemetry Semantic Conventions, 2026. [Online]. Available: https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/

[8] OpenTelemetry Authors, “Attribute requirement levels,” OpenTelemetry Semantic Conventions, 2026. [Online]. Available: https://opentelemetry.io/docs/specs/semconv/general/attribute-requirement-level/
