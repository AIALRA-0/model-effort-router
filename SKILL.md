---
name: model-effort-router
description: Route the next real project iteration across GPT Pro research and GPT-5.6 Sol, Terra, or Luna with an appropriate reasoning effort, then collect consented local outcome metadata for later machine-level analysis. Use when choosing a model or effort, running a long project as bounded iterations, comparing high with xhigh, or building evidence from actual project work. Do not record prompts, code, file names, paths, logs, secrets, or model outputs, and do not claim that a recommendation changed the host model without readback evidence.
---

# Model Effort Router

## 1 目标

为下一轮真实项目工作选择满足验收门的最低充分模型与推理档位

每次启用本 Skill 时，把推荐路线、实际路线、验收结果和可取得的用量计数写入用户已经授权的本地数据目录

当前数据只用于描述真实执行情况

样本达到配置门槛后再启动整机统计分析，不提前生成模型胜率、因果结论或论文结论

## 2 隐私和授权边界

- 本地统计默认关闭，用户执行 `telemetry.py enable` 后才开始记录
- 记录只保存在本机，脚本没有网络上传功能
- 原始提示词、对话、代码、文件名、文件路径、差异内容、日志、错误文本、账号和秘密禁止进入记录
- 项目路径通过机器本地随机盐生成稳定化名，原始路径不写入数据集
- 宿主没有提供令牌或工具调用计数时保存 `null`，禁止用 `0` 冒充实测值
- 路由权限只覆盖模型与推理档位建议，不授权部署、删除、付款、外部消息或生产写入
- 推荐只在宿主完成模型写入并读回精确设置后才能标记为已切换

## 3 每次执行的固定闭环

- 第一步，建立下一轮任务合同：

  - 目标和非目标
  - 允许修改范围
  - 验收检查
  - 可逆性、影响范围和验证强度
  - 证据冲突与已经失败的不同假设

- 第二步，运行 `scripts/recommend.py` 或按 `references/routing-policy.md` 选择一个主路线

- 第三步，在首次修改前启动本地记录：

```powershell
# 在用户已经启用本地采集后，为本轮实际路线创建去标识化记录
python scripts/telemetry.py start --workspace . --policy guarded_high --task-class routine_implementation --recommended-model terra --recommended-effort medium --actual-model terra --actual-effort medium --context-mode compressed_handoff --pretty
```

保存返回的 `run_id`

如果返回 `telemetry_status: disabled`，继续完成用户任务，并在最终结果中如实报告本轮没有采集

- 第四步，执行任务并运行合同中约定的验收检查

- 第五步，无论任务成功、拒绝、取消或出错，都结束同一个运行记录：

```powershell
# 把可验证结果写入同一个运行记录，宿主没有令牌计数时保留 unavailable
python scripts/telemetry.py finish --run-id <run-id> --workspace . --status accepted --tests-run 5 --tests-passed 5 --tests-failed 0 --token-source unavailable --pretty
```

- 第六步，在最终回复中报告实际路线、验收结果和 `telemetry_status`

## 4 路由预设

### 4.1 quality_first

错误代价显著高于时延和用量时使用

- 立项收敛、不可逆裁决和发布红队使用 Sol xhigh
- 首个可运行版本使用 Sol xhigh
- 日常实现使用 Terra high
- 设计已经冻结的复杂实现使用 Terra xhigh
- 清晰机械工作使用 Luna medium

### 4.2 guarded_high

默认策略，用可观察证据控制升级

- 立项收敛使用 Sol xhigh
- 首个可运行版本默认 Sol high，架构未决时升 Sol xhigh
- 日常实现默认 Terra medium，跨模块不变量或弱验证时升 Terra high
- 复杂实现默认 Terra high，边界明确且出现非显然失败时升 Terra xhigh
- 常规计划和审查使用 Sol high，命中第5节条件时升 Sol xhigh
- 清晰机械工作使用 Luna medium

### 4.3 balanced

测试完善且任务合同稳定的成熟项目使用

- 立项收敛使用 Sol high，高风险证据冲突时升 xhigh
- 日常实现使用 Terra medium
- 复杂实现和困难调试使用 Terra high
- 跨系统或发布审查使用 Sol high
- 机械工作使用 Luna low 或 medium

## 5 Sol xhigh 升级条件

以下任一条件成立时，让 Sol xhigh 处理判断或重新收敛

- 需求、测试和现有实现互相冲突
- 2个不同根因假设已经得到执行检验并全部失败
- 决策改变公共接口、数据、安全边界或部署拓扑，而且回滚代价高
- 多个证据来源冲突，需要跨模块裁决
- 最终高价值红队需要主动寻找遗漏
- 不可逆性高且验证能力弱

设计和接口已经冻结时，复杂实现优先留在 Terra high 或 Terra xhigh

Luna 只处理目标、输出格式、修改范围和机械验收方式都清楚的工作

## 6 分析触发器

运行以下命令查看当前整机样本是否达到复审门槛

```powershell
# 检查本机记录数量、活跃项目、观察天数和可比较路线数量
python scripts/telemetry.py status --pretty
```

默认门槛来自 `config/collection-policy.json`

- 已完成运行达到50次
- 去标识化项目达到3个
- 活跃观察日达到14天
- 至少2条路线各有10次记录

这些数值是启动人工统计复审的操作门槛，不代表统计功效已经充分

达到门槛后生成整机脱敏快照

```powershell
# 导出不含项目标识、机器标识、路径和原始内容的整机聚合数据
python scripts/telemetry.py snapshot --output machine-snapshot.json --pretty
```

公开快照中样本少于5次的分组隐藏质量比率

## 7 结果合同

每次路由输出一个主推荐和最多2个不重复备选，并包含：

- 推荐与实际模型、推理档位
- 任务类型和策略预设
- 推荐所依据的具体证据
- 允许范围、验收门和停止条件
- 升级和降档触发器
- 宿主切换状态
- 本地采集状态与运行 ID

本地样本未达到门槛时，校准状态保持 `policy_based_uncalibrated`

## 8 进一步阅读

- 模型、推理档位和硬门：`references/routing-policy.md`
- 采集字段、保存边界和删除路径：`references/telemetry-policy.md`
- 真实项目观察与整机分析准备：`references/observation-protocol.md`
