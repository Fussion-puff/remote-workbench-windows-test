# 在聊天中查看远端图片

`0.3.0a8` 新增 `fetch_image(path, page=1)`。在当前聊天已经明确选择的远端工作区中读取图片，保存到该聊天的本机审计缓存，并返回 `local_path`、`preview_path` 和 `image_markdown`。Agent 直接把 `image_markdown` 放进回复，图片就在聊天中展示；不需要先打开 HTML。HTML 查看器没有增加图片预览功能。

可以直接说：“把远端 `plots/fit.png` 显示在聊天里”，或“看一下 `plots/result.pdf` 第 2 页”。本地项目的图片仍用原生本地工具，不需要经过此 MCP。

## 格式与使用

- PNG、JPG：保留原图，聊天直接使用本地副本；也支持 GIF、WebP。
- PDF：默认预览第 1 页，`page=2` 选择第 2 页；只在本机将所选页转为 PNG，保留原 PDF。
- SVG：保留原文件，在本机生成 PNG 用于聊天展示。预览只支持自包含的静态图，不加载外部图片、样式或脚本。

拉取只返回一个紧凑的 JSON 文本结果，不返回 MCP 图片块或 base64。原始图片和预览留在本机，各次拉取生成独立副本，聊天之间分开存放。若 Agent 需要分析图片，可以再用原生图片查看工具读取 `preview_path`；该分析仍会使用模型的图片上下文。

远端只需现有的 SSH 连接与普通 Python，不需要安装渲染库。读取沿用主机配置的 `max_file_size`（默认 10 MiB），拒绝非普通文件及读取期间改变的文件。预览失败时仍返回成功拉取的原文件路径和 `preview_error`，不会返回一个失效的聊天图片引用。PDF/SVG 的 PNG 预览最长边限制为 2048 像素；需要原分辨率或矢量版本时打开 `local_path`。

## 安装本地渲染支持

PNG/JPG 原图展示不依赖可选渲染包。PDF/SVG 预览需在本地安装 `images` extra：

```sh
uv sync --locked --extra dev --extra images
```

使用 wheel 时安装 `remote-workbench[images]`。更新安装后重载当前唯一的 `remote-workbench` MCP，工具列表中应出现 `fetch_image`。stable 采用固定 wheel 独立安装，需按既有流程验证后推广。

本地 PDF 渲染使用 [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html)，SVG 渲染使用 [resvg-py](https://resvg-py.readthedocs.io/en/latest/api.html)。预览是方便查看的副本，原文件用于保留完整内容。
