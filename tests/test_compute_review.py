import pytest

from remote_workbench.compute_review import compute_risks, check_compute_review
from remote_workbench.tools import commands


@pytest.mark.parametrize("command", [
    "make -j32", "make -j", "ninja", "cmake --build build --parallel 16", "ctest -j8",
    "pytest -n auto", "mpirun -np 16 ./fit", "srun ./fit", "sbatch script.sh",
    "find . -type f | xargs -P 32 worker", "LD_PRELOAD=hook.so ./probe",
    "python3 -c 'from concurrent.futures import ProcessPoolExecutor; ProcessPoolExecutor(max_workers=32)'",
    "cd source\nmake -j64", "python3 -m pytest -n16",
])
def test_parallel_and_wrapper_workloads_require_review(command):
    assert compute_risks(command)


@pytest.mark.parametrize("command", [
    "pwd", "git diff --stat", "pytest -q tests/test_parser.py", "make -j2",
    "echo 'make -j32'", "printf '%s' mpirun", "rg LD_PRELOAD .", "mpirun --help", "make -n", "ninja -n",
])
def test_ordinary_work_is_direct(command):
    assert compute_risks(command) == []


async def test_review_refusal_does_not_open_ssh(local, monkeypatch):
    _, root = local
    async def forbidden(*args, **kwargs):
        raise AssertionError("no remote execution before review")
    monkeypatch.setattr(commands, "require_connection", forbidden)
    for function in (commands.ssh_run, commands.ssh_run_background):
        value = await function("make -j32")
        assert value["error"] == "compute_review_required" and value["executed"] is False
        assert value["command"] == "make -j32" and value["cwd"] == str(root)


def test_confirmation_records_plan_without_imposing_resource_limit():
    review = {"concurrency": 32, "environment": "compute node; explicit environment activation",
              "confirmation": "User confirmed this command, concurrency and environment in chat"}
    assert check_compute_review("make -j32", "/project", "host", review) is None
    with pytest.raises(ValueError):
        check_compute_review("make -j32", "/project", "host", {"approved": True})
