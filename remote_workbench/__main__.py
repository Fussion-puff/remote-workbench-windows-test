"""Entry point for remote-workbench-ssh MCP server."""

import argparse

from remote_workbench.server import mcp


def main():
    parser = argparse.ArgumentParser(description="Remote Workbench MCP Server")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config YAML (default: ~/.config/remote-workbench/config.yaml)",
    )
    args = parser.parse_args()

    if args.config:
        import os
        os.environ["REMOTE_WORKBENCH_CONFIG"] = args.config

    mcp.run()


if __name__ == "__main__":
    main()
