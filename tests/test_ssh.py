"""Tests for SSHExecutor command wrapping and result handling."""

import pytest
import os
import subprocess
import sys
from pathlib import Path

from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor, SSHResult, _is_ssh_error


class TestCommandWrapping:
    def run_wrapped(self, command, cwd, home, preamble=""):
        executor = SSHExecutor(HostConfig(ssh_host="test", preamble=preamble))
        env = dict(os.environ, HOME=str(home), ZDOTDIR=str(home),
                   REMOTE_WORKBENCH_LOGIN_SHELL="/bin/sh",
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        return subprocess.run(["/bin/sh", "-c", executor._wrap_command(command, str(cwd))],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_wrap_applies_preamble(self, tmp_path):
        value = self.run_wrapped('printf "%s" "$REMOTE_WORKBENCH_TEST_VALUE"', tmp_path, tmp_path,
                                 preamble="export REMOTE_WORKBENCH_TEST_VALUE=loaded")
        assert value.returncode == 0 and value.stdout == "loaded", value

    def test_wrap_enters_requested_directory(self, tmp_path):
        target = tmp_path / "project"
        target.mkdir()
        value = self.run_wrapped("pwd", target, tmp_path)
        assert value.returncode == 0 and value.stdout.strip() == str(target), value

    def test_wrap_uses_interactive_login_account_shell(self, tmp_path):
        value = self.run_wrapped('printf "%s:%s" "$0" "$-"', tmp_path, tmp_path)
        name, options = value.stdout.strip().split(":", 1)
        assert value.returncode == 0 and name.startswith("-") and "i" in options, value

    def test_wrap_cd_failure_stops_user_command(self, tmp_path):
        value = self.run_wrapped("printf must-not-run", tmp_path / "absent", tmp_path)
        assert value.returncode == 125 and "must-not-run" not in value.stdout, value

    def test_wrap_quotes_directory_name(self, tmp_path):
        target = tmp_path / "space and 'quotes' ; $literal"
        target.mkdir()
        value = self.run_wrapped("pwd", target, tmp_path)
        assert value.returncode == 0 and value.stdout.strip() == str(target), value


class TestSSHResult:
    def test_basic_result(self):
        r = SSHResult(stdout="hello\n", stderr="", exit_code=0)
        assert r.stdout == "hello\n"
        assert r.exit_code == 0
        assert not r.timed_out

    def test_timeout_result(self):
        r = SSHResult(stdout="", stderr="timed out", exit_code=-1, timed_out=True)
        assert r.timed_out
        assert r.exit_code == -1


class TestIsSSHError:
    def test_connection_refused(self):
        assert _is_ssh_error("ssh: connect to host x port 22: Connection refused")

    def test_stale_socket(self):
        assert _is_ssh_error("Control socket connect(/tmp/ctrl): No such file or directory")

    def test_mux_error(self):
        assert _is_ssh_error("mux_client_request_session: session request failed")

    def test_host_key_failed(self):
        assert _is_ssh_error("Host key verification failed.")

    def test_normal_stderr_not_ssh_error(self):
        assert not _is_ssh_error("grep: /tmp/foo: No such file or directory")

    def test_empty_stderr(self):
        assert not _is_ssh_error("")

    def test_command_stderr(self):
        assert not _is_ssh_error("python: can't open file 'foo.py': No such file or directory")

    def test_permission_denied_not_ssh_error(self):
        # Generic "Permission denied" from a command is not an SSH error
        assert not _is_ssh_error("Permission denied")


class TestConnectionCheckCache:
    def setup_method(self):
        self.config = HostConfig(ssh_host="x")
        self.executor = SSHExecutor(self.config)

    def test_invalidate_cache(self):
        # Simulate a cached successful check
        self.executor._last_check_ok = True
        self.executor._last_check = 999999999999.0  # far future
        self.executor.invalidate_cache()
        assert not self.executor._last_check_ok
