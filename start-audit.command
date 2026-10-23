#!/bin/sh
set -eu
REMOTE_WORKBENCH_COPY_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$REMOTE_WORKBENCH_COPY_ROOT/.venv/bin/remote-workbench-audit" "$@"
