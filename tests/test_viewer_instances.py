from http.server import ThreadingHTTPServer
import errno
import json
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

from remote_workbench import audit
from remote_workbench import audit_web
from remote_workbench.audit_web import handler_for_directory, handler_for_instance, handler_for_workspaces


@pytest.fixture
def viewer_server():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start

    for server, _ in servers:
        server.shutdown()
        server.server_close()
    for _, thread in servers:
        thread.join()


def test_viewer_instance_prefix_is_required_and_bound_to_its_identity(tmp_path, viewer_server):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    with audit.scoped_root(first_root):
        first_record = audit.create(kind="tool", command="first project", status="finished")
    with audit.scoped_root(second_root):
        second_record = audit.create(kind="tool", command="second project", status="finished")

    first_id = "a" * 32
    second_id = "b" * 32
    first_base = viewer_server(handler_for_instance(handler_for_directory(first_root), first_id))
    second_base = viewer_server(handler_for_instance(handler_for_directory(second_root), second_id))

    with httpx.Client(trust_env=False) as client:
        first_page = client.get(f"{first_base}/v/{first_id}/")
        assert first_page.status_code == 200
        assert first_page.headers["X-Remote-Workbench-Viewer"] == first_id
        assert first_page.headers["Cache-Control"] == "no-store"
        assert f'const VIEWER_ID = "{first_id}";' in first_page.text

        first_rows = client.get(f"{first_base}/v/{first_id}/api/records")
        assert first_rows.status_code == 200
        assert first_rows.headers["X-Remote-Workbench-Viewer"] == first_id
        assert {row["id"] for row in first_rows.json()} == {first_record}

        second_rows = client.get(f"{second_base}/v/{second_id}/api/records")
        assert {row["id"] for row in second_rows.json()} == {second_record}

        # A browser page left open from the first server cannot follow its old
        # token into a second server, even if the operating system reuses the port.
        stale_page = client.get(f"{second_base}/v/{first_id}/")
        stale_api = client.get(f"{second_base}/v/{first_id}/api/records")
        assert stale_page.status_code == stale_api.status_code == 404
        assert stale_page.headers["X-Remote-Workbench-Viewer"] == second_id
        assert stale_api.headers["X-Remote-Workbench-Viewer"] == second_id
        assert "second project" not in stale_page.text
        assert "second project" not in stale_api.text

        assert client.get(f"{second_base}/").status_code == 404
        assert client.get(f"{second_base}/api/records").status_code == 404
        assert client.get(f"{second_base}/v/{second_id}suffix/").status_code == 404


@pytest.mark.parametrize("instance_id", ["", "A" * 32, "a" * 31, "../" + "a" * 29])
def test_instance_handler_rejects_invalid_identity(instance_id):
    with pytest.raises(ValueError, match="viewer instance ID"):
        handler_for_instance(handler_for_directory("/tmp/audit"), instance_id)


def test_instance_and_workspace_scopes_preserve_separate_records(tmp_path, viewer_server):
    viewer_id = "e" * 32
    first_id, second_id = "a" * 32, "b" * 32
    with audit.scoped_root(tmp_path / first_id):
        first_record = audit.create(kind="tool", command="first workspace", status="finished")
    with audit.scoped_root(tmp_path / second_id):
        second_record = audit.create(kind="tool", command="second workspace", status="finished")
    base = viewer_server(handler_for_instance(handler_for_workspaces(tmp_path), viewer_id))
    with httpx.Client(trust_env=False) as client:
        first_url = f"{base}/v/{viewer_id}/w/{first_id}"
        second_url = f"{base}/v/{viewer_id}/w/{second_id}"
        assert client.get(first_url + "/").status_code == 200
        assert {row["id"] for row in client.get(first_url + "/api/records").json()} == {first_record}
        assert {row["id"] for row in client.get(second_url + "/api/records").json()} == {second_record}
        assert client.get(first_url + f"/api/record?id={second_record}").status_code == 404
        assert client.get(f"{base}/v/{viewer_id}/api/records").status_code == 404
        assert client.get(f"{base}/w/{first_id}/api/records").status_code == 404


def test_standalone_viewer_uses_ephemeral_port_by_default(capsys):
    server = MagicMock(server_port=43210)
    server.serve_forever.side_effect = KeyboardInterrupt
    with patch("sys.argv", ["remote-workbench-audit"]), \
         patch.object(audit_web, "ThreadingHTTPServer", return_value=server) as factory:
        audit_web.main()
    assert factory.call_args.args[0] == ("127.0.0.1", 0)
    identity = factory.call_args.args[1].viewer_instance_id
    assert f"http://127.0.0.1:43210/v/{identity}/" in capsys.readouterr().out
    server.server_close.assert_called_once_with()


@pytest.mark.parametrize("bind_error", [errno.EADDRINUSE, errno.EACCES])
def test_standalone_viewer_only_falls_back_for_a_port_conflict(bind_error):
    server = MagicMock(server_port=43210)
    server.serve_forever.side_effect = KeyboardInterrupt
    failure = OSError(bind_error, "bind failed")
    with patch("sys.argv", ["remote-workbench-audit", "--port", "8768"]), \
         patch.object(audit_web, "ThreadingHTTPServer", side_effect=[failure, server]) as factory:
        if bind_error == errno.EACCES:
            with pytest.raises(OSError) as caught:
                audit_web.main()
            assert caught.value is failure
            assert factory.call_count == 1
        else:
            audit_web.main()
            assert [call.args[0] for call in factory.call_args_list] == [("127.0.0.1", 8768), ("127.0.0.1", 0)]
            assert factory.call_args_list[0].args[1] is factory.call_args_list[1].args[1]

