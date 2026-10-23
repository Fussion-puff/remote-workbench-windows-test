"""Exercise the shipped viewer script with deterministic polling and HTTP races."""
from pathlib import Path
import shutil
import subprocess

import pytest

from remote_workbench.audit_web import HTML


@pytest.mark.parametrize("case", [
    "follow_latest", "initially_empty", "final_tail", "late_stdout", "late_stderr",
    "manual_history", "raw_output", "new_output_source", "restored_checkbox", "retry",
    "manual_refresh", "pinned_stream", "retry_record",
])
def test_live_refresh_behaviour(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for browser-script behaviour tests")
    script = HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(
        [node, str(Path(__file__).with_name("audit_viewer_refresh.cjs")), case],
        input=script, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
