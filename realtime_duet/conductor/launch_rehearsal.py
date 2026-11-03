"""One-command mock rehearsal. Session/domain/port are shared automatically.

Use --video for validated replay. --camera is an explicit future camera entry,
not exercised during this delivery. No hardware or audio mode is provided.
"""
from integration_replay import main

if __name__=="__main__":
    raise SystemExit(main())
