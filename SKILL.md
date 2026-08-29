---
name: model-effort-router
description: Recommend and lock the concrete next project segment to an exact GPT Pro, Sol, Terra, or Luna reasoning route, require a verified handoff before model changes, and collect consented local outcome metadata for route and switch-loss evaluation. Use throughout ongoing project work so each delivery ends with the next task and model recommendation. Do not record prompts, code, file names, paths, logs, secrets, or model outputs, and do not claim that a recommendation changed the host model without readback evidence.
---

# Model Effort Router

## 1 默认交付协议

每次完成一轮项目交付后，在最终回复末尾追加以下两行：

```text
下一段：<一个具体、可执行的下一阶段任务>
建议模型：<GPT Pro | Sol | Terra | Luna> <low | medium | high | xhigh>
```

必须遵守以下规则：

- 模型名称始终使用 `GPT Pro`、`Sol`、`Terra`、`Luna`，禁止翻译、音译或改写名称
- 默认收尾只保留这两行，禁止追加理由、备选路线、升级条件、当前实际路线、遥测状态、运行 ID、统计门槛或重复验收结果
- 下一段必须是根据当前项目状态推导出的最小有价值任务，存在安全且明确的后续工作时，禁止写“等待用户指定”
- 建议模型必须对应下一段任务，不能把当前宿主模型写成下一段建议
- 用户明确要求模型比较、路由解释、统计状态或完整任务合同时，可以在正文展开；回复末尾仍使用两行收尾
- 本地采集发生需要用户处理的异常时，在两行收尾之前增加一句短警告；正常采集保持静默

## 2 内部执行闭环

### 2.1 建立任务合同

在内部确认以下内容，不默认展示：

- 当前目标和非目标
- 允许修改范围
- 验收检查
- 可逆性、影响范围和验证强度
- 证据冲突与已经失败的不同假设

### 2.2 选择路线

运行 `scripts/recommend.py`，或按照 `references/routing-policy.md` 选择满足验收门的最低充分路线

主路线用于执行和本地记录；备选路线只在用户要求比较时展示

### 2.3 锁定任务段

一个任务段由同一目标、冻结决定、允许范围、起点和验收条件组成

任务段开始后，模型和 effort 保持不变；每次回复末尾的建议模型只针对下一任务段，不能覆盖仍在执行的活动任务段

本地状态已经初始化时，使用 `scripts/segment_guard.py`：

```powershell
# 在仓库外初始化任务段锁的私有状态
python scripts/segment_guard.py --pretty init

# 用完整合同摘要建立活动任务段和精确路线锁
python scripts/segment_guard.py --pretty start --project-key <private-key> --task-key <private-key> --phase routine_implementation --execution-shape continuous_iteration --model terra --effort high --contract-file <private-contract.json>

# 当前回合准备使用模型前检查是否符合活动任务段路线锁
python scripts/segment_guard.py --pretty check --segment-id <segment-id> --model terra --effort high
```

只有以下边界允许准备切换：

- 里程碑已通过并且强制验收全部通过
- 当前任务阻塞，同时两个不同假设失败、证据冲突或风险上升

活动任务段推荐路线发生变化时，继续当前锁定路线，或先建立检查点和交接；禁止直接把新建议解释为已经切换

完整状态机和隐私边界见 `references/segment-continuity-policy.md`

### 2.4 启动本地记录

用户已经启用本地采集时，在首次修改前启动记录：

```powershell
# 在首次修改前创建本轮去标识化记录
python scripts/telemetry.py start --workspace . --policy guarded_high --task-class routine_implementation --recommended-model terra --recommended-effort medium --actual-model terra --actual-effort medium --context-mode compressed_handoff --pretty
```

保存返回的 `run_id`

采集关闭时继续任务，不把采集状态加入正常收尾

### 2.5 执行与验收

完成合同范围内的工作并运行约定检查

验收通过后停止；修改将超出允许范围、需要未授权外部动作或两个不同假设均失败时，停止并重新路由

