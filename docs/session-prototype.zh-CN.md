# 按 Codex 对话路由的 Session 原型

`remote-workbench-session` 从 `0.3.0a1` 起作为 develop 的可选入口，当前版本为 `0.3.0a2`。它复用原有 SSH、文件、Git 和后台任务工具，保留 `remote-workbench-workspace` 与 `remote-workbench-ssh`。安装到独立 wheel 环境的日常版不会随开发源码改变。

## 身份和首次绑定

新入口仅从每次 MCP 请求的 `_meta.threadId` 识别对话。`sessionId` 仅作诊断信息；进程 ID、环境变量、工具参数和全局默认主机都不能代替 threadId。获取不到身份时返回 MCP 错误，在执行任何远端命令前停止。

首次 `workspace_info()` 创建未绑定的工作区，返回允许使用的主机名称。用户选定服务器和工程模板后，调用：

```text
workspace_switch(host="myserver", cwd="/absolute/path/to/project")
```

这里的 cwd 是模板目录。工具校验主机与远端目录，创建本对话的独立工程副本后才提交绑定。后续文件、命令和 Git 工具使用副本作为默认工作目录，模型无需传递 thread ID 或 workspace ID。

同一 threadId 在 MCP 重启后恢复原有副本和修改。新 threadId 创建新的未绑定工作区；分叉对话不会自动继承父对话的代码，需要显式选择模板。关闭聊天不会自动删除工作区、记录或远端目录。

## 切换与执行

工作区管理与观察工具：

| 工具 | 行为 |
| --- | --- |
| `workspace_info()` | 读取当前绑定、revision、身份来源及允许的主机，不执行 SSH |
| `workspace_switch(host, cwd, expected_revision=None)` | 验证目标、创建或恢复私有副本，然后原子切换 |
| `workspace_history(limit=20)` | 读取本对话的目标切换历史，最新在前 |
| `workspace_snapshot()` | 一次读取当前 Git 概况、本目标的历史测试证据及通信计量 |

切换失败时，原绑定保持不变。`expected_revision` 可拒绝基于过时状态的切换。切回相同主机和规范化模板路径会恢复已保存的副本，不重新覆盖代码；重复选择当前目标是幂等操作。已保存目录消失或发生重定位时会报错，不会静默从模板重建。

每次执行使用不可变快照。每个工作区的本机文件锁会串行化前台调用和目标切换，同一数据目录下的不同 MCP 进程也遵守该锁；等待超过 30 秒会提示当前存在前台操作。不同工作区可以并行。

后台任务另存提交时的目标。切换后，`job_status(job_id)` 仍访问原服务器及原状态目录。job ID 在同一工作区内不能重新绑定到其他目标，不同工作区可以使用同名 job ID。SSH 提交结果不明时也保留原始目标，避免重试时误投递至另一台服务器。

从 `0.3.0a2` 起，任务还记录实际节点与进程启动标识。Linux 使用 boot ID 和 `/proc` 启动时钟标识，其他 Unix 使用 `ps` 启动时间作尽力检查。节点变化、重启、PID 身份不符或旧任务缺少身份记录时，返回 `running=null, execution_state="unknown"`，而非根据同号 PID 猜测。若已有 supervisor 写出的退出码，则即使从其他共享文件系统节点读取，也可以报告该次任务完成。工具不自动切换到新节点或代查调度器。

执行通道显式禁用 PTY，并在正常 mux 复用失败时使用不会连接网络的代理命令阻断 OpenSSH 的普通连接回退。Master 在检查后失效也不会触发新的 SSH 认证；用户仍通过普通终端完成认证。该设置不改变远端账号默认 shell 的选择及其启动文件。

每条命令仍启动账号默认的登录 shell，不要求 zsh，也不自动延续上条命令的环境。需要的 Conda、venv 或模块环境应在同一命令中显式启用。这里不运行远端 Codex CLI，不提供操作系统级文件隔离。

## 副本与持久化

普通目录的副本路径为：

```text
source_parent/.remote-workbench-sessions/source_basename/target_uuid/project
```

Git 模板按仓库根目录选择副本的外部父目录，因此绑定仓库子目录也不会把副本建进源仓库。当前 cwd 保留其在仓库中的相对位置。每个对话和每个目标的 target UUID 均独立。复制规则继续使用原有快照实现，包含当前未提交代码及未被忽略的文件，详见 [session 复制规则](sessions.zh-CN.md)。Git 模板需要已有 HEAD；尚无提交的仓库会被拒绝。失败或并发冲突产生的未绑定副本保留，不自动清理。

默认本机数据目录是 `$XDG_DATA_HOME/remote-workbench`，未设置 XDG 时为 `~/.local/share/remote-workbench`。可用 `REMOTE_WORKBENCH_DATA_DIR` 或 `--data-dir` 指定独立目录，开发联调应与日常数据分开。

```text
data-dir/
  workspaces.sqlite3       # 对话身份、当前目标、历史和任务来源
  locks/                   # 跨进程前台操作锁
  audit/<workspace_uuid>/  # 本对话的完整记录、输出和 diff
```

