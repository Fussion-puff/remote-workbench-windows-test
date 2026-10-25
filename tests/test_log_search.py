import json

import pytest

from remote_workbench import audit
from remote_workbench.log_search import search_saved_output


def make_output(root, content, stream="stdout"):
    with audit.scoped_root(root):
        output_id = audit.create(kind="command", status="finished")
        with audit.open_stream(output_id, stream) as output:
            output.write(content)
    return output_id


def search(root, *args, **kwargs):
    with audit.scoped_root(root):
        return search_saved_output(*args, **kwargs)


def test_search_returns_byte_offsets_context_and_resumable_cursor(tmp_path):
    data = "前文 needle 中间 needle 后文".encode()
    output_id = make_output(tmp_path, data)

    first = search(tmp_path, output_id, "needle", cursor=0, max_scan_bytes=16,
                   context_bytes=5, max_matches=10)
    assert first["matches"][0]["offset"] == data.index(b"needle")
    assert "needle" in first["matches"][0]["context"]
    assert first["next_cursor"] == 16
    assert first["truncated"] is True

    second = search(tmp_path, output_id, "needle", cursor=first["next_cursor"],
                    max_scan_bytes=64, context_bytes=5)
    assert [item["offset"] for item in first["matches"] + second["matches"]] == [
        data.index(b"needle"), data.rindex(b"needle")]
    assert second["next_cursor"] == len(data)
    assert second["truncated"] is False


def test_search_paginates_overlapping_matches_without_skipping(tmp_path):
    output_id = make_output(tmp_path, b"ababa")
    first = search(tmp_path, output_id, "aba", max_scan_bytes=5,
                   max_matches=1, context_bytes=0)
    assert [item["offset"] for item in first["matches"]] == [0]
    assert first["next_cursor"] == 2
    assert first["truncated"] is True

    second = search(tmp_path, output_id, "aba", cursor=first["next_cursor"],
                    max_scan_bytes=5, max_matches=1, context_bytes=0)
    assert [item["offset"] for item in second["matches"]] == [2]
    assert second["truncated"] is False


def test_search_response_stays_within_byte_budget_for_control_characters(tmp_path):
    output_id = make_output(tmp_path, (b"\x00" + b"x" * 1024) * 20)
    result = search(tmp_path, output_id, "\x00", max_scan_bytes=65536,
                    context_bytes=512, max_matches=100, max_bytes=2048)
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 2048
    assert result["matches"]
    assert result["truncated"] is True
    assert result["next_cursor"] < result["total_bytes"]


@pytest.mark.parametrize("kwargs", [
    {"query": ""},
    {"query": "x", "cursor": -1},
    {"query": "x", "max_scan_bytes": 0},
    {"query": "x", "context_bytes": 513},
    {"query": "x", "max_matches": 0},
    {"query": "x", "max_bytes": 512},
])
def test_search_rejects_invalid_limits(tmp_path, kwargs):
    output_id = make_output(tmp_path, b"x")
    query = kwargs["query"]
    limits = {key: value for key, value in kwargs.items() if key != "query"}
    with pytest.raises(ValueError):
        search(tmp_path, output_id, query, **limits)


def test_search_validates_stream_and_record_id(tmp_path):
    output_id = make_output(tmp_path, b"x")
    with pytest.raises(ValueError, match="Unknown stream"):
        search(tmp_path, output_id, "x", stream="../record.json")
    with pytest.raises(ValueError, match="Invalid operation/output ID"):
        search(tmp_path, "../other", "x")
