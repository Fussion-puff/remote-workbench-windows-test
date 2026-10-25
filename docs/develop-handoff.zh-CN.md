# Develop 交接记录：操作记录页的会话隔离

更新日期：2026-10-25  
渠道：develop，源码版本 `0.3.0a7`  
状态：代码、文档及本机回归已完成（全套 410 项通过）。本机安装渠道和固定提交以 `.local/stable/releases/` 下的发布记录为准。

## 交付内容

每个本机查看页默认由操作系统分配 loopback 端口。启动器若指定首选端口，遇到 `EADDRINUSE` 会改用系统分配的空闲端口；Remote Workbench 不会接入、停止或覆盖已经占用端口的服务。

查看页 URL 带有该进程随机生成的 `/v/<instance-id>` 路径。服务端只接受对应路径；HTML 自动刷新还会检查响应中的实例标识，再解析和显示内容。若浏览器保留旧页，而端口随后被其他 Remote Workbench 实例复用，旧路径会收到 404，自动刷新也不会显示新实例的数据。Session 查看页继续在实例路径下使用 `/w/<workspace-id>/`，审计根目录仍按聊天的 workspace 隔离。

MCP 的 `operation_log` 和 `viewer_url` 返回实际端口与完整实例路径。查看页只随对应 MCP 进程存活；操作记录仍保存在本地。MCP 重载或重启后，用户应打开新工具结果提供的链接。

主要实现位置：

- `remote_workbench/workspace.py`：loopback 监听、端口回退、URL 实例 ID 和默认端口。
- `remote_workbench/session_server.py`：Session/identity probe 使用当前实例的真实 URL。
- `remote_workbench/audit_web.py`：实例路径路由、响应标头和浏览器刷新身份检查。
- `tests/test_viewer_instances.py`、`tests/test_workspace_efficiency.py`、`tests/test_audit_viewer_refresh.py`：路由、端口冲突、页面复用及刷新回归。

## 验证记录

本机针对性回归结果：`40 passed in 6.34s`。全套回归：`410 passed in 92.89s`。覆盖 viewer 生命周期、真实 loopback HTTP、实例路径、端口冲突、旧链接端口复用和浏览器自动刷新。测试仅启动本机服务；没有连接 IHEP、CERN 或其他 SSH 主机。

实际 develop 启动器通过 MCP stdio 与 loopback HTTP 验收：两个 MCP 进程、三个本地聊天，工具返回实例路径，记录目录分别隔离，旧实例与跨聊天记录请求均被拒绝。SSH 调用次数为零。

本记录不表示 GitHub CI、实际 Codex App 页面验收或远端验收已完成。若后续再改 viewer 路由或启动器，更新本页中的验证时间和结果，避免沿用旧数字。

## 推广与接手步骤

本地 develop 启动器位于 `.local/develop/bin/remote-workbench-session`，指向 checkout 的 `.venv`，默认传入 `--viewer-port 0`。此处 `.local` 配置和数据不进入 Git。

全局只保留一个 `remote-workbench` MCP 入口。stable 是独立 wheel 安装，develop 启动器使用 checkout 的 `.venv`。切换渠道时替换同一条目的启动器并重载 MCP，再分别打开两个聊天的新操作记录链接；不要添加 develop 的第二个全局 MCP。更新安装不会替换已经运行中的 MCP 进程或旧页面。

后续推广前应确认 develop 构建来自已检查的干净提交，完成全套本机回归及 stdio 启动验收，检查实例 URL 和操作记录链接，再独立构建/安装 stable wheel。stable 推广仍采用手动固定版本；不得让 stable 自动跟随 develop。
