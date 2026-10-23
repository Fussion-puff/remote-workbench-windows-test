"""Quote remote paths without confusing leading characters with CLI options."""
import shlex


def shell_path(path: str) -> str:
    if not path:
        raise ValueError("path must not be empty")
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    if path.startswith(("-", "!", "(")):
        path = "./" + path
    return shlex.quote(path)
