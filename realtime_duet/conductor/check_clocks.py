"""Read-only same-host monotonic-clock bracket check for two interpreters."""
import argparse
import json
import subprocess
import sys
import time

def main():
    p=argparse.ArgumentParser();p.add_argument('--other-python',required=True);a=p.parse_args()
    samples=[]
    for _ in range(3):
        before=time.perf_counter_ns()
        proc=subprocess.run([a.other_python,'-B','-c','import time; print(time.perf_counter_ns())'],capture_output=True,text=True,timeout=10)
        after=time.perf_counter_ns()
        other=int(proc.stdout.strip()) if proc.returncode==0 else None
        samples.append(dict(parent_before_ns=before,child_ns=other,parent_after_ns=after,
                            bracket_width_ms=(after-before)/1e6,compatible=other is not None and before<=other<=after))
    okay=all(x['compatible'] for x in samples)
    print(json.dumps(dict(parent_python=sys.executable,other_python=a.other_python,samples=samples,shared_clock_bracket_ok=okay,
        claim='Same-machine clock-domain compatibility bracket, NOT a precise offset or real-time latency measurement.',hardware_calls=0,camera_opened=False),indent=2))
    return 0 if okay else 1

if __name__=='__main__':raise SystemExit(main())
