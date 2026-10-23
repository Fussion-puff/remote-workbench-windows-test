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
