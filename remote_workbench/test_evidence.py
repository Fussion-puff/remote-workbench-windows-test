"""Conservative test observations tied to content, command and configuration.

A file digest does not certify installed dependencies or an environment activated
inside an arbitrary shell command. Those remain explicitly unmeasured.
"""
from __future__ import annotations

import hashlib
import json
import time

from remote_workbench.baseline import project_fingerprint


def config_fingerprint(snapshot):
    config = snapshot.workspace.executor.config
    selected = {name: getattr(config, name, None) for name in (
        'ssh_host', 'user', 'port', 'preamble', 'command_timeout', 'job_timeout',
        'terminate_grace_seconds')}
    selected.update(cwd=snapshot.cwd, project_root=snapshot.project_root, host=snapshot.host)
    return hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()


async def capture(snapshot):
    try:
        content = await project_fingerprint(snapshot.workspace.executor, snapshot.project_root)
    except Exception as exc:
        content = {'complete': False, 'digest': None, 'reason': type(exc).__name__}
    return {'code': content, 'config_digest': config_fingerprint(snapshot), 'observed_at': time.time()}


def observation(before, after, result, command, revision):
    known = result.get('execution_state') == 'finished' and not result.get('timed_out') and not result.get('error')
    stable = matches(before, after)
    return {
        'operation_id': result.get('operation_id'), 'output_id': result.get('output_id'),
        'exit_code': result.get('exit_code'), 'command': command, 'revision': revision,
        'status': ('passed' if result.get('exit_code') == 0 else 'failed') if known else 'unknown',
        'observed_at': time.time(), 'evidence': 'command_exit_and_bounded_content_fingerprints',
        'before': before, 'after': after, 'code_unchanged_during_test': stable,
        'applies_to_current_code': True if known and stable else None,
        'environment_evidence': 'command_and_host_configuration_only',
        'environment_limit': 'Activated shell environment, installed dependencies and external inputs were not fingerprinted.',
    }


def matches(left, right):
    a, b = left.get('code', {}), right.get('code', {})
    return bool(a.get('complete') and b.get('complete') and a.get('digest') and
                a['digest'] == b.get('digest') and left.get('config_digest') == right.get('config_digest'))


def compare(last_test, current):
    if last_test is None:
        return None
    value = dict(last_test, checked_at=time.time(), applies_to_current_code=None, historical=True)
    before, after = last_test.get('before'), last_test.get('after')
    if last_test.get('status') in {'passed', 'failed'} and before and after and matches(before, after):
        value['applies_to_current_code'] = matches(after, current) if current.get('code', {}).get('complete') else None
        value['historical'] = value['applies_to_current_code'] is not True
    value['environment_verified_current'] = False
    return value
