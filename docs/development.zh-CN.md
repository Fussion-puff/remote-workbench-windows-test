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

浏览器脚本的行为回归使用本机 Node.js 内置 VM，不需要安装 npm 依赖；本机缺少 Node 时 pytest 会跳过这组检查，CI 会先确认 Node 可用。Node 仅用于开发测试，运行 MCP 和远端执行仍使用原有 Python 环境。

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

## Codex 只注册一个 MCP

日常只保留一个名为 `remote-workbench` 的条目，指向已验证的 stable session 启动器。stable 和 develop 是两个独立安装渠道，不是两个需要同时注册的 MCP。服务器由同一服务按聊天切换，不为每台服务器再注册一个条目。

```toml
[mcp_servers.remote-workbench]
command = "/absolute/path/to/stable/bin/remote-workbench-session"
args = []
enabled = true
```

启动器应固定 stable release 的 Python/入口，并明确传入 stable 的 `--config`、`--data-dir` 和查看器端口。新聊天默认本地；`workspace_info` 仅读本地身份和状态。需要远端时才选择主机及源目录，本地命令始终使用原生工具。

开发测试也使用同一个 MCP 名称：临时将这一个条目的 command 改为 develop session 启动器，测试结束后改回 stable。两个启动器各自使用独立状态和查看器端口，不额外增加 `remote-workbench-develop` 或项目级重复条目。日常新聊天不会因 develop 源码更新而切换版本。

身份诊断通常由 `workspace_info` 提供的 `identity_source`、`client_session_id`、`mode` 和工作区 ID完成。缺少 threadId 时会明确拒绝路由。需要检查原始 metadata 键时，可在隔离的命令行测试中使用 `--identity-probe`；它不作为另一个常驻 MCP 注册。

每次工具结果提供 **Remote Workbench 操作记录** 链接；查看器端口冲突时以返回地址为准。stable/develop 的日志和状态保留在各自目录；合并配置条目不合并不同聊天的数据，也不删除旧副本或 release。

切换渠道或更新单一条目的启动器后，重载客户端 MCP。旧工具列表可能继续缓存，需确认当前条目是 `remote-workbench` 并包含 `workspace_use_local`。回滚时把同一条目的启动器改回保留的旧 release，不新增第二个 MCP。

## 可选的对话级 Session 原型

Develop 的 `0.3.0a1` 增加了 `remote-workbench-session`。它使用请求 metadata 中的 threadId 和本机 SQLite Registry，支持跨进程恢复及主机/模板切换，同时保留每个对话的私有远端工程副本。它使用单独的 `--data-dir`，首次工作区未绑定；不会继承固定 workspace manifest 的默认目标。详见 [Session 原型](session-prototype.zh-CN.md)。

启用开发原型前，需要在实际客户端验证 `_meta.threadId`；SDK 测试客户端主动传入 metadata 的成功结果不能代替这一检查。日常只使用一个固定 stable 入口，develop 验证不自动推广到日常版。

## `0.3.0a6` 起的混合工作入口与推广顺序

本地和多个服务器协作时，使用 `remote-workbench-session`，不再将固定 `--workspace` 入口作为所有聊天的默认远端环境。日常 MCP 可保持可见，但每个新 thread 都从本地模式开始；需要远端时显式选择配置中的主机和绝对源目录。本地命令始终使用本地工具，远端选择只影响 Remote Workbench 工具；`workspace_use_local` 可清除当前绑定并保留原任务和副本。

固定 workspace 入口仍可供明确选择单个远端模板的旧流程使用。其 `workspace_info()` 现在仅查看本地配置；`probe=true` 才检查远端，避免为了查看信息而准备目录。

推广顺序是：develop 通过本机回归与隔离 wheel 验收后提交、推送；再从该固定提交的 wheel 创建新的 stable release、验证安装来源与默认本地行为，最后手动切换日常启动器。stable 不使用 editable 安装，不因 develop 的后续修改自动更新；没有自动定时升级。真实集群测试与本机模拟验收分开记录。

日常入口示例：

```toml
[mcp_servers.remote-workbench]
command = "/absolute/path/to/stable/releases/VERSION-COMMIT/venv/bin/remote-workbench-session"
args = ["--config", "/absolute/path/to/stable/hosts.yaml", "--data-dir", "/absolute/path/to/stable/session-data", "--viewer-port", "8768"]
enabled = true
```

`hosts.yaml` 提供可选主机，不设置自动绑定的模板；`--data-dir` 与 develop 分开。旧固定 workspace 的进程 UUID 没有可信 threadId 映射，因此旧副本和记录保留，但不会静默转成新聊天的默认远端绑定。缺少 threadId 时新入口拒绝远端路由，不能回退到固定默认服务器。更换启动器后重载 App 的 MCP，再在新聊天验证 `workspace_info.mode=local`。
