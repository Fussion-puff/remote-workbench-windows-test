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

每个工作区指定一个主机和远端绝对路径。下面把 `project-a` 绑定到 `/home/alice/project-a`：

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

首次使用时可先发这条消息：

> 先调用 `workspace_info`，核对 `connected`、`ready`、远端目录和账号默认 shell；通过后调用 `git_status`。这个项目的命令、文件读写、搜索、测试和 Git 操作都使用 Remote Workbench MCP 工具。SSH 断开时停止并报告，不要改在本机执行。

这会先确认连接和目标路径，再让 agent 从当前 Git 状态继续处理。MCP 不会自动替换客户端内置的本地终端；可将上述指令放入项目的 `AGENTS.md`，固定后续操作使用的工具。

MCP 的本机只读查看页优先使用 <http://127.0.0.1:8768>，可查看实际远端命令、输出和文件改动。如果端口已被其他聊天或程序占用，会自动选择空闲端口；请以 `workspace_info` 返回的 `viewer_url` 为准。各实例独立提供查看页，不会复用占用端口的服务。该页面在 MCP 运行期间可用，人工翻页不增加 MCP 调用；agent 主动读取完整输出时仍会消耗模型上下文。它是单独的查看页，不会生成 Codex 原生终端或文件改动卡片。

每次工具调用都会启动新的远端登录 shell，使用远端账号配置的默认 shell，并读取相应启动配置；不要求使用 zsh。调用之间的 `cd`、环境变量和 Conda/venv 激活状态不会保留，需要在每条相关命令中按该 shell 的语法显式设置。连接不分配伪终端，因此依赖终端或交互式作业控制的启动配置可能与手动 SSH 终端不同。

同一台服务器上的不同项目可分别建立工作区配置，并为每个 MCP 实例使用不同名称和查看端口。工作区只是默认起始目录，不是远端文件系统沙箱；远端命令仍受该账号的文件权限约束。本机审计记录可能包含命令和输出，请妥善保管。
