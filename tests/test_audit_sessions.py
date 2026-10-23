from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import os
import threading

import httpx

from remote_workbench import audit
from remote_workbench.audit_web import handler_for_directory


def test_base_root_resolves_configuration_without_creating_it(tmp_path, monkeypatch):
    configured = tmp_path / "configured-audit"
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(configured))

    assert audit.base_root() == configured
    assert not configured.exists()


def test_scoped_roots_do_not_change_environment_or_leak_across_calls(tmp_path, monkeypatch):
    base = tmp_path / "base"
    first = tmp_path / "sessions" / "one"
    second = tmp_path / "sessions" / "two"
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(base))

    with audit.scoped_root(first):
        first_id = audit.create(kind="command", command="first session")
        assert audit.root() == first
        assert (audit.directory(first_id) / "record.json").is_file()
    assert str(base) == os.environ["REMOTE_WORKBENCH_AUDIT_DIR"]

    with audit.scoped_root(second):
        second_id = audit.create(kind="command", command="second session")
        assert audit.root() == second
        assert (audit.directory(second_id) / "record.json").is_file()
        assert not (second / first_id).exists()

    assert (first / first_id / "record.json").is_file()
    assert (second / second_id / "record.json").is_file()
    assert not base.exists()
    assert audit.root() == base


def test_context_variable_keeps_concurrent_sessions_isolated(tmp_path, monkeypatch):
    base = tmp_path / "base"
    roots = [tmp_path / "sessions" / "alpha", tmp_path / "sessions" / "beta"]
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(base))
    entered = threading.Barrier(2)

    def record_in_session(path, command):
        with audit.scoped_root(path):
            entered.wait(timeout=5)
            record_id = audit.create(kind="command", command=command)
            return audit.root(), record_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(record_in_session, path, f"command-{index}")
                   for index, path in enumerate(roots)]
        results = [future.result(timeout=10) for future in futures]

    for (root, record_id), expected_root in zip(results, roots):
        assert root == expected_root
        assert (expected_root / record_id / "record.json").is_file()
        other_root = roots[1] if expected_root == roots[0] else roots[0]
        assert not (other_root / record_id).exists()
    assert audit.base_root() == base


def test_same_record_id_is_resolved_only_inside_the_active_root(tmp_path):
    roots = [tmp_path / "sessions" / "one", tmp_path / "sessions" / "two"]
    record_id = "a" * 32
    with audit.scoped_root(roots[0]):
        audit.directory(record_id).mkdir(parents=True)
        (audit.directory(record_id) / "record.json").write_text(json.dumps({"id": record_id}))

    with audit.scoped_root(roots[1]):
        assert audit.directory(record_id) == roots[1] / record_id
        assert not audit.directory(record_id).exists()


def test_viewer_handlers_are_bound_to_their_own_session_directory(tmp_path, monkeypatch):
    base = tmp_path / "unscoped"
    roots = [tmp_path / "sessions" / "one", tmp_path / "sessions" / "two"]
    monkeypatch.setenv("REMOTE_WORKBENCH_AUDIT_DIR", str(base))
    record_ids = []
    for index, path in enumerate(roots):
        with audit.scoped_root(path):
            record_id = audit.create(kind="command", command=f"private-{index}", status="finished", exit_code=0)
            with audit.open_stream(record_id, "stdout") as output:
                output.write(f"output-{index}".encode())
            record_ids.append(record_id)

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler_for_directory(path)) for path in roots]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            responses = [client.get(f"http://127.0.0.1:{server.server_port}/api/records")
                         for server in servers]
            rows = [response.json() for response in responses]
            assert all(response.status_code == 200 for response in responses)
            assert [row["id"] for result in rows for row in result] == record_ids

            for index, server in enumerate(servers):
                base_url = f"http://127.0.0.1:{server.server_port}"
                own = client.get(base_url + "/api/record", params={"id": record_ids[index]})
                foreign = client.get(base_url + "/api/record", params={"id": record_ids[1 - index]})
                output = client.get(base_url + "/api/output", params={"id": record_ids[index], "stream": "stdout"})
                assert own.status_code == 200
                assert foreign.status_code == 404
                assert output.json()["content"] == f"output-{index}"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
