# Validation status

## Current renamed copy — 2026-10-23

- Local regression suite: 154 tests passed in the independent `remote-workbench` checkout environment (16.17 seconds).
- MCP stdio initialization handshake passed.
- Wheel and source distribution builds passed.
- Independent environment installation with the locked dependencies passed. The renamed package imports from the independent checkout and reports version 0.2.0.
- The built wheel was installed in a separate empty environment with dependencies constrained by the lock file. Ten smoke checks passed, including all six command help entries, isolated package imports and bundled resources, and MCP stdio handshakes exposing 25 multi-host tools or 16 bound-workspace tools. The upstream `onirika` package was absent from that environment.
- Remote recheck through the renamed MCP passed: all 23 tests in the existing engineering project passed again. Nine workflow checks also passed, covering the default remote directory and zsh, 16 bound tools, guarded edits, unified patch validation and application, execution of the modified code, Git and saved edit diffs, local audit metadata, and the local viewer.
- The edit checks used a newly created disposable remote test directory. The agent and MCP stayed local; no Codex CLI was installed or run remotely.

## Historical remote engineering exercise on the predecessor — 2026-10-23

A separate Python CSV statistics project was developed over SSH using the predecessor package before the rename. Its final 23 unit and acceptance tests passed, including a 50,000-row input case. The exercise covered an initial failing feature test, implementation, reviewable Git changes, and final checks. The renamed MCP reran the final 23 tests successfully, as recorded above; the entire development sequence was not repeated.

Earlier real-host MCP/SSH and App audit-viewer checks were also performed on the predecessor. Raw records, host names, personal paths, and screenshots are intentionally not included in this repository.

Every live test requires an explicit `--host`; some also require a parent or target directory. These tests execute commands and make changes on the selected remote host, so run them only against a disposable test area.

The local audit viewer was designed to show the actual commands, output, and diffs in a browser. It does not create native Codex terminal or file-change cards. The compact workspace MCP defaults (2 KiB for command output, 4 KiB for file/patch/diff previews) reduce tool-result payloads; they do not prevent token use when an agent reads full output through MCP.
