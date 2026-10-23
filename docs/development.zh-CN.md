# 本机开发版与稳定版

建议把源码开发环境和 Codex 日常使用的稳定版分开放。下面用 `~/opt/remote-workbench` 举例；它只是本机目录示例。Codex MCP 配置里的 `command`、manifest 和审计目录都要填写展开后的绝对路径。

```text
~/opt/remote-workbench/
  develop/                         # develop 分支源码和开发用 .venv
  stable/
    config/hosts.yaml              # 稳定版 SSH 主机配置
    workspace.json                 # 稳定版远端项目绑定
    audit/                         # 稳定版本机审计记录
    releases/<version>-<commit>/   # 每个稳定版本单独保留
      dist/<wheel>.whl
      runtime-requirements.txt
      .venv/
```

## 开发版

在源码 checkout 中使用 develop 分支和它自己的虚拟环境。`uv sync` 会把当前 checkout 安装到该环境，适合边改边测：

```sh
git clone https://github.com/chanfeee/remote-workbench.git "$HOME/opt/remote-workbench/develop"
cd "$HOME/opt/remote-workbench/develop"
git switch develop
uv sync --locked --extra dev
```

开发版使用独立的主机配置、workspace manifest 和审计根目录。manifest 的 `config` 字段要显式指向开发版配置；`cwd` 指向远端模板目录，工具首次调用时会在模板旁创建该进程 session 专用副本，测试不会直接修改模板。稳定版和开发版可分别绑定日常与测试模板。MCP 开发项使用 `--viewer-port 8769`，并设 `enabled = false`，需要测试时再临时启用。每次启动进程都会得到新的 session UUID，进程内的本机审计、输出和查看页各自独立。

## 稳定版安装

稳定版从通过测试的源码 commit 构建 wheel，再安装到独立的新环境。每次发布都创建新的版本目录，例如 `~/opt/remote-workbench/stable/releases/0.2.3-a1b2c3d`；不要覆盖旧目录。

在 develop checkout 内确认改动已提交、工作区干净，完成所需测试后，设置新版本目录并构建：

```sh
RELEASE="$HOME/opt/remote-workbench/stable/releases/0.2.3-a1b2c3d"
mkdir -p "$RELEASE/dist"
git rev-parse HEAD
uv export --locked --no-dev --no-emit-project --no-hashes --output-file "$RELEASE/runtime-requirements.txt"
uv build --wheel --out-dir "$RELEASE/dist"
```

将下面的 wheel 文件名换成 `dist` 中实际生成的文件名，然后先创建 venv，再从 wheel 普通安装。这里不要对 stable 目录运行 `uv sync`，也不要用 editable 安装：

```sh
uv venv --python 3.13 "$RELEASE/.venv"
uv pip install --python "$RELEASE/.venv/bin/python" \
  --constraints "$RELEASE/runtime-requirements.txt" \
  "$RELEASE/dist/remote_workbench-0.2.3-py3-none-any.whl"
```

记录 `git rev-parse HEAD` 的完整 commit、wheel 的 SHA-256、版本号和测试结果。安装后检查包版本及导入位置，确认它位于该 release 的 `.venv` 下：

```sh
"$RELEASE/.venv/bin/python" -I -c 'import importlib.metadata, remote_workbench; print(importlib.metadata.version("remote-workbench")); print(remote_workbench.__file__)'
shasum -a 256 "$RELEASE/dist/remote_workbench-0.2.3-py3-none-any.whl"
```

## Codex MCP 配置与切换

稳定版 workspace manifest 要显式填写稳定版 `config`，避免继承 `REMOTE_WORKBENCH_CONFIG` 或读到默认配置；其 `cwd` 指向日常工程模板。首次远端工具调用会创建 session 专用副本，日常工程模板不被工具修改。稳定版的 MCP 项使用独立审计根目录和 `--viewer-port 8768`。示意如下，所有本机路径都要替换为真实绝对路径：

```toml
[mcp_servers.remote-workbench-stable]
command = "/absolute/path/to/opt/remote-workbench/stable/releases/0.2.3-a1b2c3d/.venv/bin/remote-workbench-workspace"
args = ["--workspace", "/absolute/path/to/opt/remote-workbench/stable/workspace.json", "--viewer-port", "8768"]
env = { REMOTE_WORKBENCH_AUDIT_DIR = "/absolute/path/to/opt/remote-workbench/stable/audit" }

[mcp_servers.remote-workbench-develop]
enabled = false
command = "/absolute/path/to/opt/remote-workbench/develop/.venv/bin/remote-workbench-workspace"
args = ["--workspace", "/absolute/path/to/opt/remote-workbench/develop/workspace.json", "--viewer-port", "8769"]
env = { REMOTE_WORKBENCH_AUDIT_DIR = "/absolute/path/to/opt/remote-workbench/develop/audit" }
```

开发版 manifest 的 `config` 和 `cwd` 应分别指向开发配置及远端测试模板。稳定版 manifest 的 `config` 和 `cwd` 则固定到稳定配置及日常工程模板。不要让开发与稳定 workspace 共用 audit 根目录；两个 viewer 使用不同的首选端口。同一个 MCP 被多个聊天同时启动时，每个 stdio 进程生成独立 session、审计记录、输出、查看页及远端工程副本。若发生端口冲突，实际查看地址会在工具返回的 `operation_log` Markdown 链接中给出；也可查看 `workspace_info.viewer_url`。链接的标题固定为 **Remote Workbench 操作记录**，MCP 指令要求 agent 在工具调用后的回复附上它，不需要再额外调用 `workspace_info` 来获取链接。

远端工程副本放在 `source_parent/.remote-workbench-sessions/source_basename/session_uuid/project`。从 Git 模板创建副本时，会带上模板当前的已跟踪工作树（包含未提交修改）和未被忽略的未跟踪文件；忽略的依赖、环境目录和缓存不复制。模板只作为每个新 session 的起点，不会由工具直接修改。进程重启会创建全新 UUID、日志及远端副本；旧目录和本机记录保留，任务目录及环境不会自动复用。该保证是 MCP 进程级的；固定 workspace 模式不获得或推断 Codex chat ID。若客户端把多聊天复用在一个进程内，必须提供显式 session 路由才有聊天级隔离。

发布时，先在新 release 环境核对版本和导入路径，再用新版本运行 `workspace_info`，确认远端主机、目录和默认 shell；随后确认命令、输出和 diff 出现在稳定版审计记录与本机 viewer 中。通过后，把 Codex 稳定 MCP 项的 `command` 明确改为新 release 的启动器路径，重新加载 MCP 配置，再做一次连接检查。该切换不会自动从 Git 拉取或更新代码。

每个旧 release 的 wheel、venv 和版本记录都保留。若新版本有问题，把稳定 MCP 的 `command` 改回旧 release 路径并重新加载即可回滚；不要删除仍可能用于回滚的版本目录。开发项默认禁用，只有需要检验新改动时才启用它。

## 可选的对话级 Session 原型

Develop 的 `0.3.0a1` 增加了 `remote-workbench-session`。它使用请求 metadata 中的 threadId 和本机 SQLite Registry，支持跨进程恢复及主机/模板切换，同时保留每个对话的私有远端工程副本。它使用单独的 `--data-dir`，首次工作区未绑定；不会继承固定 workspace manifest 的默认目标。详见 [Session 原型](session-prototype.zh-CN.md)。

启用开发原型前，需要在实际客户端验证 `_meta.threadId`；SDK 测试客户端主动传入 metadata 的成功结果不能代替这一检查。当前日常稳定版无需切换或升级。