SQLite 使用 WAL 和事务，绑定更新检查 revision。审计记录附带 thread_id、workspace_id、target_id 和调用开始时的 revision；历史记录不会因当前目标变化而改变。元数据文件更新使用记录锁和唯一临时文件，防止多个写入者丢失字段或子步骤。

## 记录页与通信量

每个返回结果继续包含加粗的 **Remote Workbench 操作记录** 链接。一个查看器通过 `/w/<workspace_uuid>/` 分别展示各工作区记录；未选择工作区的请求不会落入全局默认记录。通过一个工作区的 API 路径无法读取另一工作区的 operation ID。

MCP 结果为一份紧凑 JSON 文本，不重复发送 structuredContent。命令预览默认 2 KiB，文件、补丁和 Git diff 预览默认 4 KiB；完整内容仍保存在本机。通常无需把 SSH 包装代码读回模型上下文。记录页仍是本机 HTML，不会因远端 cwd 改变而自动成为 Codex 的原生终端或原生 Git diff。

`workspace_snapshot` 中的 Git 状态是当次远端观察，包含时间、分支、HEAD 和变更文件数；超出统计界限时保留 `status_complete=false`，不把截断数据当成完整结果。它不会在普通工具调用时自动增加 Git 探测。

`last_test` 按目标分别保存，来源是被识别为测试命令的退出结果；超时或传输状态不明时为 unknown。它同时返回 `evidence="command_exit_only", applies_to_current_code=null`：没有做代码指纹匹配，因此历史成功不能证明当前代码仍通过。切换到另一目标不会带过去旧目标的测试结果。

通信计量保存在本机 SQLite，不增加额外 MCP 调用，也不存参数或输出正文。它记录完成的响应数量、已验证参数 JSON 的 UTF-8 字节数、返回文本字节数、失败结果和耗时。重复读取仅统计同一工作区内、同一保存输出页面的相同内容；增长中的日志返回新字节不算重复。Snapshot 的计量截至本次快照响应之前，不包含 JSON-RPC 外壳、工具 schema 或实际 tokenizer 成本，不等同于任务成功率。计量故障不会把已经执行的写入误报为失败。

本原型没有新增项目/聊天管理 UI，也没有实现本地与远端代码的双向同步。完整审计元数据的 SQLite 索引、全局 Session/Host 筛选 UI、Worker 内容缓存和日志配额属于后续工作。

## 启动 develop 原型

```sh
uv sync --locked --extra dev
uv run --locked remote-workbench-session \
  --config /absolute/path/to/develop/hosts.yaml \
  --data-dir /absolute/path/to/develop/session-data \
  --viewer-port 8770
```

MCP 客户端配置示例，路径需替换为本机展开后的绝对路径：

```toml
[mcp_servers.remote-workbench-session-develop]
enabled = false
command = "/absolute/path/to/develop/.venv/bin/remote-workbench-session"
args = ["--config", "/absolute/path/to/develop/hosts.yaml", "--data-dir", "/absolute/path/to/develop/session-data", "--viewer-port", "8770"]
```

原型不改变客户端的进程管理。客户端可以让不同聊天启动不同 stdio 进程，也可以向同一个进程发送多个 threadId；两种情况均按同一个 SQLite Registry 路由。仅使用 stdio 配置不能保证 App 自动共享一个 MCP 进程。本阶段没有新增常驻 HTTP MCP 服务。

## 客户端联调边界

[Codex MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)说明了 MCP 的连接方式。[OpenAI Codex 源码](https://github.com/openai/codex/blob/main/codex-rs/core/src/mcp_tool_call.rs)目前存在 `with_mcp_tool_call_ids_meta()` 注入 threadId/sessionId 的实现，但这不构成每个已安装 App 版本都会转发这些字段的承诺。

自动测试通过真实 Python MCP 客户端向 stdio 服务传入 metadata，验证 SDK 注入、工具 schema、进程重启、双对话隔离、切换和任务来源；这与 App 亲自发出 metadata 是两项不同的验证。

App 联调时，仅启用 develop 原型，在两个聊天中分别调用 `workspace_info()`：应看到不同 workspace_id，同一聊天重载 MCP 后应恢复同一个 workspace_id。如果 App 没有转发 threadId，应收到明确的身份错误。此时不要用 sessionId、PID、用户手填 ID 或默认目录绕过。未完成真实 App 的这一检查前，不替换日常版。

也可先用只读诊断模式单独联调，不启用任何远端工具：

```sh
remote-workbench-session --identity-probe --data-dir /absolute/path/to/develop/identity-data --viewer-port 8771
```

此模式只暴露无参数的 `session_identity_probe`。它不读取主机配置、不建立 SSH、不创建或选择 Workspace，仅返回实际 MCP 请求中的身份是否可用及字段名，并保存本机记录。其他 metadata 的值不会写入记录。App 设置中出现服务器条目、或本机端口已启动，都不能代替真实的 tool call 验证；修改配置后，已开始回合的工具清单也可能仍需在后续回合刷新。
