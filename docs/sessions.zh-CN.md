# Session 隔离与操作记录

从 Remote Workbench 0.2.3 开始，workspace MCP 以 stdio 运行时，每次进程启动都会在本机生成新的 UUID `session_id`。session 是 MCP 进程级别的标识，不是由 Remote Workbench 获取或推断的 Codex chat ID。

## 一个 session 有自己的工程副本

workspace manifest 的 `cwd` 指向远端模板工程。第一次调用需要访问远端的工具时，Remote Workbench 会从模板创建一个本 session 专用的工程副本，后续该进程的工程操作都以这个副本为工作目录。模板目录本身不由 workspace 工具修改。

副本目录遵循以下结构：

```text
source_parent/
  source_basename/                         # 只作为模板
  .remote-workbench-sessions/
    source_basename/
      session_uuid/
        project/                            # 该 session 的工程副本
```

例如，模板 `/home/alice/projects/project-a` 对应的副本会位于：

```text
/home/alice/projects/.remote-workbench-sessions/project-a/<session_uuid>/project
```

Git 副本先从仓库 `HEAD` 独立 clone，并以该 commit 建立副本自己的 index 基线，再应用模板当前的文件状态：源中的跟踪文件修改和删除会带入，未被 Git 忽略的未跟踪文件也会带入。源原有的暂存状态不会逐项复制，因此源中已暂存的文件变化在新副本里可能表现为未暂存差异。Git 忽略的依赖、环境目录和缓存不会复制；Git 已跟踪的普通源文件即使位于名称为 `cache` 的目录中仍会保留。Remote Workbench 自有的 runtime 元数据始终排除。新副本不会复用之前 session 的运行环境；需要的依赖应在副本内按项目惯例重新准备。模板可以有意保留未提交修改，这些内容会成为每个新 session 的起点。

`cwd` 也可以指向 Git 仓库中的子目录。创建的副本保留仓库结构，`workspace_info.cwd` 指向副本中的对应子目录。

每次 MCP 进程重新启动都会生成新的 UUID 和远端目录。旧 session 的远端副本、本机审计记录、输出及查看数据会保留，不自动复用或清理。manifest 的 `cwd` 仍指向模板；workspace 工具会把当前 session 工程副本作为默认根目录，agent 的相对路径读写、补丁、测试和 Git 操作都从这个副本开始。

## 本机操作记录

正常 JSON 成功结果和以 JSON 字典返回的工具错误包含 `session_id` 与 `operation_log` Markdown 链接，例如：

```json
{
  "session_id": "<session_uuid>",
  "operation_log": "[**Remote Workbench 操作记录**](http://127.0.0.1:<port>/v/<viewer_id>/?id=<operation_id>)"
}
```

若调用以 MCP `ToolError` 失败，结果保留 `isError` 标记，并在错误文本中附上操作记录链接；这类错误不一定以 JSON 字段返回 `session_id` 或 `operation_log`。`operation_log` 会打开本 session 的本机查看页，并定位到对应操作或该 session 的操作列表。查看页展示实际远端命令、输出和 diff；完整记录留在本机，不需要经 MCP 再传给模型。每个 session 有独立的本机审计/输出目录和 viewer。查看页默认由操作系统分配 loopback 端口，并使用本次服务实例的随机 URL 前缀；显式指定 `--viewer-port` 时如果首选端口被占用，会另选空闲端口，不会复用或终止占用它的 HTML 服务。页面自动刷新会核对服务实例身份。使用工具结果里的完整链接或当前 `viewer_url`；MCP 重启后重新打开新链接，旧链接不保证继续有效。

MCP server instructions 要求 agent 在使用工具后的回复中附上该链接，并保留显示标题 **Remote Workbench 操作记录**。正常 JSON 工具响应提供 session ID 和链接，MCP `ToolError` 文本提供链接；因此无需为了获取操作记录而额外调用 `workspace_info`。这是 MCP 返回数据和指令定义的默认协作方式；服务器不能强制模型一定按要求组织最终回复，也不会把普通 MCP 操作转换为 Codex 原生终端或文件改动卡片。查看器服务随 MCP 进程运行，进程退出后本机记录和输出仍保留。

如果 agent 在回复中省略链接，可从正常 JSON 结果的 `operation_log` 字段复制；对 MCP `ToolError`，可从错误文本中复制 Markdown 链接。通常在本次任务结束回复中附上最后一个相关工具结果的链接即可。

## 隔离边界与 Codex 客户端

隔离保证的单位是 workspace MCP stdio 进程。当前 Codex App 为每个聊天启动独立的 MCP 进程，因此聊天之间有不同的 session、远端目录、本机记录和查看页。Remote Workbench 不会读取 Codex 内部 chat ID。

如果以后某个客户端让多个聊天共享一个 stdio MCP 进程，这些聊天会落在同一个进程级 session。要实现此类客户端的聊天级隔离，客户端必须显式传入并路由 session ID；在没有该路由时，不能把 MCP 进程 session 误认为聊天 session。

每次远端命令仍使用远端账号配置的默认登录 shell，不要求使用 zsh。每次工具调用创建新的 shell，上次调用的 `cd`、环境变量和 Conda/venv 激活状态不会保留。SSH 通道不分配 PTY，因此终端相关启动分支和作业控制行为可能与手动 SSH 终端不同。

查看页默认开启自动刷新，每 2 秒更新，并跟随最新操作。可以手动关闭自动刷新；手动选择历史记录后会继续更新该记录，避免打断阅读。
