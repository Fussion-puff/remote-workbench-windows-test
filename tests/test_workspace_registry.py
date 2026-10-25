from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import uuid

import pytest

from remote_workbench.workspace_registry import Registry, RevisionConflict, WorkspaceTarget


def target(state, host="a", source="/source"):
    key = uuid.uuid4().hex
    return WorkspaceTarget(key, state.workspace_id, host, source, f"/copies/{key}/project", f"/copies/{key}/project")


def test_restart_recovers_identity_target_and_history(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    first = registry.get_or_create("thread-a")
    second = registry.get_or_create("thread-b")
    assert first.host is None and first.revision == 0
    assert first.workspace_id != second.workspace_id
    a = target(first)
    bound = registry.commit_switch(first.workspace_id, 0, a)
    other = target(bound, "b", "/other")
    changed = registry.commit_switch(first.workspace_id, 1, other)
    restarted = Registry(path)
    assert restarted.get_or_create("thread-a") == changed
    assert restarted.get_or_create("thread-b") == second
    assert restarted.find_target(first.workspace_id, "a", "/source") == a
    restored = restarted.commit_switch(first.workspace_id, 2, a)
    assert restored.cwd == bound.cwd and restored.revision == 3
    assert [v["revision"] for v in restarted.history(first.workspace_id)] == [3, 2, 1]
    assert restarted.commit_switch(first.workspace_id, 3, a) == restored
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_two_instances_do_not_silently_overwrite_revision(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    state = registry.get_or_create("thread")
    def commit(host):
        try:
            return Registry(path).commit_switch(state.workspace_id, 0, target(state, host))
        except RevisionConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, ["a", "b"]))
    assert sum(v is not None for v in results) == 1
    assert registry.get(state.workspace_id).revision == 1
    assert len(registry.history(state.workspace_id)) == 1


def test_clear_binding_is_atomic_and_preserves_targets_history_and_job_origins(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path)
    state = registry.get_or_create("thread")
    original = target(state)
    bound = registry.commit_switch(state.workspace_id, 0, original)
    registry.register_job(state.workspace_id, "origin-job", original)

    local = registry.clear_binding(state.workspace_id, expected_revision=bound.revision)
    assert local.host is None and local.source_cwd is None and local.cwd is None
    assert local.project_root is None and local.target_id is None
    assert local.revision == bound.revision + 1
    assert registry.find_target(state.workspace_id, original.host, original.source_cwd) == original
    history = registry.history(state.workspace_id)
    assert len(history) == 1
    assert history[0]["target_id"] == original.target_id
    assert history[0]["host"] == original.host and history[0]["cwd"] == original.cwd
    assert history[0]["revision"] == bound.revision
    assert registry.get_job(state.workspace_id, "origin-job") == original

    # Selecting local again is an idempotent no-op, but stale revisions fail.
    assert registry.clear_binding(state.workspace_id, local.revision) == local
    with pytest.raises(RevisionConflict):
        registry.clear_binding(state.workspace_id, bound.revision)
    assert registry.get(state.workspace_id) == local

    restored = registry.commit_switch(state.workspace_id, local.revision, original)
    assert restored.host == original.host and restored.cwd == original.cwd
    assert restored.revision == local.revision + 1
    assert registry.get_job(state.workspace_id, "origin-job") == original


