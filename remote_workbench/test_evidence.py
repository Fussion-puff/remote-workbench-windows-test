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


def invalidate(last_test, operation):
    """Keep the result as history while withdrawing current-code applicability."""
    if last_test is None:
        return None
    value = dict(last_test)
    value.update(
        applies_to_current_code=None,
        historical=True,
        invalidated=True,
        invalidated_by=operation,
        invalidated_at=time.time(),
    )
    return value


def matches(left, right):
    return _same_known_state(left, right) is True


def _same_known_state(left, right):
    a, b = left.get('code', {}), right.get('code', {})
    if (not a.get('complete') or not b.get('complete') or not a.get('digest')
            or not b.get('digest')):
        return None
    if a['digest'] != b['digest']:
        return False
    if not left.get('config_digest') or not right.get('config_digest'):
        return None
    return left['config_digest'] == right['config_digest']


def compare(last_test, current):
    if last_test is None:
        return None
    value = dict(last_test, checked_at=time.time(), applies_to_current_code=None, historical=True)
    current_code = current.get('code', {})
    value.update(
        test_execution_is_historical=True,
        current_code_checked=True,
        applicability_check_source='workspace_snapshot_fresh_bounded_fingerprint',
        applicability_checked_at=current.get('observed_at') or time.time(),
        applicability_check_scope=current_code.get('scope'),
        applicability_check_configuration='workspace_execution_config_fingerprint',
    )
    if last_test.get('invalidated'):
        value['applicability_revalidated_after_invalidation'] = False
    before, after = last_test.get('before'), last_test.get('after')
    if after and after.get('config_digest') and current.get('config_digest'):
        value['applicability_configuration_matched'] = (
            after['config_digest'] == current['config_digest'])
    else:
        value['applicability_configuration_matched'] = None
    if last_test.get('status') in {'passed', 'failed'} and before and after and matches(before, after):
        if current_code.get('complete'):
            current_matches = _same_known_state(after, current)
            if last_test.get('invalidated'):
                value['applies_to_current_code'] = current_matches
                value['applicability_revalidated_after_invalidation'] = current_matches is True
            else:
                value['applies_to_current_code'] = current_matches
            value['historical'] = value['applies_to_current_code'] is not True
    value['environment_verified_current'] = False
    return value
