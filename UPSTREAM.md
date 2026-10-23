# Upstream provenance

Remote Workbench is an independently maintained derivative of [Onirika](https://github.com/xinyue-uoft/Onirika). Its source baseline is Onirika version 0.1.0 at upstream commit `1e297c353891eb9e1e9881ffa9db5cb08a877c55`.

The Remote Workbench changes in this source tree were made on 2026-10-23 and 2026-10-24. They include local command and output auditing, bounded MCP responses, remote file and patch operations, a local audit viewer, an immutable remote-workspace MCP entry point, shared remote account-shell selection for commands and jobs, and per-session project copies with isolated records and operation links. The project has been renamed and uses the `remote_workbench` Python package and `remote-workbench` command namespace. It is not an official Onirika release and is not represented as endorsed by the upstream maintainers.

The 0.3.0a1 development prototype adds opt-in request-metadata thread routing, a local SQLite workspace registry, immutable execution snapshots, private target switching and restoration, persistent job origins, and workspace-scoped audit routes. Existing fixed-workspace and explicit-host entry points remain available.

The upstream README is preserved in [README.upstream.md](README.upstream.md). The [LICENSE](LICENSE) is preserved. This derivative is distributed under GNU AGPL version 3; retain upstream copyright, license, and warranty notices when redistributing it. This provenance notice identifies the source lineage and does not claim authorship of upstream work.
