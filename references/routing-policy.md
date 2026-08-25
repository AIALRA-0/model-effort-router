# 路由策略与决策合同

## 1 决策对象

本路由器同时处理三层选择

| 层 | 问题 | 典型错误 |
| --- | --- | --- |
| 模型层 | Sol、Terra、Luna 谁更适合 | 把 Luna 当项目负责人，或让 Sol 做全部机械工作 |
| effort 层 | medium、high、xhigh 是否值得 | 把推理时长当成质量保证，或在长程关键步骤欠推理 |
| 路由层 | 当前分类和升级条件是否正确 | 因关键词“重要”盲目升档，或错过证据冲突和不可逆风险 |

评测时必须分别记录三类错误

## 2 输入字段

所有数值采用 `0..4`

| 字段 | 含义 |
| --- | --- |
| `ambiguity` | 目标和实现路径的不确定程度 |
| `complexity` | 依赖、步骤和不变量的复杂程度 |
| `blast_radius` | 错误能够影响的范围 |
| `irreversibility` | 回滚困难程度 |
| `verification_strength` | 测试、验收器和复现证据强度，数值越高越强 |
| `evidence_conflict` | 需求、测试、实现或来源是否冲突 |
| `failed_hypotheses` | 已经执行验证且失败的不同假设数量 |
| `cross_module` | 是否跨模块 |
| `public_interface_change` | 是否改变公开接口或数据结构 |
| `security_or_data_boundary` | 是否改变安全、隐私或数据边界 |
| `deployment_topology_change` | 是否改变部署拓扑 |
| `final_red_team` | 是否是发布前主动找遗漏的高价值审查 |

## 3 任务阶段

- `initial_research`
- `project_convergence`
- `first_runnable`
- `routine_implementation`
- `complex_implementation`
- `debugging`
- `planning`
- `review`
- `evaluation`
- `decision`
- `release_review`
- `mechanical`
- `batch_edit`
- `log_summary`
- `format_conversion`
- `test_execution`

## 4 硬门优先于平均分

不要把所有维度平均后掩盖一个严重信号

### Sol xhigh 判断门

- `evidence_conflict = true` 且任务需要计划、裁决或跨模块判断
- `failed_hypotheses >= 2`
- 安全或数据边界改变且仍有歧义
- 高不可逆性与弱验证同时出现
- 最终红队审查
- 公共接口或部署拓扑改变，且两个以上方案都有合理证据

### Terra xhigh 实现门

- 设计和接口已经冻结
- 任务复杂、跨模块且含细微不变量
- 验证不够强，或 Terra high 的一个非显然假设已经失败
- 工作仍是有边界的实现，不需要重做产品或架构裁决

### Luna 门

只有以下条件同时满足时使用 Luna

- 目标和输出格式清楚
- 修改可机械验证
- 爆炸半径低
- 没有隐藏架构判断
- 失败后可廉价重做

## 5 阶段矩阵

| 阶段 | 默认主路线 | 升级 | 降档 |
| --- | --- | --- | --- |
| 外部调查 | GPT Pro + web/deep research | 来源冲突或高风险裁决交 Sol xhigh | 只做固定资料提取时可 Luna medium |
| 立项收敛 | Sol xhigh | max/ultra 仅在宿主支持且用户明确授权，并有 eval 证据 | 规格已冻结的后续工作交 Sol high 或 Terra |
| 首个可运行版本 | Sol high | 架构或接口未决时 Sol xhigh | 冻结纵向切片且测试强时 Terra high |
| 日常实现 | Terra medium | 跨模块不变量、弱测试时 Terra high | 机械补丁可 Luna medium |
| 复杂实现 | Terra high | 有边界但细微时 Terra xhigh；重新裁决时 Sol xhigh | 拆分后普通子任务 Terra medium |
| 调试 | Terra high | 两个不同假设失败后 Sol xhigh | 报错定位明确且强复现时 Terra medium |
| 常规审查 | Sol high | 高风险最终红队 Sol xhigh | 局部固定检查 Terra high 或 Luna medium |
| 批量机械工作 | Luna medium | 多工具协调或发现隐藏依赖时 Terra medium | 单文件固定转换 Luna low |

## 6 为什么首版不必永远 Sol xhigh

首个可运行版本包含两种不同任务

1. **仍在确定架构的首版**：需要 Sol xhigh
2. **按照冻结合同实现纵向切片**：Sol high 通常是更稳的默认值，复杂实现可交 Terra high

把二者放在同一个超长 xhigh 任务中，会让旧假设持续占用上下文，并增加继续搜索、顺手改造和重复工具调用的机会

## 7 防止“用户总选更高模型”

- 只展示一个主推荐和最多两个备选
- 主推荐由用户预设策略决定，不按模型强弱排序
- 备选使用“质量优先、受控高推理、均衡”命名
- 每个 xhigh 推荐必须列出触发证据
- 同时列出降档证据
- 不显示伪造的成功概率
- 本地数据不足时写 `policy_based`
- 优先记录真实执行结果，影子运行留到用户另行授权的受控实验阶段
- 选择“成功率置信下界满足验收阈值”的最低充分路线，而非平均分最高但不稳定的路线

## 8 停止条件

每个下一轮任务至少包含

- 验收条件全部通过即停止
- 修改将超出 `allowed_scope` 时停止并重新路由
- 需要外部写入、删除或不可逆动作但未获授权时停止
- 两个不同假设失败时停止同档位第三次盲试
- 工具循环达到项目设定阈值时生成证据摘要并新开任务

## 9 宿主适配

### 只有建议能力

输出 `recommendation_only`
不要声称已经改变当前线程或子任务的模型

### 可写模型但不可读回

输出 `switch_available_but_unconfirmed`
执行前仍应要求宿主返回所选模型与 effort

### 可写且可读回

只有读回精确匹配后输出 `confirmed_switched`

模型路由动作不授予其他外部写权限
