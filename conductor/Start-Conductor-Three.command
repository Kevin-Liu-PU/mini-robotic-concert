#!/bin/zsh
# Three beats, gesture beats, sender only. Start the receiver first (it must be listening on 127.0.0.1:8766).
# Uses the interpreter in $PYTHON, or python3. Activate the project's virtual environment first.
set -eu
umask 077
here="${0:A:h}"
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
print 'Conductor sender only (gesture beats). The receiver must already be listening on 127.0.0.1:8766.'
print '3/4, tempo hint 70 BPM. No receiver and no robot control is started here.'
print 'The camera starts when this command runs, and the RAW camera picture is recorded to conductor/runs/ on this computer (for diagnosis only, never uploaded).'
print 'No Prepare gesture is needed: step into the picture and conduct. The piece starts after one or two complete bars. STOP button or Space stops; Q quits.'
print 'You have 10 minutes from now to begin. A piece that is playing is never cut by that limit.'
exec "${PYTHON:-python3}" -B "$here/launch_live_tcp.py" --camera 0 --initial-bpm 70 --meter 3 --conducting-hand right --startup count-in --port 8766 --wait-client 20 --ack-wait 3 --duration 600 --dashboard --preview --keep-dashboard --record-video --auto-prepare "$@"
