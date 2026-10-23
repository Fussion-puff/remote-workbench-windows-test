"""Shared test fixtures."""

import pytest
from unittest.mock import AsyncMock

from remote_workbench.config import HostConfig
from remote_workbench.ssh import SSHExecutor, SSHResult


@pytest.fixture
def host_config():
    return HostConfig(
        ssh_host="test.example.com",
        user="testuser",
        preamble="source ~/env.sh",
        default_cwd="~/project",
    )


@pytest.fixture
def mock_executor(host_config):
    """An SSHExecutor with mocked run/check methods for tool tests."""
    executor = SSHExecutor(host_config)
    executor.run = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    executor.check_connection = AsyncMock(return_value=(True, "OK"))
    executor.read_file_raw = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    executor.write_file_raw = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    return executor


import asyncio
import os
import shlex
import sys
from pathlib import Path
from remote_workbench.tools import files, git, commands, patch, search

@pytest.fixture
async def local(tmp_path, monkeypatch):
    monkeypatch.setenv('REMOTE_WORKBENCH_AUDIT_DIR', str(tmp_path / 'audit'))
    shell = tmp_path / 'native-shell'
    shell.write_text('#!/bin/sh\n[ "$1" = "-lic" ] || exit 99\nshift\nexec /bin/bash --noprofile --norc -c "$1"\n')
    shell.chmod(0o700)
    real_create = asyncio.create_subprocess_exec
    async def transport(*args, **kwargs):
        assert args[0] == 'ssh'
        script = args[-1]
        env = dict(os.environ, HOME=str(tmp_path), SHELL=str(shell), PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'])
        return await real_create('/bin/bash', '--noprofile', '--norc', '-c', script, env=env, **kwargs)
    monkeypatch.setattr('remote_workbench.ssh.asyncio.create_subprocess_exec', transport)
    executor = SSHExecutor(HostConfig(ssh_host='local-simulated', default_cwd=str(tmp_path)))
    for module in (files, git, commands, patch, search):
        monkeypatch.setattr(module, 'require_connection', AsyncMock(return_value=executor))
    return executor, tmp_path
