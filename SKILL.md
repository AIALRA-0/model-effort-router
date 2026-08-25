---
name: model-effort-router
description: Recommend the concrete next project segment and an exact GPT Pro, Sol, Terra, or Luna reasoning route after every project delivery, while collecting consented local outcome metadata for later machine-level analysis. Use throughout ongoing project work so each completed iteration ends with the next task and model recommendation. Do not record prompts, code, file names, paths, logs, secrets, or model outputs, and do not claim that a recommendation changed the host model without readback evidence.
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

### 2.3 启动本地记录

用户已经启用本地采集时，在首次修改前启动记录：

```powershell
# 在首次修改前创建本轮去标识化记录
python scripts/telemetry.py start --workspace . --policy guarded_high --task-class routine_implementation --recommended-model terra --recommended-effort medium --actual-model terra --actual-effort medium --context-mode compressed_handoff --pretty
```

保存返回的 `run_id`

采集关闭时继续任务，不把采集状态加入正常收尾

### 2.4 执行与验收

完成合同范围内的工作并运行约定检查

验收通过后停止；修改将超出允许范围、需要未授权外部动作或两个不同假设均失败时，停止并重新路由

### 2.5 结束本地记录

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

## 7 进一步阅读

- 模型、推理档位和硬门：`references/routing-policy.md`
- 采集字段、保存边界和删除路径：`references/telemetry-policy.md`
- 真实项目观察与整机分析准备：`references/observation-protocol.md`
