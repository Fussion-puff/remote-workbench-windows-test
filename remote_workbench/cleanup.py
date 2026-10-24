"""Control-only cleanup CLI, independent of the MCP/agent session."""
import argparse
import asyncio
import json

from remote_workbench.config import load_config
from remote_workbench.ssh import SSHExecutor


def main():
    parser = argparse.ArgumentParser(description="Stop a supervised Remote Workbench task through an existing SSH master")
    parser.add_argument("--config", help="Host configuration YAML")
    parser.add_argument("--host", help="Configured SSH host alias")
    parser.add_argument("--state-dir", required=True, help="Exact remote state directory from the operation record")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--job-id", help="Detached job ID")
    group.add_argument("--task-id", help="Foreground task ID from remote_task.task_id")
    args = parser.parse_args()
    config = load_config(args.config).get_host(args.host)
    executor = SSHExecutor(config, remote_state_dir=args.state_dir)
    value = asyncio.run(executor.cancel_task(args.job_id or args.task_id, kind="jobs" if args.job_id else "tasks"))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    raise SystemExit(0 if value.get("execution_state") == "finished" else 1)


if __name__ == "__main__":
    main()
