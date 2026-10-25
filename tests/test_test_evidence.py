from remote_workbench.test_evidence import observation, compare


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
