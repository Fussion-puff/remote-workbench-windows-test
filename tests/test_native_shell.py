"""Exercise login-shell launch behavior against the local shells we have installed.

These tests intentionally run real shells. They check the behavior consumed by the
SSH transport rather than the spelling of the generated shell command.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from remote_workbench import remote_shell
from remote_workbench import ssh as ssh_module
from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor
from remote_workbench.tools import commands, files, git, patch, search


SHELLS = ("bash", "sh", "dash", "ksh", "zsh", "tcsh")


def _isolated_login_environment(monkeypatch, tmp_path: Path, shell: str) -> None:
    """Keep native login startup files inside pytest's temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    monkeypatch.setenv("REMOTE_WORKBENCH_LOGIN_SHELL", shell)
    # A shell that trusts the account-captured value must replace this stale
    # environment value before starting the native login shell.
    monkeypatch.setenv("SHELL", "/definitely/not/the/account-shell")
    monkeypatch.delenv("TERM", raising=False)


def _run(spec: dict, input_bytes: bytes = b"", env: dict | None = None) -> subprocess.CompletedProcess:
    child_env = os.environ.copy() if env is None else dict(env)
    child_env.update(spec["env_updates"])
    return subprocess.run(
        spec["argv"],
        executable=spec["shell"],
        env=child_env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("shell_name", SHELLS)
def test_login_shell_preserves_binary_stdin_cwd_environment_and_markers(
    shell_name: str, tmp_path: Path, monkeypatch
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _isolated_login_environment(monkeypatch, tmp_path, shell)

    project = tmp_path / "project with spaces"
    project.mkdir()
    markers = {
        "stdout": "\x1eRW_CWD=%s\x1f\n",
        "stderr": "\x1eRW_ERR_CWD=%s\x1f\n",
    }
    command = (
        "printf 'cwd=%s\\n' \"$PWD\"; "
        "printf 'injected=%s\\n' \"$REMOTE_WORKBENCH_MATRIX_VALUE\"; cat"
    )
    spec = remote_shell.login_spec(
        command,
        str(project),
        markers=markers,
        environment={"REMOTE_WORKBENCH_MATRIX_VALUE": "from-launch-spec"},
    )
    binary_stdin = b"stdin-binary:\x00\xff\r\nend"

    result = _run(spec, binary_stdin)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    expected_cwd = str(project).encode()
    assert b"\x1eRW_CWD=" + expected_cwd + b"\x1f\n" in result.stdout
    assert b"\x1eRW_ERR_CWD=" + expected_cwd + b"\x1f\n" in result.stderr
    assert b"cwd=" + expected_cwd + b"\n" in result.stdout
    assert b"injected=from-launch-spec\n" in result.stdout
    assert result.stdout.endswith(binary_stdin)
    assert spec["env_updates"]["SHELL"] == shell
    assert spec["env_updates"]["REMOTE_WORKBENCH_LOGIN_SHELL"] == shell
    assert spec["env_updates"]["TERM"] == "dumb"


def _write_login_startup_file(shell_name: str, home: Path) -> None:
    if shell_name == "bash":
        path = home / ".bash_profile"
        body = "export REMOTE_WORKBENCH_NATIVE_RC=loaded\n"
    elif shell_name == "zsh":
        path = home / ".zprofile"
        body = "export REMOTE_WORKBENCH_NATIVE_RC=loaded\n"
    elif shell_name == "tcsh":
        path = home / ".login"
        body = "setenv REMOTE_WORKBENCH_NATIVE_RC loaded\n"
    else:
        path = home / ".profile"
        body = "export REMOTE_WORKBENCH_NATIVE_RC=loaded\n"
    path.write_text(body)


@pytest.mark.parametrize("shell_name", SHELLS)
def test_native_login_startup_runs_before_preamble_and_environment(
    shell_name: str, tmp_path: Path, monkeypatch
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _isolated_login_environment(monkeypatch, tmp_path, shell)
    _write_login_startup_file(shell_name, tmp_path)
    monkeypatch.setenv("REMOTE_WORKBENCH_TEST_ORDER", "before-preamble")

    spec = remote_shell.login_spec(
        "printf 'command-rc=%s command-env=%s\\n' \"$REMOTE_WORKBENCH_NATIVE_RC\" \"$REMOTE_WORKBENCH_TEST_ORDER\"",
        str(tmp_path),
        preamble="printf 'preamble-rc=%s preamble-env=%s\\n' \"$REMOTE_WORKBENCH_NATIVE_RC\" \"$REMOTE_WORKBENCH_TEST_ORDER\"",
        environment={"REMOTE_WORKBENCH_TEST_ORDER": "after-preamble"},
    )
    result = _run(spec)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"preamble-rc=loaded preamble-env=before-preamble\n" in result.stdout
    assert b"command-rc=loaded command-env=after-preamble\n" in result.stdout


@pytest.mark.parametrize("shell_name", SHELLS)
def test_login_shell_returns_command_failure_and_trace(shell_name: str, tmp_path: Path, monkeypatch) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _isolated_login_environment(monkeypatch, tmp_path, shell)

    spec = remote_shell.login_spec(
        "printf 'TRACE_SENTINEL\\n'; exit 37",
        str(tmp_path),
        trace=True,
    )
    result = _run(spec)

    assert result.returncode == 37
    assert b"TRACE_SENTINEL\n" in result.stdout
    assert b"TRACE_SENTINEL" in result.stderr


@pytest.mark.parametrize("shell_name", SHELLS)
def test_missing_cwd_fails_before_command(shell_name: str, tmp_path: Path, monkeypatch) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _isolated_login_environment(monkeypatch, tmp_path, shell)

    spec = remote_shell.login_spec("printf COMMAND_SHOULD_NOT_RUN", str(tmp_path / "missing"))
    result = _run(spec)

    assert result.returncode == 125
    assert b"COMMAND_SHOULD_NOT_RUN" not in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("shell_name", SHELLS)
async def test_file_git_search_and_job_tools_run_under_native_shell(
    shell_name: str, tmp_path: Path, monkeypatch
) -> None:
    found = shutil.which(shell_name)
    if found is None:
        pytest.skip(f"{shell_name} is not installed")
    shell = os.path.realpath(found)
    _isolated_login_environment(monkeypatch, tmp_path, shell)
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = HostConfig(ssh_host="native-shell-test", execution_enabled=True, default_cwd=str(workspace))
    executor = SSHExecutor(config, remote_state_dir=str(workspace / ".remote-workbench-state"))

    async def require_test_executor(*_args, **_kwargs):
        return executor

    for module in (files, git, commands, patch, search):
        monkeypatch.setattr(module, "require_connection", require_test_executor)
    monkeypatch.setattr(commands, "get_executor", lambda host=None: executor)

    real_create = asyncio.create_subprocess_exec
    child_env = os.environ.copy()
    child_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + child_env.get("PATH", "")

    async def local_ssh(*args, **kwargs):
        assert args[0] == "ssh"
        return await real_create(shell, "-c", args[-1], env=child_env, **kwargs)

    monkeypatch.setattr(ssh_module.asyncio, "create_subprocess_exec", local_ssh)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: object = None) -> None:
        if not condition:
            failures.append(f"{label}: {detail!r}")

    command = await commands.ssh_run("printf 'native-command\\n'; exit 6", cwd=str(workspace))
    check("native_command_exit", command.get("exit_code") == 6 and "native-command" in command.get("stdout", ""), command)

    written = await files.ssh_write_file("sample.txt", "before\n")
    check("write_file", written.get("success") is True, written)

    initialized = await commands.ssh_run(
        "git init -q && git add -- sample.txt && "
        "git -c user.name=ShellMatrix -c user.email=shell-matrix@example.invalid "
        "commit -qm baseline",
        cwd=str(workspace),
    )
    check("git_baseline", initialized.get("exit_code") == 0, initialized)

    read = await files.ssh_read_file("sample.txt")
    check("read_file", "before" in read.get("content", ""), read)
    patched = await files.ssh_patch_file(
        "sample.txt", old="before", new="after", expected_sha256=read.get("sha256")
    )
    check("patch_file", patched.get("success") is True and "+after" in patched.get("diff", ""), patched)

    status = await git.ssh_git_status(cwd=str(workspace))
    check("git_status", "M sample.txt" in status.get("modified", []), status)
    diff = await git.ssh_git_diff(cwd=str(workspace))
    check("git_diff", "sample.txt" in diff.get("summary", "") and "1\t1" in diff.get("summary", ""), diff)

    matches = await search.ssh_grep("after", path="sample.txt")
    check("grep", matches.get("count") == 1 and any("sample.txt" in line for line in matches.get("matches", [])), matches)
    found_files = await search.ssh_find_files(path=".", pattern="*.txt", max_depth=1)
    check("find_files", found_files.get("count") == 1 and any("sample.txt" in path for path in found_files.get("files", [])), found_files)
    listing = await files.ssh_list_dir(path=".")
    check("list_dir", any("sample.txt" in line for line in listing.get("entries", [])), listing)

    job = await commands.ssh_run_background(
        "printf 'native-job\\n'; exit 7", cwd=str(workspace), job_id=f"native-{shell_name}"
    )
    check("start_job", bool(job.get("job_id")), job)
    job_status: dict = {}
    if job.get("job_id"):
        for _ in range(80):
            job_status = await commands.ssh_job_status(job["job_id"])
            if job_status.get("exit_code") is not None:
                break
            await asyncio.sleep(0.025)
        check(
            "job_status",
            job_status.get("exit_code") == 7 and job_status.get("stdout_tail") == "native-job\n",
            job_status,
        )

    assert not failures, f"{shell_name}:\n" + "\n".join(failures)


def test_account_shell_selection_and_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_WORKBENCH_LOGIN_SHELL", "/bin/bash")
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    monkeypatch.setattr(remote_shell.pwd, "getpwuid", lambda *_: pytest.fail("captured account shell should win"))
    assert remote_shell.login_shell() == "/bin/bash"

    monkeypatch.delenv("REMOTE_WORKBENCH_LOGIN_SHELL")
    monkeypatch.setattr(remote_shell.os, "getuid", lambda: 4242)
    monkeypatch.setattr(remote_shell.pwd, "getpwuid", lambda _: SimpleNamespace(pw_shell="/bin/ksh"))
    assert remote_shell.login_shell() == "/bin/ksh"

    monkeypatch.setattr(remote_shell.pwd, "getpwuid", lambda _: SimpleNamespace(pw_shell=""))
    assert remote_shell.login_shell() == "/bin/sh"

    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(remote_shell.pwd, "getpwuid", lambda _: (_ for _ in ()).throw(OSError("no passwd entry")))
    assert remote_shell.login_shell() == "/bin/zsh"

    monkeypatch.delenv("SHELL")
    assert remote_shell.login_shell() == "/bin/sh"


def test_existing_term_is_preserved_in_launch_environment(tmp_path: Path, monkeypatch) -> None:
    shell = shutil.which("sh")
    assert shell is not None
    _isolated_login_environment(monkeypatch, tmp_path, shell)
    monkeypatch.setenv("TERM", "screen-256color")

    spec = remote_shell.login_spec("printf '%s\\n' \"$TERM\"", str(tmp_path))

    assert "TERM" not in spec["env_updates"]
    result = _run(spec)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert b"screen-256color\n" in result.stdout


@pytest.mark.parametrize("shell_name", SHELLS)
async def test_ssh_command_bootstrap_survives_outer_native_shell_and_opaque_paths(
    shell_name: str, tmp_path: Path, monkeypatch
) -> None:
    found = shutil.which(shell_name)
    if found is None:
        pytest.skip(f"{shell_name} is not installed")
    shell = os.path.realpath(found)
    _isolated_login_environment(monkeypatch, tmp_path, shell)

    project = tmp_path / "project with 'quote' !;literal$cash"
    project.mkdir()
    state_dir = tmp_path / "state with 'quote' !;literal$cash"
    payload = b"outer-wrapper-stdin:\x00\xff\r\nend"
    command = (
        "printf 'cwd=%s\\nstate=%s\\n' "
        "\"$PWD\" \"$REMOTE_WORKBENCH_REMOTE_STATE_DIR\"; cat"
    )
    executor = SSHExecutor(
        HostConfig(ssh_host="shell-test", execution_enabled=True, default_cwd=str(project)),
        remote_state_dir=str(state_dir),
    )
    child_env = os.environ.copy()
    child_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + child_env.get("PATH", "")

    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(tmp_path / "audit"))
    real_create = asyncio.create_subprocess_exec
    async def transport(*args, **kwargs):
        return await real_create(shell, "-c", args[-1], env=child_env, **kwargs)
    monkeypatch.setattr(ssh_module.asyncio, "create_subprocess_exec", transport)
    result = await executor.run(command, input_data=payload, timeout=15)

    from remote_workbench import audit
    output = (audit.directory(result.output_id) / "stdout").read_bytes()
    record = __import__("json").loads((audit.directory(result.output_id) / "record.json").read_text())
    assert result.exit_code == 0, result.stderr
    assert record["actual_cwd"] == str(project)
    assert f"cwd={project}\n".encode() in output
    assert f"state={state_dir}\n".encode() in output
    assert output.endswith(payload)
