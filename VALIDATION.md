# Validation status

## Develop task lifecycle and compute review (0.3.0a5) — 2026-10-24

- Final local regression suite: 316 tests passed (121.95 seconds), using simulated SSH and bounded local processes.
- Coverage includes cancellation, controller disconnect/heartbeat expiry, foreground and detached deadlines, owned-group cleanup, identity mismatch refusal, job stopping after workspace switches, and compute-review refusal before remote execution. Six local shell variants remain covered.
- Wheel and source-distribution builds passed. Archive inspection confirmed that private runtime/configuration directories are absent. The extracted wheel imported the new modules, advertised the cleanup entry point, exposed 17 fixed-workspace and 20 session tools, and its remote worker source parsed with Python 3.9 syntax compatibility.
- No IHEP/CERN commands or resource-exhaustion reproductions were run for this version. Cluster acceptance is still pending. The independent stable installation remains 0.2.4.
- Process-group cleanup is best effort for processes which stay in that group. Compute approval is an agent-reported user-confirmation workflow, not an OS sandbox or independently verified chat authorization; see [task lifecycle boundaries](docs/process-safety.zh-CN.md).

## Current account-shell update (0.2.1) — 2026-10-23

- Full regression suite: 192 tests passed (33.87 seconds).
- Real local shell coverage: bash, sh, dash, ksh, zsh, and tcsh. Tests exercise login startup files, command and background exit codes, guarded file edits, Git, search, directory listing, binary stdin, framing, quoted paths, trace output, and refusal to execute after a directory change fails.
- Real remote recheck: 23 engineering tests and 11 workflow checks passed. The selected shell was compared with the server account's system login-shell entry; no account or global shell configuration was changed.
- A freshly installed 0.2.1 wheel passed all 10 entry-point/resource/MCP smoke checks. Both new helper modules are present in the wheel, and generated remote code was checked for Python 3.9 syntax compatibility.
- Commands and detached jobs share account-shell selection. Generated file/search/Git operations pass argument arrays instead of relying on the account shell's parsing rules. Limited search output is drained to preserve the real process exit code.
- The MCP channel has no PTY. Interactive login startup files are loaded, but terminal-only initialization and job control can differ from a manual SSH terminal.

## Initial independent distribution (0.2.0) — 2026-10-23

- Local regression suite: 154 tests passed in the independent `remote-workbench` checkout environment (16.17 seconds).
- MCP stdio initialization handshake passed.
- Wheel and source distribution builds passed.
- Independent environment installation with the locked dependencies passed. The renamed package imports from the independent checkout and reports version 0.2.0.
- The built wheel was installed in a separate empty environment with dependencies constrained by the lock file. Ten smoke checks passed, including all six command help entries, isolated package imports and bundled resources, and MCP stdio handshakes exposing 25 multi-host tools or 16 bound-workspace tools. The upstream `onirika` package was absent from that environment.
- Remote recheck through the renamed MCP passed: all 23 tests in the existing engineering project passed again. Nine workflow checks also passed, covering the default remote directory and the tested account's login shell, 16 bound tools, guarded edits, unified patch validation and application, execution of the modified code, Git and saved edit diffs, local audit metadata, and the local viewer.
- The edit checks used a newly created disposable remote test directory. The agent and MCP stayed local; no Codex CLI was installed or run remotely.

## Historical remote engineering exercise on the predecessor — 2026-10-23

A separate Python CSV statistics project was developed over SSH using the predecessor package before the rename. Its final 23 unit and acceptance tests passed, including a 50,000-row input case. The exercise covered an initial failing feature test, implementation, reviewable Git changes, and final checks. The renamed MCP reran the final 23 tests successfully, as recorded above; the entire development sequence was not repeated.

Earlier real-host MCP/SSH and App audit-viewer checks were also performed on the predecessor. Raw records, host names, personal paths, and screenshots are intentionally not included in this repository.

Every live test requires an explicit `--host`; some also require a parent or target directory. These tests execute commands and make changes on the selected remote host, so run them only against a disposable test area.

The local audit viewer was designed to show the actual commands, output, and diffs in a browser. It does not create native Codex terminal or file-change cards. The compact workspace MCP defaults (2 KiB for command output, 4 KiB for file/patch/diff previews) reduce tool-result payloads; they do not prevent token use when an agent reads full output through MCP.
