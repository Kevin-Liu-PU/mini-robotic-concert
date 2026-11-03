#!/bin/zsh
set -eu
cd -- "${0:A:h}"
trial_python="$PWD/.venv/bin/python"
export MPLCONFIGDIR="$PWD/runs/matplotlib"
session="beat-$("$trial_python" -c 'import uuid; print(uuid.uuid4())')"
exec "$trial_python" -B conductor/launch_beat_test.py \
  --sender-python "$trial_python" --session "$session" --clock-domain "local-$session" \
  --output "$PWD/runs/$session" --camera 0 --width 640 \
  --initial-bpm 90 --meter 3 --conducting-hand right --startup count-in --interactive --experimental-auto-stop
