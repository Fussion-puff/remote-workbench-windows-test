from remote_workbench.test_evidence import observation, compare, invalidate


def point(digest='abc', complete=True, config='env'):
    return {'code': {'complete': complete, 'digest': digest}, 'config_digest': config}


def test_result_is_historical_when_code_changes_and_environment_never_certified():
    value = observation(point(), point(), {'exit_code': 0, 'execution_state': 'finished'}, 'pytest', 1)
    assert value['status'] == 'passed'
    assert compare(value, point())['applies_to_current_code'] is True
    changed = compare(value, point('def'))
    assert changed['historical'] is True
    assert changed['applies_to_current_code'] is False
    assert changed['environment_verified_current'] is False


def test_changed_during_test_or_incomplete_scan_never_certifies_current_code():
    for before, after in ((point(), point('def')), (point(complete=False), point())):
        value = observation(before, after, {'exit_code': 0, 'execution_state': 'finished'}, 'pytest', 1)
        assert value['code_unchanged_during_test'] is False
        assert compare(value, after)['applies_to_current_code'] is None
    value = observation(point(), point(), {'execution_state': 'unknown', 'exit_code': 0}, 'pytest', 1)
    assert value['status'] == 'unknown'
    assert value['applies_to_current_code'] is None
    assert compare(value, point())['applies_to_current_code'] is None
    assert compare(value, point(complete=False))['applies_to_current_code'] is None


def test_fresh_matching_snapshot_revalidates_invalidated_evidence_without_a_new_test():
    value = observation(point(), point(), {'exit_code': 0, 'execution_state': 'finished'}, 'pytest', 1)
    stale = invalidate(value, 'exec_command')

    current = point()
    current['code']['scope'] = 'bounded project files'
    current['observed_at'] = 123.0
    same_code = compare(stale, current)
    assert same_code['status'] == 'passed'
    assert same_code['applies_to_current_code'] is True
    assert same_code['historical'] is False
    assert same_code['test_execution_is_historical'] is True
    assert same_code['invalidated_by'] == 'exec_command'
    assert same_code['invalidated'] is True
    assert same_code['applicability_revalidated_after_invalidation'] is True
    assert same_code['applicability_check_source'] == 'workspace_snapshot_fresh_bounded_fingerprint'
    assert same_code['applicability_checked_at'] == 123.0
    assert same_code['applicability_check_scope'] == 'bounded project files'
    assert same_code['environment_verified_current'] is False

    changed_code = compare(stale, point('def'))
    assert changed_code['applies_to_current_code'] is False
    assert changed_code['historical'] is True
    assert changed_code['applicability_revalidated_after_invalidation'] is False

    changed_config = compare(stale, point(config='other-env'))
    assert changed_config['applies_to_current_code'] is False

    incomplete = compare(stale, point(complete=False))
    assert incomplete['applies_to_current_code'] is None
    assert incomplete['applicability_revalidated_after_invalidation'] is False

    missing_fingerprint = point()
    missing_fingerprint.pop('config_digest')
    assert compare(value, missing_fingerprint)['applies_to_current_code'] is None

    known_code_change_missing_config = point('def')
    known_code_change_missing_config.pop('config_digest')
    assert compare(stale, known_code_change_missing_config)['applies_to_current_code'] is False


def test_error_timeout_and_unknown_execution_never_claim_passed_evidence():
    for result in (
        {'exit_code': 0, 'execution_state': 'unknown'},
        {'exit_code': 0, 'execution_state': 'finished', 'timed_out': True},
        {'exit_code': 0, 'execution_state': 'finished', 'error': 'response lost'},
    ):
        value = observation(point(), point(), result, 'pytest', 1)
        assert value['status'] == 'unknown'
        assert value['applies_to_current_code'] is None
        assert compare(value, point())['applies_to_current_code'] is None
