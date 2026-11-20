# 电力规范卡片 PDF 原文下载

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/power_spec/answer.py

## 问题

规范卡片的 `answer_markdown` 已有 `[[PDF:<url>]]` 标记协议，但知识库文件记录中的 MinIO 地址在浏览器侧不可稳定请求：`minio` 是 Compose 内网主机名，`knowledgebases` bucket 也不是公开桶。普通链接也不会携带当前登录凭据，直接展示 MinIO 地址会造成 404、403 或内部地址泄露。

用户需要从每张规范卡片直接下载对应原 PDF。下载目标由标准号映射到共享 `powerspec` 原文件，后端以登录身份为访问边界。

## 决策

`PowerSpecRepository.list_power_spec_extractions` 从规范卡片元数据读取 `standard_no`，归一化后生成固定下载地址：

```text
/api/knowledge/power-spec/pdfs/<标准号归一化文件名>.pdf
```

文件名使用大写标准号，移除空白，`/` 替换为 `_`，例如 `DL/T 408-2023` 映射为 `DL_T408-2023.pdf`。下载路由从私有 `powerspec` bucket 根目录读取同名对象；不依赖 `knowledge_files` 的 PDF 记录，也不引用 Markdown 条款载体。

`answer.py` 把该地址写入每张卡片末尾的 `[[PDF:<url>]]` 标记。前端按卡片边界解析标记并携带登录凭据下载。后端路由要求已登录用户，只接受安全的标准号文件名，并固定读取共享 `powerspec` bucket，不接受任意 MinIO 对象路径，也不追加单知识库读权限查询。

前端只接受上述知识库下载路径。`SearchPowerSpecTool` 按卡片边界解析标记并展示下载按钮；最终 Markdown 的标记渲染为“下载 PDF 原文”链接。两者都通过统一 API 封装携带登录凭据请求 blob，再使用 `Content-Disposition` 文件名触发浏览器下载。外部 URL、原始 MinIO URL、空地址和非法标记不生成下载入口。

## 替代方案

1. 直接返回 `minio_url` 或 `path`。`minio` 主机名在浏览器不可达，私有 bucket 会拒绝匿名请求，且会泄露内部存储地址。
2. 把 `knowledgebases` bucket 改成公开读。链接可直接打开，但绕过知识库权限并暴露所有对象。
3. 生成 MinIO 预签名 URL。签名地址有有效期，当前签名使用内网 endpoint，浏览器仍不可达；需要额外配置外部 endpoint、CORS 和对象生命周期。
4. 继续从 `knowledge_files` 查找 PDF 记录。当前主检索库主要是 Markdown 记录，PDF 分散在其他知识库，无法稳定形成唯一映射。
5. 只返回后端相对路径而不改前端。普通 `<a href>` 不会携带认证头，下载仍会失败。

## 后果

- 卡片 `items` 仍只有四个字段；PDF 地址只存在于 `answer_markdown` 标记中。
- 所有标准号都能生成稳定下载入口，但只有 `powerspec` bucket 中存在同名对象时可成功下载。
- `powerspec` 对象缺失时，下载路由返回 404，不伪造成功。
- 任一已登录用户只要知道安全文件名即可读取对应规范 PDF；该共享原文不按知识库资源权限再收口。
- 标记解析必须按卡片边界进行，不能全局取最后一个地址，否则多卡片会串位。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 标准号稳定映射到 powerspec PDF 文件名 | 空格、全角斜杠或分隔符导致文件名漂移 | `power_spec/pdf_sources.py` | `test_power_spec_pdf_sources.py` | 任一标准号生成的路径不一致时失败 | Passed |
| 每张卡片只携带自己的下载地址 | 多卡片地址串位 | `power_spec/answer.py`、`power_spec_repository.py` | `test_power_spec_formatting.py`、`test_power_spec_repository.py` | 地址顺序错位时失败 | Passed |
| 下载路由固定读取 powerspec bucket | 路径注入或读取错误 bucket | `knowledge_router.py` | `test_power_spec_pdf_route.py` | 非法文件名未拒绝时失败 | Passed |
| 前端按卡片边界解析标记 | 标记数量与卡片错位 | `web/src/utils/powerSpecPdf.js` | `web/test/unit/powerSpecPdf.test.js` | 第二个卡片使用第一个地址时失败 | Passed |
| 外部地址不携带登录凭据 | 用户点击外部 URL 时附带 Bearer Token | `web/src/utils/powerSpecPdf.js`、`MarkdownPreview.vue` | `web/test/unit/powerSpecPdf.test.js`、`markdown_preview_fallback.test.js` | 外部地址被识别为可下载时失败 | Passed |
| 登录用户可下载 powerspec PDF | 直接链接不带认证而 401/404 | `knowledge_router.py`、`knowledge_api.js`、下载工具 | 真实 HTTP/页面验证 | 使用匿名地址或普通 `<a href>` 时失败 | Not run |

执行记录：后端目标用例 `117 passed`；真实规范库 `364` 条记录生成 `7` 个唯一 powerspec PDF 地址；远端 `powerspec` bucket 已回读确认 `DL_T408-2023.pdf`、`DL_T409-2023.pdf`、`DL_T584-2017.pdf`、`GB_T11022-2020.pdf`、`GB_T6451-2023.pdf`、`DL_T393-2021.pdf`、`DL_T596-2021.pdf` 共 7 个 `application/pdf` 对象。真实流式读取 `DL_T393-2021.pdf` 返回 `application/pdf`、`attachment` 文件名和 `%PDF-` 文件头。前 5 个当前来源文件均为 3 页预览版，不是完整标准；完整文件仍需替换同名对象。`DL_T393-2021.pdf` 和 `DL_T596-2021.pdf` 来自原知识库完整 PDF。
