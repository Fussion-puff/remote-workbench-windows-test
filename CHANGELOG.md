# Changelog

## 0.3.0a7 (develop)

- Assign each local audit viewer an OS-selected loopback port by default and a random per-process URL prefix, so other project pages are not reused or overwritten.
- Keep operation links pointed at the actual viewer instance; reject stale URLs and prevent auto-refresh from rendering responses from another instance.
- Preserve per-session audit data and workspace routing under the instance-prefixed viewer URL.

## 0.3.0a5 (develop) — 2026-10-24

- Supervise foreground commands and detached jobs with owned process groups, remote deadlines, foreground heartbeats, and TERM-to-KILL cleanup.
- Add `stop_job` and the independent `remote-workbench-cleanup` entry point, with node and process-identity checks and original-target routing.
- Require explicit user confirmation of substantial compute plans through the agent workflow; ordinary operations remain direct, without cgroup requirements or an account-wide process cap.
- Select public source-distribution files explicitly so local configuration, virtual environments, audit records and runtime data stay out of packages.
- Run CI for pushes to develop as well as main. This development release does not update the independent stable installation.

## 0.2.1 — 2026-10-23

- Use the remote account's configured login shell for commands and detached jobs, with no zsh fallback or fixed-zsh live-test requirement.
- Launch login shells using the SSH-style login name and preserve native startup files, byte-safe stdin, output framing, project directory binding, and shell-specific tracing.
- Report the selected shell in `workspace_info` and document the difference between a tool command channel and a manual SSH terminal.
- Run generated file/search/Git operations with argument arrays, independently of the login shell's quoting and pipeline syntax; preserve real search exit codes while limiting returned lines.

## 0.2.0 — 2026-10-23

This version prepares an independent Remote Workbench distribution from the Onirika 0.1.0 source baseline. See [UPSTREAM.md](UPSTREAM.md) for the exact upstream commit.

- Added local operation records for remote commands, inputs, outputs, and file diffs, with a read-only local viewer.
- Added bounded output previews and paged retrieval to reduce the amount of data returned through MCP.
- Added guarded file edits and checked unified Git patch application on the remote host.
- Added `remote-workbench-workspace`, which binds a local MCP process to one remote host and project directory.
- Renamed the Python namespace, configuration directory, environment variables, and command entry points for the independent project.

The renamed source passed 154 local regression tests, package builds, installation checks, and a remote recheck. Details and the earlier development exercise are documented in [VALIDATION.md](VALIDATION.md).
