"""Read-only remote log follower, sent over the existing mux; no workload launch."""
import base64


def follow_logs(settings):
    root = Path(settings["job_dir"])
    cursors = dict(settings["cursors"])
    prefix = settings["prefix"]

    def emit(value):
        print(prefix + json.dumps(value, separators=(",", ":")), flush=True)

    last_heartbeat = time.monotonic()
    last_status = None
    while True:
        ready, _, _ = select.select([sys.stdin.buffer], [], [], 0)
        if ready:
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                return
            last_heartbeat = time.monotonic()
        if time.monotonic() - last_heartbeat > 15:
            return
        try:
            status = json.loads((root / "status.json").read_text())
            if (root / "exit_code").exists():
                code = int((root / "exit_code").read_text())
                running, detail = False, {}
            else:
                code = None
                origin = json.loads((root / "origin.json").read_text())
                running, detail = job_liveness(origin)
            sizes = {}
            for stream in ("stdout", "stderr"):
                path = root / (stream + ".log")
                sizes[stream] = path.stat().st_size
                if sizes[stream] < cursors[stream]:
                    raise ValueError("remote log was truncated")
                with path.open("rb") as source:
                    source.seek(cursors[stream])
                    data = source.read(24576)
                if data:
                    emit(dict(type="data", stream=stream, offset=cursors[stream],
                              data=base64.b64encode(data).decode("ascii")))
                    cursors[stream] += len(data)
            complete = code is not None and cursors == sizes
            value = dict(type="status", running=running, exit_code=code,
                         execution_state="unknown" if status.get("cleanup_state") == "unconfirmed"
                         else "finished" if code is not None else "running" if running else "unknown",
                         remote_task=status, remote_bytes=sizes, complete=complete, **detail)
            if value != last_status:
                emit(value)
                last_status = value
            if complete or running is None:
                return
            if cursors == sizes:
                time.sleep(0.15)
        except (OSError, ValueError, TypeError) as exc:
            emit(dict(type="error", message=str(exc)))
            return
