"""SSH executor — runs commands on remote hosts via ControlMaster sockets."""

from __future__ import annotations

import asyncio
import shlex
import time
import os
import re
import hashlib
import shutil
from pathlib import Path
from remote_workbench import audit
from dataclasses import dataclass

from remote_workbench.config import HostConfig

# How long a successful connection check remains valid (seconds).
_CHECK_CACHE_TTL = 5.0


@dataclass
class SSHResult:
    """Result of a remote SSH command execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    output_id: str = ""
    truncated: bool = False
    execution_state: str = "finished"


class SSHExecutor:
    """Executes commands on a remote host via an existing SSH ControlMaster socket.

    Requires the user to have established a ControlMaster connection beforehand
    (e.g. via `ssh -M -S <socket> user@host`). This class never handles
    authentication — it piggybacks on the existing session.
    """

    def __init__(self, host_config: HostConfig, remote_state_dir: str | None = None):
        self.config = host_config
        self.remote_state_dir = remote_state_dir
        self._control_path = host_config.resolved_control_path
        self._last_check: float = 0.0
        self._last_check_ok: bool = False

    async def check_connection(self, force: bool = False) -> tuple[bool, str]:
        """Verify the ControlMaster socket is alive.

        Caches a successful result for a few seconds to reduce overhead
        when multiple tools are called in quick succession.

        Returns (is_connected, message).
        """
        now = time.monotonic()
        if not force and self._last_check_ok and (now - self._last_check) < _CHECK_CACHE_TTL:
            return True, "Connection OK (cached)"

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-O", "check",
                "-S", self._control_path,
                self.config.ssh_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            self._last_check_ok = False
            return False, "SSH connection check timed out."
        except FileNotFoundError:
            self._last_check_ok = False
            return False, "ssh binary not found. Ensure OpenSSH is installed."
        except BaseException:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            self._last_check_ok = False
            raise

        output = (stdout.decode() + stderr.decode()).strip()

        if proc.returncode == 0:
            self._last_check = now
            self._last_check_ok = True
            return True, output

        self._last_check_ok = False
        return False, (
            f"SSH ControlMaster socket is not active.\n"
            f"Please authenticate first:\n"
            f"  ssh -M -S {self.config.control_path} {self.config.ssh_target}\n"
            f"(This handles Kerberos/2FA in your terminal, "
            f"then the MCP server piggybacks on the connection.)"
        )

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        input_data: bytes | None = None,
        output_budget: int | None = None,
        trace: bool = False,
        display_command: str | None = None,
    ) -> SSHResult:
        """Run a command on the remote host.

        Args:
            command: Shell command to execute.
            cwd: Working directory on the remote. Defaults to host config's default_cwd.
            timeout: Timeout in seconds. Defaults to host config's command_timeout.
            input_data: Data to pipe to the command's stdin.

        Returns:
            SSHResult with stdout, stderr, exit code, and timeout flag.
        """
        cwd = cwd if cwd is not None else self.config.default_cwd
        timeout = timeout if timeout is not None else self.config.command_timeout
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        budget = self.config.max_output_bytes if output_budget is None else output_budget
        if not 256 <= budget <= 1048576:
            raise ValueError("output_budget must be between 256 and 1048576 bytes")
        record_id = audit.create(kind="command", parent_id=audit.operation_id.get(),
            host=self.config.ssh_target, cwd=cwd, command=display_command or command, status="starting",
            stdin_bytes=len(input_data or b""), stdin_sha256=hashlib.sha256(input_data or b"").hexdigest())
        audit.add_child(audit.operation_id.get(), record_id)
        wrapped = self._wrap_command(command, cwd, record_id, trace)
        audit.update(record_id, wrapped_command=wrapped, shell_trace=trace)
        if display_command is not None:
            audit.update(record_id, executed_command=command)
        if input_data is not None:
            with audit.open_stream(record_id, "stdin") as output:
                output.write(input_data)

        ssh_args = [
            "ssh",
            "-T",
            "-S", self._control_path,
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",
            # OpenSSH normally falls back to a new connection when a master
            # disappears, even after a successful -O check. An inert proxy is
            # consulted only on that fallback path: an existing mux channel
            # works normally, but fallback cannot dial/authenticate anywhere.
            "-o", "ProxyCommand=false",
        ]
        if self.config.port != 22:
            ssh_args.extend(["-p", str(self.config.port)])
        ssh_args.append(self.config.ssh_target)
        ssh_args.append(wrapped)

        started = time.monotonic()
        proc = None
        tasks = []
        actual_cwd = None
        marker = re.compile(rb"\x1eREMOTE_WORKBENCH_CWD_" + record_id.encode() + rb":([^\x1f]*)\x1f\n")
        stdout_marker = re.compile(rb"\x1eREMOTE_WORKBENCH_STDOUT_" + record_id.encode() + rb"\x1f\n")
        async def pump(reader, stream):
            nonlocal actual_cwd
            pending = b""
            looking = True
            pattern = marker if stream == "stderr" else stdout_marker
            startup_path = audit.directory(record_id) / ("startup_" + stream)
            with audit.open_stream(record_id, stream) as output, audit.open_stream(record_id, "startup_"+stream) as startup:
                try:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        if looking:
                            pending += chunk
                            match = pattern.search(pending)
                            if match:
                                if stream == "stderr":
                                    actual_cwd = match[1].decode("utf-8", "replace")
                                startup.write(pending[:match.start()])
                                output.write(pending[match.end():])
                                pending = b""
                                looking = False
                            elif len(pending) > 65536:
                                startup.write(pending[:-1024])
                                pending = pending[-1024:]
                        else:
                            output.write(chunk)
                finally:
                    if looking:
                        # A failed startup/cd may exit before the delimiter. Keep
                        # diagnostics in the primary stream in that case.
                        startup.flush()
                        with startup_path.open("rb") as prefix:
                            shutil.copyfileobj(prefix, output)
                        output.write(pending)
        async def feed():
            if proc.stdin is not None:
                try:
                    proc.stdin.write(input_data or b"")
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(*ssh_args,
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            audit.update(record_id, status="running")
            tasks = [asyncio.create_task(pump(proc.stdout, "stdout")),
                     asyncio.create_task(pump(proc.stderr, "stderr")), asyncio.create_task(feed())]
            await asyncio.wait_for(asyncio.gather(proc.wait(), *tasks), timeout=timeout)
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            timed_out = True
            exit_code = -1
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            audit.update(record_id, timeout_seconds=timeout)
        except OSError as e:
            exit_code = -1
            with audit.open_stream(record_id, "stderr") as output:
                output.write(str(e).encode())
        except BaseException:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            audit.update(record_id, status="cancelled", execution_state="remote_state_unknown")
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        contents = {}
        sizes = {}
        for stream in ("stdout", "stderr"):
            path = audit.directory(record_id) / stream
            if not path.exists():
                with audit.open_stream(record_id, stream):
                    pass
            sizes[stream] = path.stat().st_size
        # Shared byte budget, allowing unused stderr space to go to stdout.
        err_budget = min(max(sizes["stderr"], 128 if timed_out else 0), budget // 2)
        out_budget = min(sizes["stdout"], budget - err_budget)
        err_budget = budget - out_budget
        truncated = False
        for stream, cap in (("stdout", out_budget), ("stderr", err_budget)):
            path = audit.directory(record_id) / stream
            with path.open("rb") as source:
                if sizes[stream] <= cap:
                    data = source.read(cap)
                elif cap > 64:
                    first = cap // 2 - 16
                    data = source.read(first) + b"\n[truncated bytes]\n"
                    source.seek(max(0, sizes[stream] - first))
                    data += source.read(first)
                else:
                    data = source.read(cap)
            text, cut = audit.preview(data, max(0, cap), self.config.max_output_lines)
            contents[stream] = text
            truncated |= cut or sizes[stream] > cap
        transport_failed = exit_code == 255 and _is_ssh_error(contents["stderr"])
        if transport_failed:
            self._last_check_ok = False
        state = "remote_state_unknown" if timed_out or transport_failed else "finished"
        audit.update(record_id, status="timeout" if timed_out else "finished", actual_cwd=actual_cwd,
            exit_code=exit_code, execution_state=state, elapsed_ms=round((time.monotonic()-started)*1000), output_bytes=sizes,
            startup_output_bytes={name: (audit.directory(record_id)/name).stat().st_size
                                  for name in ("startup_stdout", "startup_stderr")
                                  if (audit.directory(record_id)/name).exists()})
        if timed_out:
            # Partial output remains available; do not claim remote cancellation.
            message = f"Timed out after {timeout}s; remote state unknown. "
            contents["stderr"], _ = audit.preview((message + contents["stderr"]).encode(), err_budget)
        return SSHResult(contents["stdout"], contents["stderr"], exit_code, timed_out,
                         record_id, truncated, state)

    async def run_program(self, argv: list[str], cwd: str | None = None,
                          timeout: int | None = None, output_budget: int | None = None,
                          max_lines: int | None = None, path_indices=()) -> SSHResult:
        """Execute generated tool arguments without parsing them as shell syntax."""
        from remote_workbench.remote_program import program_command
        return await self.run(program_command(argv, max_lines=max_lines, path_indices=path_indices),
                              cwd=cwd, timeout=timeout, output_budget=output_budget,
                              display_command=shlex.join(argv))

    async def read_file_raw(self, path: str, timeout: int | None = None) -> SSHResult:
        """Read a remote file via cat. Uses file_timeout from config."""
        timeout = timeout or self.config.file_timeout
        result = await self.run_program(["cat", "--", path], path_indices=[2], timeout=timeout)
        if result.exit_code == 0:
            source = audit.directory(result.output_id) / "stdout"
            if source.stat().st_size > self.config.max_file_size:
                result.exit_code = -1
                result.stderr = "Raw read exceeds max_file_size"
            else:
                result.stdout = source.read_bytes().decode("utf-8", errors="replace")
                result.truncated = False
        return result

    async def write_file_raw(
        self, path: str, content: bytes, timeout: int | None = None
    ) -> SSHResult:
        """Write content to a remote file via stdin pipe.

        Writes to a temp file first, then atomically moves it to avoid partial writes.
        """
        timeout = timeout or self.config.file_timeout
        import json
        from remote_workbench.remote_client import edit
        value = await edit(self, {"op": "write", "path": path, "content": content.decode("utf-8")}, timeout=timeout)
        return SSHResult(stdout=json.dumps(value), stderr=value.get("message", ""),
                         exit_code=1 if value.get("error") else 0, output_id=value.get("output_id", ""),
                         execution_state=value.get("execution_state", "finished"))

    def invalidate_cache(self) -> None:
        """Force next check_connection to actually probe the socket."""
        self._last_check_ok = False

    def _wrap_command(self, command: str, cwd: str, record_id: str = "", trace: bool = False) -> str:
        """Start the configured account shell, without assuming its syntax."""
        from remote_workbench.remote_shell import python_command
        markers = None
        if record_id:
            markers = {"stdout": f"\x1eREMOTE_WORKBENCH_STDOUT_{record_id}\x1f\n",
                       "stderr": f"\x1eREMOTE_WORKBENCH_CWD_{record_id}:%s\x1f\n"}
        environment = {"REMOTE_WORKBENCH_REMOTE_STATE_DIR": self.remote_state_dir} if self.remote_state_dir else {}
        arguments = dict(command=command, cwd=cwd, preamble=self.config.preamble,
                         markers=markers, trace=trace, environment=environment)
        bootstrap = Path(__file__).with_name("remote_shell.py").read_text()
        bootstrap += "\n_spec = login_spec(**" + repr(arguments) + ")\n"
        bootstrap += "_env = dict(os.environ, **_spec['env_updates'])\n"
        bootstrap += "os.execvpe(_spec['shell'], _spec['argv'], _env)\n"
        return "exec " + python_command(bootstrap)


def _is_ssh_error(stderr: str) -> bool:
    """Detect SSH-level errors (as opposed to remote command failures).

    Only matches patterns that are specific to SSH transport failures,
    not generic error strings that remote commands might also produce.
    """
    indicators = [
        "ssh: connect to host",
        "Connection refused",
        "Connection reset by peer",
        "Connection closed by remote host",
        "Connection closed by UNKNOWN",
        "Control socket connect",
        "mux_client_request_session",
        "Host key verification failed",
        "Could not resolve hostname",
        "kex_exchange_identification",
        "ssh_exchange_identification",
    ]
    stderr_lower = stderr.lower()
    return any(ind.lower() in stderr_lower for ind in indicators)
