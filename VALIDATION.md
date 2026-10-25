# Validation status

## Compact task workflow (0.3.0a11 develop candidate)

- Full local regression after the explicit-byte-budget fix: 477 tests passed (99.35 seconds). The later inclusive 2% gate passed its 10 targeted checks. Wheel/sdist builds passed; this develop candidate has not been promoted to main or the independent stable installation.
- Opt-in session profile: eight exposed tools; advanced operations retain their original validation and routing behind an on-demand gateway. The full profile remains the default.
- Both local and real IHEP disposable private copies passed the same 21-case acceptance and 18 project tests across eight scripted tool steps. The context patch was checked/applied once; full command output and normalized Git diffs remain in the local audit viewer. These are functional replays, not independent model usage measurements.
- Successful test replies in this replay shrank from 1183 to 373 UTF-8 bytes after removing repeated normal metadata. Bytes are not tokens or subscription usage.
- The current complete-task ceiling is **2% extra**, inclusive, with original functionality preserved. A fresh actual nine-vs-nine pair on the loaded candidate used 355,677 native vs 372,086 remote tokens (+4.613%); both passed the same 21-case independent acceptance, but the usage gate remains **not qualified**. Original 1% historical reports are retained. The checker rejects different baseline/task hashes, models/settings, request counts, failed acceptance and incomplete/retry-excluded totals.
- Independent stable remains 0.3.0a10. A local project-only override selects the develop candidate without changing global MCP registration or other projects.

## Stable source promotion and GNU grep (0.3.0a10)

- Full local regression suite: 437 tests passed (95.82 seconds).
- GitHub Actions for the preceding develop commit exposed single-file GNU grep output without filenames on Python 3.10–3.13. The tool now requests `-H` explicitly; native-shell test assertions remain unchanged.
- `main` is the stable source branch. A fixed commit is promoted after develop CI succeeds; main CI and independent local wheel acceptance are checked before updating the local stable installation.

## Default viewer refresh (0.3.0a9)

- Targeted viewer/session regressions: 42 tests passed (7.05 seconds), including the shipped HTML checkbox default, polling without a click, manual off, page restore, and manual refresh after disabling polling.
- Full-suite results below belong to their recorded releases; the HTML default change does not alter command execution or image fetching.

## Chat image fetch (0.3.0a8)

- Full local regression suite: 435 tests passed (95.86 seconds).
- Actual develop MCP stdio acceptance passed for PNG, JPG, SVG and PDF page 2, using a local SSH fixture. Binary originals matched; generated previews opened successfully and the PDF page content was checked.
- Each MCP reply is one compact text result, measured at 1040–1068 UTF-8 bytes in this fixture, with local paths and chat Markdown rather than binary/base64 payloads. These are byte counts, not tokenizer measurements.
- Separate chat caches were verified. Missing, non-image, oversized and nonregular files, invalid pages, unavailable renderers, and external/active SVG references are covered. The remote worker still parses as Python 3.9; image rendering libraries run locally.
- No real server connection or Codex App chat-rendering acceptance was performed. The HTML viewer is unchanged. See [聊天内看图](docs/images.zh-CN.md).

## Viewer instance isolation (0.3.0a7)

- Full local regression suite: 410 tests passed (92.89 seconds). Targeted viewer/session checks also passed: 40 tests (6.34 seconds).
- Coverage includes OS-assigned loopback ports, a foreign local HTML service holding the preferred port, simultaneous viewers, port reuse with stale URLs, instance response headers, workspace-scoped routes, and browser auto-refresh identity checks.
- Actual develop launcher acceptance over MCP stdio and loopback HTTP passed: two MCP processes, three local chats, separate audit records, valid instance links, and stale/cross-chat record rejection. SSH invocation count: zero.
- Tests use loopback HTTP and simulated SSH. Codex App integration and real IHEP/CERN SSH execution are separate acceptance steps; neither is implied by these results.
- Implementation and handoff state: [Develop 交接记录](docs/develop-handoff.zh-CN.md).

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