### 2.6 结束本地记录

无论任务成功、拒绝、取消或出错，都结束同一个运行记录：

```powershell
# 把可验证结果写入同一个运行记录，宿主没有令牌计数时保留 unavailable
python scripts/telemetry.py finish --run-id <run-id> --workspace . --status accepted --tests-run 5 --tests-passed 5 --tests-failed 0 --token-source unavailable --pretty
```

## 3 路由预设

### 3.1 guarded_high

默认使用此策略：

- 立项收敛：`Sol xhigh`
- 首个可运行版本：`Sol high`，架构未决时使用 `Sol xhigh`
- 日常实现：`Terra medium`，跨模块不变量或弱验证时使用 `Terra high`
- 复杂实现：`Terra high`，边界明确且出现非显然失败时使用 `Terra xhigh`
- 常规计划和审查：`Sol high`
- 清晰机械工作：`Luna medium`

### 3.2 quality_first

错误代价显著高于时延和用量时使用：

- 立项收敛、不可逆裁决和发布红队：`Sol xhigh`
- 首个可运行版本：`Sol xhigh`
- 日常实现：`Terra high`
- 设计冻结的复杂实现：`Terra xhigh`
- 清晰机械工作：`Luna medium`

### 3.3 balanced

测试完善且任务合同稳定时使用：

- 立项收敛：`Sol high`
- 日常实现：`Terra medium`
- 复杂实现和困难调试：`Terra high`
- 跨系统或发布审查：`Sol high`
- 机械工作：`Luna low` 或 `Luna medium`

## 4 升级判断

以下任一条件成立时，让 `Sol xhigh` 重新收敛判断：

- 需求、测试和现有实现互相冲突
- 两个不同根因假设经过检验后均失败
- 决策改变公共接口、数据、安全边界或部署拓扑，而且回滚代价高
- 多个证据来源冲突，需要跨模块裁决
- 最终高价值红队需要主动寻找遗漏
- 不可逆性高且验证能力弱

设计和接口已经冻结时，复杂实现优先使用 `Terra high` 或 `Terra xhigh`

`Luna` 只处理目标、输出格式、修改范围和机械验收方式都清楚的工作

## 5 隐私和宿主边界

- 本地统计默认关闭，用户执行 `telemetry.py enable` 后才开始记录
- 记录只保存在本机，脚本没有网络上传功能
- 原始提示词、对话、代码、文件名、文件路径、差异内容、日志、错误文本、账号和秘密禁止进入记录
- 项目路径通过机器本地随机盐生成稳定化名，原始路径不写入数据集
- 宿主没有提供令牌或工具调用计数时保存 `null`，禁止用 `0` 冒充实测值
- 路由权限只覆盖模型与推理档位建议，不授权部署、删除、付款、外部消息或生产写入
- 推荐只在宿主完成模型写入并读回精确设置后才能标记为已切换

## 6 分析触发器

需要检查整机样本或生成脱敏快照时，读取 `references/observation-protocol.md`

在达到配置门槛前，校准状态保持 `policy_based_uncalibrated`，不生成模型胜率、因果结论或论文结论

### 6.1 本地历史审计

用户明确要求审计本机 Codex 历史时，使用 `scripts/history_audit.py`

完整纵向运行：

```powershell
# 输出目录必须位于公开仓库外
python scripts/history_audit.py run --output-dir <private-output-directory> --pretty
```

必须遵守以下边界：

- 源 JSONL 只读，运行前后核对来源数量、大小和修改时间
- 同一 session 与 turn 的迁移、归档和 fork 观察只计一次
- 每轮只使用最后一个累计 token 快照，再减去同一 session 上一轮快照
- 原始提示词、回复、代码、日志、文件名、路径、URL、邮箱、账号和秘密不得进入派生文件
- 原文只允许在内存中参与确定性分类，落盘摘要只使用固定标签
- `likely_overrouted` 是历史疑似过度路由，不能写成已经验证的低路线结论
- `lower_route_validated` 只能来自通过质量门的前瞻可比较样本

