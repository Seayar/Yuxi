# 在故障溯源与设备预测报告中展示服务返回图片

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/fault_trace/answer.py

## 问题

YOLO 故障识别服务与 LSTM 故障预测服务已在成功响应中增加可选 `image_url` 字段。字段内容是完整的 Markdown 图片，例如 `![预测图](/api/data/image?objectName=...)`。此前客户端在协议转译时丢弃该字段，报告装配层也只渲染原有摘要与趋势，用户无法看到故障识别图片或预测数据图片。

需求指定两个位置：故障溯源报告在“① 故障摘要 / 涉及设备”之后插入故障识别图片；设备预测维护报告在“二、故障预测 / 退化趋势”之后插入预测数据图片。字段为空时不得输出空图片、占位文本或额外空行。

## 决策

1. `yuxi/services/yolo_service.py` 与 `yuxi/knowledge/device_prediction/services.py` 将 `image_url` 作为可选响应字段解析。字段缺省、`null`、空串或纯空白归一为空；字段出现但不是字符串时按上游响应不合法处理。
2. 故障溯源把 YOLO 结果的 `image_url` 写入本次图片证据，并在 `build_fault_trace_payload` 的 `summary.image_url` 中保留；`answer_markdown` 在涉及设备行之后原样插入非空值。该插入点由 `fault_trace/answer.py` 拥有。
3. 设备预测维护把 LSTM 结果的 `image_url` 写入 `FaultPrediction`、`PredictionReport` 和最终结构化载荷；`verify_report` 要求报告字段与证据一致，Markdown 在退化趋势行之后原样插入非空值。该插入点由 `device_prediction/answer.py` 拥有。
4. 不解析、不改写或重新编码 Markdown 内容。当前消费者需要服务提供的完整展示文案，图片 URL 中的编码与转义都由产生该字段的服务拥有。

## 替代方案

- 只在前端遇到 `image_url` 时拼接图片：前端拿不到工具内部的服务响应，最终消息只消费 `answer_markdown`，需要新增跨层协议且会让报告 JSON 与正文不一致。
- 从 `image_url` 中提取 URL 后再包装 Markdown：会改变服务已经提供的 alt 文本、转义和查询参数，违背直接插入正文的要求。
- 将空值渲染成“图片未返回”：需求明确只需要非空字段，空值不应产生用户可见内容。

## 后果

- 两个报告的 Markdown 只在字段非空时增加一行图片；旧服务不返回该字段时，现有正文和空行结构保持不变。
- `image_url` 是上游提供的 Markdown 原文；当前信任边界是管理员配置的外部服务响应。客户端只校验字段类型，不尝试净化正文。
- 设备预测的结构化报告新增 `image_url`，并纳入证据校验，篡改图片字段会使 `answer_verified` 为 `False`。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 两个服务客户端保留非空 `image_url`，空值可省略 | 字段被丢弃、对象被字符串化或字段缺失导致整份报告失败 | `yolo_service.py`、`device_prediction/services.py` | `test/unit/services/test_yolo_service.py`、`test/unit/knowledge/test_device_prediction_services.py` | `image_url` 为非字符串时按响应不合法失败 | Passed |
| 故障识别图片只出现在“涉及设备”之后 | 图片被放在摘要前、推理过程后或追加到结构化字段之外 | `fault_trace/answer.py`、`fault_trace/workflow.py` | `test/unit/knowledge/test_fault_trace_answer.py`、`test/unit/knowledge/test_fault_trace_tool.py` | 空 `image_url` 仍输出图片或空行时失败 | Passed |
| 预测数据图片只出现在“退化趋势”之后 | 图片被放入表格、维护建议或错误章节 | `device_prediction/answer.py`、`device_prediction/tool.py` | `test/unit/knowledge/test_device_prediction_answer.py`、`test/unit/knowledge/test_device_prediction_tool.py` | 空 `image_url` 仍输出图片或空行时失败 | Passed |
| 预测报告校验覆盖图片字段 | 报告证据被篡改成另一张图仍标记为已校验 | `device_prediction/answer.py::verify_report` | 篡改 `image_url` 的负向 unit | 篡改后 `answer_verified` 仍为 `true` 时失败 | Passed |
| 最终正文的 Markdown 图片语法可生成图片节点 | 图片语法被当成普通文本或路径被改写 | `web/src/utils/markdown_preview.js` | `web/test/unit/markdown_preview_fallback.test.js` | 渲染结果无 `<img>` 或原查询参数丢失时失败 | Passed |

聚焦后端测试命令与结果：

```text
docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_yolo_service.py test/unit/knowledge/test_device_prediction_services.py test/unit/knowledge/test_device_prediction_answer.py test/unit/knowledge/test_device_prediction_tool.py test/unit/knowledge/test_fault_trace_answer.py test/unit/knowledge/test_fault_trace_tool.py -q -p no:cacheprovider
159 passed in 233.74s
```

前端渲染测试：

```text
docker compose exec -T web pnpm exec node --test test/unit/markdown_preview_fallback.test.js
6 passed
```

未验证范围：真实 YOLO/LSTM 服务响应与真实 `/api/data/image` 对象可达性未在本次环境中探测；生产页面未连接真实图片来源做视觉截图。
