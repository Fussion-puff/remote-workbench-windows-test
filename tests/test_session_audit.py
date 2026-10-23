from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import threading

import httpx

from remote_workbench import audit
from remote_workbench.audit_web import handler_for_workspaces


def test_record_updates_and_child_links_are_atomic(tmp_path):
    with audit.scoped_root(tmp_path):
        parent = audit.create(kind="tool")
    def update(index):
        with audit.scoped_root(tmp_path), audit.scoped_metadata(thread_id="a", workspace_id="a" * 32, revision=2):
            child = audit.create(kind="command")
            audit.add_child(parent, child)
            audit.update(parent, **{f"field_{index}": index})
            return child
    with ThreadPoolExecutor(max_workers=8) as pool:
        children = list(pool.map(update, range(40)))
    value = json.loads((tmp_path / parent / "record.json").read_text())
    assert set(value["child_commands"]) == set(children)
    assert all(value[f"field_{i}"] == i for i in range(40))
    child = json.loads((tmp_path / children[0] / "record.json").read_text())
    assert child["thread_id"] == "a" and child["revision"] == 2
    assert not list(tmp_path.glob("*/.record-*.tmp"))


def test_shared_viewer_requires_explicit_workspace_and_isolates_records(tmp_path):
    roots = [tmp_path / (name * 32) for name in ("a", "b")]
    ids = []
    for root in roots:
        with audit.scoped_root(root):
            ids.append(audit.create(kind="tool", status="finished"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_workspaces(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.get(base + "/api/records").status_code == 404
            prefix = base + "/w/" + roots[0].name
            page = client.get(prefix + "/")
            assert page.status_code == 200
            assert "workspacePrefix" in page.text
            assert [v["id"] for v in client.get(prefix + "/api/records").json()] == [ids[0]]
            assert client.get(prefix + "/api/record", params={"id": ids[0]}).status_code == 200
            assert client.get(prefix + "/api/record", params={"id": ids[1]}).status_code == 404
            assert client.get(base + "/w/" + "c" * 32 + "/").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