建立试运行：

```powershell
python scripts/history_audit.py prospective --action init --output-dir <private-output-directory> --pretty
```

试运行出现严重缺陷、范围违规或降档回归时，受影响任务类别恢复上一档并进入人工复核

### 6.2 少样本配对评测

用户明确要求比较 Sol、Terra 或 Luna 的真实能力差异时，读取 `references/paired-evaluation-policy.md`，使用 `scripts/paired_eval.py`

配对评测只回答某个任务单元的预设路线是否足够，不生成全局模型排名

首次初始化：

```powershell
# 在仓库外创建最多 24 对任务的私有评测状态
python scripts/paired_eval.py --pretty init
```

每个任务先用 `plan` 冻结任务单元并随机分配 A/B，再分别执行两条路线

执行完成后，`attach` 必须从两份 Codex JSONL 的 `turn_context` 读回精确模型与 effort；缺失读回、路线不符或遥测冲突时整对无效

`blind` 只生成匿名评审合同，不复制提示词、输出、代码、日志或路径

`judge` 只接受固定布尔值、非负计数和五项 `0–4` 分，拒绝备注和其他自由文本

判定边界：

- `preset_sufficient`：同一任务单元取得 4 对实用等效结果，覆盖至少 2 个项目和 2 种执行形态
- `surface_only`：有限认知评审接受预设路线，深度评审发现高端路线避免的实质缺陷
- `material_gap`：高端路线通过、预设路线失败，并在反序复测中重现
- `both_failed`：两条路线均未通过硬验收，不能归因于预设路线
- `indeterminate`：证据不足或仍有冲突，保持当前策略

评测最多分配 24 对任务，不自动创建 Codex 任务，不授权重复执行外部写入，也不把 GPT Pro 调查混入首轮比较

### 6.3 模型切换损失评测

用户明确要求验证模型切换造成的恢复成本或质量变化时，读取 `references/switch-loss-evaluation-policy.md`，使用 `scripts/switch_eval.py`

切换评测比较同一检查点的两条路线：

- `continuation`：保持原模型和 effort 继续执行
- `switched`：使用通过 `segment_guard.py handoff` 生成的交接合同切换到目标路线

每条路线必须从同一检查点独立执行；`attach` 从各自 Codex JSONL 读回实际路线，缺失或不匹配时整对无效

评审只保存固定字段，包括接受状态、恢复时间、重复动作、纠正次数、上下文缺失、五项评分和硬验收标记

判定边界：

- `no_material_switch_loss`：切换路线在全部质量和恢复阈值内达到连续路线结果
- `recoverable_switch_loss`：两边都完成，但切换产生超过阈值的恢复、重复、纠正或上下文缺失
- `material_switch_loss`：连续路线通过而切换路线硬失败，并在反序复测中重现
- `switch_benefit`：切换路线通过而连续路线失败，并在反序复测中重现
- `both_failed`：两条路线共同失败，不能归因于切换
- `indeterminate`：证据不足或冲突，保持当前任务段锁策略

强制用户评审无法取得时，使用 `resolve-review --disposition unavailable` 关闭待处理状态；该配对继续保持 `indeterminate`，不得计入路线完成门，后续真实用户评审可以覆盖此状态

每个精确路线转换最多 4 对，全部实验最多 12 对；结论升级还需要覆盖至少 2 个项目和 2 个阶段

## 7 进一步阅读

- 模型、推理档位和硬门：`references/routing-policy.md`
- 采集字段、保存边界和删除路径：`references/telemetry-policy.md`
- 真实项目观察与整机分析准备：`references/observation-protocol.md`
- 本地历史清点、派生字段和解释边界：`references/history-audit-policy.md`
- 同题配对、三层盲评和完成门：`references/paired-evaluation-policy.md`
- 任务段模型锁、交接和路线读回：`references/segment-continuity-policy.md`
- 连续执行与切换执行的配对评测：`references/switch-loss-evaluation-policy.md`
