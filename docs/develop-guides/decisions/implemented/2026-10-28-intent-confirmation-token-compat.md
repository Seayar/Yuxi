# 意图确认指纹兼容不回传选项值的客户端

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/power_spec/intent.py

## 问题

确认卡片把“计划指纹”编码在选项值里（`confirm#<hash>`），恢复时用它比对重算出的计划，避免执行用户没有确认过的另一份计划。

线上复现了两个独立缺陷：

1. 指纹把模型每次都会变的字段（`normalized_terms`、`unknown_terms`、`normalized_query`）也算进去。本部署的 `local:Qwen3.6-35B-A3B-w8a8` 在 `temperature=0` 下同一提示词连跑三次，`normalized_terms` 每次不同（`DGA` / `检修规程` / 缺失），重放必然算出不同指纹，用户每次点“正确”都收到“意图识别结果与已确认内容不一致”。
2. 校验实现为“指纹缺失即判为不一致”，不回传选项值的客户端（预构建的 `new-web`）被永久挡在确认循环里：每次点击都失败，模型重新调用工具再次中断，对话无法继续。

## 决策

- 指纹只覆盖确认卡片上用户实际看到并确认的字段：电力规范为查询类型/设备类型/电压等级/问题维度（维度按集合比较），故障溯源为设备名称/设备类型/电压等级/图片识别结果/故障现象（现象按集合比较）。模型生成的关键词、重述句不参与。
- 恢复值带指纹时严格比对：与重算计划不一致 → 返回固定失败文案，不执行新计划。
- 恢复值没有指纹时按已确认放行，并写一条 warning 日志（客户端未回传选项值）。
- 两个智能体预设的系统提示补充一条：工具返回“意图识别结果与已确认内容不一致”时只能原样转述并让用户重新描述，禁止再次调用工具。

指纹仍然由服务端生成、随 checkpoint 往返；具备回传能力的客户端（`web/` 的 Vue 确认卡片）继续受严格校验保护。

## 替代方案

- **保持“缺失即拒绝”**：安全性更高，但会把不回传选项值的客户端锁死，线上已经复现，放弃。
- **服务端保存确认快照并在重放时比对**：需要跨 Run 的持久快照（tool 中断时没有返回 `Command`，LangGraph state 拿不到），改动面与失败模式都更大，本回合不做。
- **移除确认卡片，改为工具内直接放行**：丢失用户确认语义，放弃。

## 后果

- 不回传指纹的客户端重新可用；代价是该客户端上的“重放重算漂移”不再被阻断，只留 warning。
- 回传指纹的客户端行为不变，负向用例继续覆盖“计划变化 → 固定失败文案”。

## 验证

- `backend/test/unit/knowledge/test_power_spec_tool_intent.py`：缺指纹放行、指纹不一致拒绝、易变字段不影响指纹、维度顺序无关。
- `backend/test/unit/knowledge/test_fault_trace_intent.py`：现象顺序/重复不影响指纹，设备或现象变化可检出。
- `backend/test/unit/knowledge/test_fault_trace_confirmation_replay.py`：真实 LangGraph 中断/恢复下指纹往返与漂移可检出。
- `backend/test/unit/knowledge/test_power_spec_intent_interrupt_loop.py`：真实重放循环仍按确认继续。
- 服务器实测（真实 `local:Qwen3.6-35B-A3B-w8a8` 连续三次识别）：指纹一致；`plain` 与 `token` 两种确认都能继续，篡改指纹仍被拒绝。
- 命令：`docker compose exec api uv run --group test pytest test/unit/knowledge/test_power_spec_*.py test/unit/knowledge/test_fault_trace_*.py`。
