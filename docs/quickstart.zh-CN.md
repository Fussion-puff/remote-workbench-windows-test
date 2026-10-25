# Remote Workbench 快速上手

Remote Workbench 在本地运行 Agent 与 MCP 控制程序，通过用户已建立的 SSH ControlMaster 操作远端。远端不需要 Codex CLI，需要可用的 Python 3.9+。

## 1. 本机安装

```sh
git clone git@github.com:chanfeee/remote-workbench.git
cd remote-workbench
uv sync --locked
```

日常 stable 应从已验证提交构建 wheel 并独立安装；develop 源码用于测试。安装和推广方式见[开发与稳定版部署](development.zh-CN.md)。

## 2. 配置可选服务器

```sh
mkdir -p ~/.config/remote-workbench
cp config.example.yaml ~/.config/remote-workbench/config.yaml
```

填写服务器别名、账号和已有 ControlMaster socket。可配置多个主机，不自动选择其中一个：

```yaml
hosts:
  dev:
    ssh_host: compute.example.edu
    user: alice
    control_path: "~/.ssh/ctrl-%r@%h:%p"
    preamble: ""
  other:
    ssh_host: other.example.edu
    user: alice
    control_path: "~/.ssh/ctrl-%r@%h:%p"
    preamble: ""
```

使用已有 SSH 配置时可以将 ssh_host 填成相应别名；control_path 必须对应实际已有的 master。密码与 OTP 不进入 MCP 参数或普通操作记录。

## 3. Codex 只添加一个 MCP

```toml
[mcp_servers.remote-workbench]
command = "/absolute/local/path/remote-workbench/.venv/bin/remote-workbench-session"
args = [
  "--config", "/absolute/local/path/.config/remote-workbench/config.yaml",
  "--data-dir", "/absolute/local/path/remote-workbench-state",
  "--viewer-port", "8768"
]
enabled = true
```

将所有路径替换为本机绝对路径。日常使用时 command 应改为独立 stable release 的入口或固定启动器；测试 develop 时替换同一个条目的命令，不增加第二个 MCP。Codex 支持全局或受信任项目配置，见[官方说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。保存后重载 MCP。

`workspace_info` 显示当前聊天身份、模式及允许主机，默认不连接 SSH。不要另加 identity-probe、stable、develop 三个常驻条目。

## 4. 需要远端时，人工建立 SSH 连接

纯本地任务可直接使用 Codex 本地工具，无需建立 SSH。需要远端且没有活动 master 时，在本机另一个终端运行：

```sh
uv run remote-workbench establish dev --config ~/.config/remote-workbench/config.yaml
```

按提示完成认证。MCP 只复用已建立连接，连接失效时报告错误，不自行重新认证。

## 5. 同一聊天内使用本地和多台服务器

可以直接告诉 Agent：

> 先在本地运行检查，再在 dev 的 /home/alice/project 构建，随后到 other 的指定工程目录验证。

- 新聊天默认 `mode=local`，不继承其他聊天的服务器。
- `workspace_switch(host, cwd)` 为当前聊天选择远端工程并准备私有副本；切换不会影响其他聊天。
- 本地命令仍使用原生本地工具。选择服务器只影响 Remote Workbench 工具，不会接管整个项目。
- `workspace_use_local()` 清除当前聊天活动绑定；保留原副本、历史和后台任务。
- 原任务始终按原服务器查询和停止；切换不改变任务来源。

例如访问 CERN 的路径，应由本地选择 CERN 目标并复用 CERN 的 master；不要因为当前目标是另一服务器，就在那台服务器里再执行 SSH 去 CERN。选择失败时停止并报告，不在错误的服务器上继续原请求。

首次创建的私有副本保留源工程已有修改，后续交付使用初始基线和 hash guard，详见[可靠性与权限边界](reliability.zh-CN.md)。每条远端命令启动账号默认 shell，不要求 zsh，也不自动沿用上一条命令的 Conda/venv 环境；需要的环境在同一条命令中启用。

## 6. 查看命令、输出与 diff

工具返回加粗的 **Remote Workbench 操作记录** 链接；打开它查看完整命令、输出和改动。实际端口以返回链接为准。后台日志由本地采集器保存，`job_wait` 等待增量，`search_output` 定位错误；完整记录保留在本地，模型按需读取有限片段。

同一 threadId 可在进程重启后恢复绑定；新聊天独立。记录与工程副本是工作流隔离，不是操作系统权限沙箱。合并 MCP 条目不会删除旧日志、旧安装或远端副本。
