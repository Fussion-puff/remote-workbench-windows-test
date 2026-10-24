# 计算审核与远端任务生命周期（develop）

这轮修改仅在 develop，未替换独立安装的 stable。普通文件操作、搜索、Git 和小规模测试直接执行；不要求 cgroup、不修改账户进程上限，也不统一压低线程或编译并发。

## 大量计算先确认

启动高并发编译、MPI、多进程计算、批量作业提交，或者测试运行时进程包装器前，Agent 应先展示完整命令、服务器与目录、预计并发规模、所用环境和可能的风险，等待用户明确确认。泛泛的“继续测试”不能替代对具体计算方案的确认。

工具会对部分常见命令返回 `compute_review_required`，此次不会执行。例如 `make -j32`、自动并行的 `ninja`、MPI 启动器和调度器提交命令。默认并行审核阈值为 8，可通过 `compute_review_threshold` 调整；它不是资源硬上限。通过用户确认后，Agent 将以下信息随 `exec_command` / `start_job` 的 `compute_review` 参数提交，完整命令仍由原来的 `command` 参数传入：

```json
{
  "concurrency": 16,
  "environment": "已分配的计算节点；在同一命令中激活所需环境",
  "confirmation": "用户已明确确认上述命令、并发规模和环境"
}
```

操作记录注明确认内容由 Agent 转述。服务器不能独立读取聊天验证确认；命令行识别也不能覆盖脚本内部的所有行为。因此 Agent 仍需检查可能大量启动进程的脚本。这是工作流约束，不是操作系统沙箱，也不保证阻止递归创建进程。

## 取消、断连与超时

前台命令和后台作业使用同一份独立监督器。工作负载在新进程组中启动，监督器留在组外；记录节点、监督器 PID、工作负载 PID、PGID 和进程出生标记。监督器直接调用系统进程 API，不注入 `LD_PRELOAD`，不重写 `subprocess` 或其他程序的启动接口。

前台命令有控制端心跳；取消会关闭该控制通道，控制端消失或心跳过期时，监督器清理自己的工作负载进程组。后台作业不会因为启动它的工具调用完成而取消，但始终有自己的期限：默认 `job_timeout: 3600` 秒，调用者可以为作业设置其他正数 `timeout`。普通命令仍沿用原有的 `command_timeout`。

监督器在超时、取消及主命令结束后处理残留同组进程：发送 TERM，再升级到 KILL。`terminate_grace_seconds` 控制等待时间。清理不使用整个 UID 的 kill，也不关闭共享 SSH master。

`cleanup_state: process_group_signalled` 只说明对该进程组完成了发信号操作，`tree_confirmed: false` 保留实际边界。主动调用 `setsid()` 或更改进程组的后代可能脱离；监督器被强制杀死、节点故障、信号失败时也不能承诺完成清理。进程组的组织与 `setsid()` 行为见 [Linux 手册](https://man7.org/linux/man-pages/man2/setsid.2.html)。

## 独立清理入口

在原工作区调用 `stop_job(job_id=...)`。对话级 Session 模式会将停止请求发送到该作业的原始目标，即使当前工作区已经切换。

若 MCP / Agent 已退出，也可以从本地终端使用清理入口：

```sh
remote-workbench-cleanup --config /absolute/local/hosts.yaml --host myserver \
  --state-dir /absolute/remote/project/.remote-workbench-state --job-id JOB_ID
```

前台任务则使用操作记录中 `remote_task.task_id` 对应的 `--task-id`。`--state-dir` 必须使用该任务的原状态目录。清理入口只通过已有 SSH master 向监督器发请求，跳过工具自己的交互式 shell、preamble 和测试环境；SSH 服务端启动命令所需的外层账户 shell 仍由 SSH 服务端决定。

清理会检查节点、boot ID（Linux）及进程出生标记；不匹配或旧版任务没有足够身份信息时返回未知，不对陈旧 PID / PGID 盲目发送信号。`execution_enabled: false` 可手动暂停普通执行，清理入口仍可调用。

## 本轮验证范围

仅使用本地临时目录中的固定少量子进程验证，不在共享集群登录节点复现递归或资源耗尽。当前源码审查确认旧实现取消/超时只终止本地 SSH 子进程，且后台任务缺少停止入口。源码和本轮核查的 develop 审计记录中未找到事故所述 preload 注入；具体递归 wrapper 的来源仍未定位，不能把生命周期修复表述为已经定位并修好了递归根因。
