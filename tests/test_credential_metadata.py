import os

import pytest

from remote_workbench.credential_metadata import inspect_password_path


def test_optional_password_path_reports_unset_without_filesystem_access(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("unset path must not touch the filesystem")

    monkeypatch.setattr(os, "lstat", unexpected)
    monkeypatch.setattr(os, "access", unexpected)
    result = inspect_password_path(None)

    assert result["configured"] is False
    assert result["exists"] is False
    assert result["nofollow_open"] == "not_configured"
    assert result["file_permission_checks_passed"] is False
    assert result["same_uid_isolation"] is False


def test_password_metadata_never_reads_or_reports_content_size_or_hash(tmp_path, monkeypatch):
    candidate = tmp_path / "fixture-password"
    candidate.write_text("fixture secret; not a real credential", encoding="utf-8")
    candidate.chmod(0o600)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("metadata checks must never read bytes")

    monkeypatch.setattr(os, "read", forbidden_read)
    result = inspect_password_path(candidate)

    assert result["configured"] is True
    assert result["exists"] is True
    assert result["is_symlink"] is False
    assert result["is_regular_file"] is True
    assert result["owner_uid"] == os.getuid()
    assert result["mode"] == "0600"
    assert result["owner_only"] is True
    assert result["readable"] is True
    assert result["nofollow_open"] == "opened"
    assert result["file_permission_checks_passed"] is True
    assert result["same_uid_isolation"] is False
    assert "same UID" in result["note"]
    assert "fixture secret" not in repr(result)
    assert not {"size", "st_size", "bytes", "sha256", "hash"}.intersection(result)


def test_password_metadata_rejects_symlink_and_group_access(tmp_path):
    target = tmp_path / "target"
    target.write_text("fixture only", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "password-link"
    link.symlink_to(target)

    symlink_result = inspect_password_path(link)
    assert symlink_result["is_symlink"] is True
    assert symlink_result["nofollow_open"] != "opened"
    assert symlink_result["file_permission_checks_passed"] is False

    target.chmod(0o640)
    permissive_result = inspect_password_path(target)
    assert permissive_result["owner_only"] is False
    assert permissive_result["file_permission_checks_passed"] is False


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_nofollow_open_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("fixture only", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)

    result = inspect_password_path(link)
    assert result["nofollow_open"] == "ELOOP"
    assert result["file_permission_checks_passed"] is False
