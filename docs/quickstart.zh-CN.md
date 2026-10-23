# Remote Workbench 快速上手

Remote Workbench 把 agent 和 MCP 控制进程留在本机，通过 SSH 在远端项目目录中执行工作。远端不需要安装 Codex CLI；需要能通过本机 SSH 连接，并提供 Python 3.9+。

如果要同时维护源码开发版和独立的日常稳定版，请先看[本机开发版与稳定版部署](development.zh-CN.md)。

## 1. 在本机安装

```sh
git clone git@github.com:chanfeee/remote-workbench.git
cd remote-workbench
uv sync --locked
```

如果仓库已经克隆，直接进入该仓库目录并运行 `uv sync --locked`。

## 2. 配置 SSH 主机

```sh
mkdir -p ~/.config/remote-workbench
cp config.example.yaml ~/.config/remote-workbench/config.yaml
```

编辑 `~/.config/remote-workbench/config.yaml`，填写实际 SSH 主机、用户名以及已有 ControlMaster 使用的 socket 路径：

```yaml
default_host: dev
hosts:
  dev:
    ssh_host: compute.example.edu
    user: alice
    port: 22
    control_path: "~/.ssh/ctrl-%r@%h:%p"
    preamble: ""
```

`dev` 是 Remote Workbench 配置中的名称。将示例地址和账号换成自己的值；如果已有 SSH 主连接，`control_path` 要填它实际使用的 socket 路径。

## 3. 绑定一个远端项目目录

每个工作区指定一个主机和远端绝对路径。`cwd` 是 session 工程副本的模板目录；工具不会直接改这个目录。下面把 `project-a` 绑定到 `/home/alice/project-a`：

```sh
mkdir -p ~/.config/remote-workbench/workspaces
cat > ~/.config/remote-workbench/workspaces/project-a.json <<'JSON'
{
  "version": 1,
  "name": "project-a",
  "host": "dev",
  "cwd": "/home/alice/project-a"
}
JSON
```

把示例中的用户名、主机名和路径换成自己的值。`host` 要对应配置文件 `hosts` 下的名称；`cwd` 必须是远端绝对路径。

## 4. 确保 SSH ControlMaster 已连接

如果已有活动的 ControlMaster，可以直接继续。否则，在另一个本机终端运行：

```sh
uv run remote-workbench establish dev --config ~/.config/remote-workbench/config.yaml
```

按 SSH 提示完成认证，并让这个终端保持打开。Codex 使用同一个 ControlMaster；MCP 不会自行处理 SSH 密码或双重认证。

## 5. 把 MCP 加到 Codex 配置

Codex 支持全局 `~/.codex/config.toml` 和受信任项目中的 `.codex/config.toml` 配置 MCP。请参阅[官方 MCP 配置说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。将下面配置中的本机路径换成实际绝对路径：

```toml
[mcp_servers.remote-workbench-project-a]
command = "/absolute/local/path/remote-workbench/.venv/bin/remote-workbench-workspace"
args = [
  "--workspace", "/absolute/local/path/.config/remote-workbench/workspaces/project-a.json",
  "--viewer-port", "8768"
]
env = { REMOTE_WORKBENCH_AUDIT_DIR = "/absolute/local/path/project-a-audit" }
```

Codex 客户端会在本机启动这个 MCP 进程。保存配置后，按客户端文档重新加载 MCP 配置或重新打开客户端。

## 6. 首次连接与日常使用

从 0.2.3 开始，每个 workspace MCP 进程启动时都会生成新的 UUID session。Codex App 当前为每个聊天启动独立的 MCP 进程，因此聊天间的本机操作记录、输出、查看页和远端工程副本彼此隔离。每次工具调用都会提供操作记录链接：正常 JSON 结果包含 `session_id` 和 `operation_log`，MCP 异常则保留错误标记并在文本中附上链接。MCP 指令会要求 agent 在使用工具后的回复中附上该链接，显示为 **Remote Workbench 操作记录**；只为取得这条记录链接，无需额外调用 `workspace_info`。

首次使用时可先发这条消息：

> 先调用 `workspace_info`，核对 `connected`、`ready`、远端目录和账号默认 shell；通过后调用 `git_status`。这个项目的命令、文件读写、搜索、测试和 Git 操作都使用 Remote Workbench MCP 工具。SSH 断开时停止并报告，不要改在本机执行。

这会先确认连接和远端 session 工程副本，再让 agent 从该副本的 Git 状态继续处理。若不需要预先检查连接，agent 也可直接使用所需工具；工具结果已经带有操作记录入口。MCP 不会自动替换客户端内置的本地终端；可将上述指令放入项目的 `AGENTS.md`，固定后续操作使用的工具。

MCP 的本机只读查看页优先使用 <http://127.0.0.1:8768>，可查看实际远端命令、输出和文件改动。工具结果中的 `operation_log` 链接直接进入该 session 的查看页和对应操作；它使用实际端口，因此端口被占用时仍可直接打开。各 session 独立提供查看页，不会复用其他 session 的服务。该页面在 MCP 运行期间可用，人工翻页不增加 MCP 调用；agent 主动读取完整输出时仍会消耗模型上下文。它是单独的查看页，不会生成 Codex 原生终端或文件改动卡片。

首次调用远端工具时，Remote Workbench 会在模板旁创建该 session 的独立工程副本，目录格式为 `source_parent/.remote-workbench-sessions/source_basename/session_uuid/project`。Git 副本包含当时的工作树状态、未提交修改和未被忽略的未跟踪文件；Git 忽略的依赖、环境目录和缓存不会复制。agent 的修改落在副本里，模板目录不变。MCP 进程重启会生成新的 session 和工程副本；已有副本及本机操作记录会保留，不会自动复用之前的环境或任务目录。

session 的隔离边界是 MCP stdio 进程。当前 Codex App 每个聊天启动一个这样的进程，所以聊天之间独立；MCP 进程本身不会读取或推断 Codex chat ID。若未来某个客户端让多个聊天共用一个 stdio 进程，需要客户端提供显式 session 路由才能继续保证聊天级隔离。

每次工具调用都会启动新的远端登录 shell，使用远端账号配置的默认 shell，并读取相应启动配置；不要求使用 zsh。调用之间的 `cd`、环境变量和 Conda/venv 激活状态不会保留，需要在每条相关命令中按该 shell 的语法显式设置。连接不分配伪终端，因此依赖终端或交互式作业控制的启动配置可能与手动 SSH 终端不同。

同一台服务器上的不同项目可分别建立工作区配置，并为每个 MCP 项设置容易辨认的名称。多个实例的查看端口冲突时会自动选择空闲端口，使用工具结果中的操作记录链接即可打开正确的 session。工作区只是远端起始目录，不是文件系统沙箱；远端命令仍受该账号的文件权限约束。本机审计记录可能包含命令和输出，请妥善保管。
