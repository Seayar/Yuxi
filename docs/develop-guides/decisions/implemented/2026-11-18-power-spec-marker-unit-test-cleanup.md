# 电力规范卡片 PDF 标记单测清理

> 2026-11-19 更新：该记录中的“展示层不输出标记”已被 [电力规范卡片 PDF 原文下载](2026-11-19-power-spec-pdf-download.md) 取代；当前实现恢复标记并增加前端鉴权下载。

状态：implemented
类型：testing
Owner：backend/package/yuxi/knowledge/power_spec/answer.py

## 问题

`answer.py::render_srs_cards` 当前把 PDF 标记固定为空，原因是现有 MinIO 地址在浏览器侧不可稳定请求，且第三方测评场景不需要展示原文索引。完整后端 unit 套件因此出现 4 个稳定失败用例；另有 3 个“非法地址不输出标记”的用例虽然通过，但在全局关闭标记后只能重复证明“任何地址都不输出标记”，无法再区分边界校验与全局关闭。

这 7 个用例都只服务已关闭的卡片展示契约。清理后保留一个合法 URL 输入下不输出标记的当前行为 guard；检索层和适配层的 `source_url` 字段仍在当前数据契约中，相关测试不随展示标记一起删除。

## 决策

删除 `test_power_spec_formatting.py` 中 7 个仅验证 PDF 标记展示的用例，并移除只为其中一个用例服务的 `_graph_evidence(source_url=...)` 参数；同时保留一个合法 URL 输入下断言卡片不输出 `[[PDF:...]]` 的当前行为 guard。不修改 `answer.py` 或任何运行时行为。

[2026-11-11 电力规范卡片 PDF 源文档标记](2026-11-11-power-spec-card-pdf-marker.md) 已记录展示断言被本决策部分取代；`source_url` 投影与数据获取契约仍由对应 repository/adapter 测试守护。

旧能力不存在：卡片展示层不再输出 `[[PDF:...]]` 标记。

重新引入条件：若未来恢复卡片 PDF 标记，必须同时恢复多卡片地址归属、图谱补卡地址归属、相对地址、空地址、非法边界和内部协议拒绝的用例，并先更新决策记录说明前端可请求地址的生成方式。

## 替代方案

- 恢复 `answer.py` 的 PDF 标记输出。这会改变当前用户可见行为，并要求先解决浏览器侧可请求地址问题，超出本次测试清理范围。
- 只删除 4 个失败用例。保留的 3 个负向用例不能证明当前边界逻辑，只会继续给全局关闭行为提供重复覆盖。
- 继续保留失败用例。完整 unit gate 会长期红，掩盖后续真实回归。
- 删除 repository/adapter 的 `source_url` 测试。`source_url` 仍是当前投影字段，数据边界测试不依赖展示标记是否启用。

## 后果

- 恢复 PDF 标记会被当前行为 guard 立即报警；重新引入时必须先更新决策，并补回多卡片地址归属、图谱补卡地址归属、相对地址和边界拒绝测试。
- 标准单测容器仍会跳过 config 契约测试；这是容器未挂载仓库根的环境事实，不代表测试失效。挂载根目录后该目录 88 项全部通过。
- 旧决策的展示断言不再作为当前事实；数据投影部分的验证继续有效。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 后端 unit 套件不再被旧 PDF 标记契约阻塞 | 恢复标记相关断言或删除无关测试 | `power_spec/answer.py` | `docker compose exec api python -m pytest test/unit -m "not slow" -q` | 合法地址误输出 PDF 标记时当前行为 guard 失败 | Passed：3233 passed、54 skipped、0 failed |
| 保留的格式化行为仍通过 | 卡片字段、顺序、Markdown/HTML 保真被破坏 | `power_spec/answer.py` | `docker compose exec api python -m pytest test/unit/knowledge/test_power_spec_formatting.py -q` | 删除或改坏卡片字段渲染时失败 | Passed：15 passed |
| `source_url` 数据契约不随展示清理丢失 | repository/adapter 投影不再携带字段 | `power_spec_repository.py`、`power_spec/retrieval.py` | `docker compose exec api python -m pytest test/unit/knowledge/test_power_spec_repository.py test/unit/knowledge/test_power_spec_adapters.py -q` | 字段丢失时失败 | Passed：15 passed |
| config 契约测试不属于失效用例 | Compose、nginx 或初始化契约回归 | `docker-compose.yml`、`docker/nginx/nginx.conf` | `docker run --rm -v D:\Yuxi:/workspace -w /workspace/backend yuxi-api:0.7.3 python -m pytest test/unit/config -q` | 挂载根目录后契约破坏时失败 | Passed：88 passed |

执行记录补充：`pnpm run test:unit` 为 363 passed、0 failed、0 skipped；CLI 独立测试在现有后端镜像加 `PYTHONPATH=src` 下运行 71 项通过，3 个导入 `questionary` 的测试文件因镜像缺该依赖未运行。Ruff 未运行，当前容器没有 ruff 可执行文件。
