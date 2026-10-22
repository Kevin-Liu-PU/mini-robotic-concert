#!/bin/sh
# Screen-only counterpart of launch-responsive.cmd; built-in camera required.
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo 'Missing project .venv/bin/python; prepare the isolated conductor environment first.' >&2
    exit 1
fi
exec .venv/bin/python -B -m conductor.app \
    --config "$PWD/config-responsive.json" --hold-clock \
    --backend avfoundation --camera-built-in "$@"
