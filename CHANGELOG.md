# Changelog

## 0.2.0 — 2026-10-23

This version prepares an independent Remote Workbench distribution from the Onirika 0.1.0 source baseline. See [UPSTREAM.md](UPSTREAM.md) for the exact upstream commit.

- Added local operation records for remote commands, inputs, outputs, and file diffs, with a read-only local viewer.
- Added bounded output previews and paged retrieval to reduce the amount of data returned through MCP.
- Added guarded file edits and checked unified Git patch application on the remote host.
- Added `remote-workbench-workspace`, which binds a local MCP process to one remote host and project directory.
- Renamed the Python namespace, configuration directory, environment variables, and command entry points for the independent project.

The renamed source passed 154 local regression tests, package builds, installation checks, and a remote recheck. Details and the earlier development exercise are documented in [VALIDATION.md](VALIDATION.md).