def test_job_origin_cannot_move_or_cross_workspaces(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    state = registry.get_or_create("a")
    other = registry.get_or_create("b")
    a, b = target(state), target(state, "b")
    registry.commit_switch(state.workspace_id, 0, a)
    registry.register_job(state.workspace_id, "same-id", a)
    registry.commit_switch(state.workspace_id, 1, b)
    assert registry.get_job(state.workspace_id, "same-id") == a
    with pytest.raises(ValueError, match="different target"):
        registry.register_job(state.workspace_id, "same-id", b)
    with pytest.raises(ValueError, match="Unknown job"):
        registry.get_job(other.workspace_id, "same-id")
    registry.register_job(other.workspace_id, "same-id", target(other))
    assert registry.get_job(other.workspace_id, "same-id").workspace_id == other.workspace_id


def test_list_jobs_paginates_newest_first_without_crossing_workspace(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    first = registry.get_or_create("thread-a")
    second = registry.get_or_create("thread-b")
    origin = target(first)
    other_origin = target(second)
    for index in range(25):
        registry.register_job(first.workspace_id, f"job-{index:02d}", origin)
    registry.register_job(second.workspace_id, "other-job", other_origin)

    page_one = registry.list_jobs(first.workspace_id, limit=10, offset=0)
    page_two = registry.list_jobs(first.workspace_id, limit=10, offset=10)
    page_three = registry.list_jobs(first.workspace_id, limit=10, offset=20)
    assert [item["job_id"] for item in page_one] == [f"job-{index:02d}" for index in range(24, 14, -1)]
    assert [item["job_id"] for item in page_two] == [f"job-{index:02d}" for index in range(14, 4, -1)]
    assert [item["job_id"] for item in page_three] == [f"job-{index:02d}" for index in range(4, -1, -1)]
    assert len(registry.list_jobs(first.workspace_id)) == 25
    assert [item["job_id"] for item in registry.list_jobs(second.workspace_id, limit=10)] == ["other-job"]
    with pytest.raises(ValueError, match="offset"):
        registry.list_jobs(first.workspace_id, offset=-1)


def test_idempotent_job_operation_is_reserved_before_submission_and_workspace_scoped(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    state = registry.get_or_create("thread-a")
    other = registry.get_or_create("thread-b")
    origin = target(state)
    fingerprint = "f" * 64

    operation, created = registry.reserve_job_operation(
        state.workspace_id, "request-1", origin, "job-1", fingerprint)
    assert created and operation.state == "reserved"
    assert operation.target == origin
    assert registry.get_job(state.workspace_id, "job-1") == origin

    duplicate, created = registry.reserve_job_operation(
        state.workspace_id, "request-1", origin, "different-random-id", fingerprint)
    assert not created and duplicate.job_id == "job-1"
    assert not registry.reopen_rejected_job_operation(state.workspace_id, "request-1")

    submitted = registry.update_job_operation(state.workspace_id, "request-1", "submitted")
    assert submitted.state == "submitted"
    assert registry.get_job_operation(state.workspace_id, "request-1") == submitted
    assert [item["job_id"] for item in registry.list_jobs(state.workspace_id)] == ["job-1"]
    assert registry.list_jobs(other.workspace_id) == []

    with pytest.raises(ValueError, match="different job parameters"):
        registry.reserve_job_operation(
            state.workspace_id, "request-1", origin, "job-1", "e" * 64)
    with pytest.raises(ValueError, match="different job parameters"):
        registry.reserve_job_operation(
            state.workspace_id, "request-1", target(state, "b"), "job-1", fingerprint)
    with pytest.raises(ValueError, match="Unknown idempotency_key"):
        registry.get_job_operation(other.workspace_id, "request-1")


def test_only_confirmed_not_executed_job_request_can_reopen(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    state = registry.get_or_create("thread")
    origin = target(state)
    registry.reserve_job_operation(state.workspace_id, "request", origin, "job", "fingerprint")
    registry.update_job_operation(state.workspace_id, "request", "rejected")
    assert registry.reopen_rejected_job_operation(state.workspace_id, "request")
    assert registry.get_job_operation(state.workspace_id, "request").state == "reserved"
    assert not registry.reopen_rejected_job_operation(state.workspace_id, "request")


def test_workspace_note_merges_fields_and_is_thread_scoped(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    first = registry.get_or_create("thread-a")
    second = registry.get_or_create("thread-b")
    assert registry.workspace_note(first.workspace_id) is None
    initial = registry.workspace_note(first.workspace_id, {"goal": "ship a feature", "next_step": "run tests"})
    assert initial["goal"] == "ship a feature"
    updated = registry.workspace_note(first.workspace_id, {"next_step": "review diff"})
    assert updated["goal"] == "ship a feature"
    assert updated["next_step"] == "review diff"
    assert registry.workspace_note(second.workspace_id) is None
    with pytest.raises(ValueError, match="accept"):
        registry.workspace_note(first.workspace_id, {"private": "no"})


def test_saved_target_is_immutable_and_conflict_leaves_history_unchanged(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    state = registry.get_or_create("thread")
    original = target(state)
    bound = registry.commit_switch(state.workspace_id, 0, original)
    with pytest.raises(ValueError, match="immutable"):
        registry.commit_switch(state.workspace_id, 1, replace(original, cwd="/replacement"))
    with pytest.raises(RevisionConflict):
        registry.commit_switch(state.workspace_id, 0, target(state, "b"))
    assert registry.get(state.workspace_id) == bound
    assert len(registry.history(state.workspace_id)) == 1
