"""Launch a conductor beat-only trial or the legacy 4/4 software mock pair.

Child handles are owned by this script and cleaned up. Video mode opens no
camera; --camera explicitly selects a local camera trial. There is no robot,
firewall, process-enumeration or machine-setting operation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import queue
import threading
from conductor_rt.camera_setup import load_calibration,validate_calibration,camera_backend
from conductor_rt.status_view import TerminalStatus


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument("--video")
    source.add_argument("--camera",type=int,help="Explicitly select a local camera trial")
    p.add_argument("--output",type=Path)
    p.add_argument("--receiver-root",type=Path,default=Path(__file__).resolve().parents[1]/"cello")
    p.add_argument("--initial-bpm",required=True,type=float)
    p.add_argument("--beat-only",action="store_true",help="Conductor and beat display only: do not spawn cello or open TCP")
    p.add_argument("--meter",type=int,choices=(3,4),default=4)
    p.add_argument('--conducting-hand',choices=('left','right'),default='right')
    p.add_argument("--record-video",action="store_true",help="Authorized camera video, saved locally only; bounded duration required")
    p.add_argument('--beat-audio',action='store_true',help='Output-only synthesized clicks from published beat events')
    p.add_argument("--keep-dashboard",action="store_true",help="Keep final results until Quit or New test; camera is released")
    p.add_argument("--preview",action="store_true",help="RAM-only camera preview in dashboard via anonymous pipe")
    p.add_argument("--dashboard",action="store_true",help="Local GUI with Prepare, Stop, Quit; optional RAM preview and explicit recording")
    p.add_argument('--startup',choices=('explicit','count-in'),default='explicit')
    calibration=p.add_mutually_exclusive_group()
    calibration.add_argument("--chin-y",type=float,help="Optional fixed chin line for manual debugging; default is automatic body reference")
    calibration.add_argument('--calibration',type=Path,help='Numeric camera calibration JSON')
    p.add_argument('--width',type=int,default=640)
    p.add_argument("--stop-at",type=float)
    p.add_argument("--start-at",type=float)
    p.add_argument("--duration",type=float)
    p.add_argument("--experimental-auto-stop",action="store_true")
    p.add_argument("--session",default="rehearsal-"+uuid.uuid4().hex[:12])
    p.add_argument("--clock-domain")
    p.add_argument("--port",type=int,default=8767)
    default_python=sys.executable
    p.add_argument("--sender-python",default=default_python)
    p.add_argument("--receiver-python",default=default_python)
    p.add_argument("--journal",type=Path,default=Path(__file__).parent/"mock-journal",help="Durable cross-run session journal; never reset it to resume a session")
    p.add_argument("--interactive",action="store_true",help="Console Enter=prepare, S=start, Space=Stop, Q=quit; auto-enabled for future camera trial")
    p.add_argument("--timeout",type=float,default=120,help="Software video test teardown deadline; camera mode runs until Stop/quit")
    a=p.parse_args()
    if a.meter != 4 and not a.beat_only:
        p.error("3/4 requires --beat-only; the legacy mock receiver supports 4/4 only")
    if not 256<=a.width<=1920:p.error('processing width must be 256..1920')
    if a.calibration is not None:
        if a.camera is None:p.error('--calibration is camera-only; videos use --chin-y')
        try:
            calibration_data=load_calibration(a.calibration,a.camera,a.width)
            validate_calibration(calibration_data,a.camera,a.width,camera_backend()[0])
        except (ValueError,OSError) as exc:p.error(str(exc)+' No camera was opened.')
        a.chin_y=calibration_data['chin_y']
    if not 30<=a.initial_bpm<=180 or a.chin_y is not None and not 0<a.chin_y<1:
        p.error("invalid explicit BPM or chin calibration")
    if a.camera is not None and a.camera<0:
        p.error("camera index must be nonnegative")
    if a.camera is not None:
        a.interactive=True
    if a.output is None:
        a.output=Path(__file__).parent/"runs"/a.session
    if a.preview and (not a.dashboard or a.camera is None or os.name=="nt"):
        p.error("RAM preview requires a camera, dashboard, and POSIX inherited pipe")
    a.output=a.output.resolve()
    a.output.mkdir(parents=True,exist_ok=False)
    domain=a.clock_domain or "local-"+a.session
    # This launcher has no hardware option. --arm applies to the mock scheduler.
    receiver=[a.receiver_python,"-B","-m","cello_adapter","--session",a.session,"--clock-domain",domain,
              "--backend","mock","--listen",str(a.port),"--arm","--journal",str(a.journal.resolve()),"--log",str(a.output/"bow.jsonl")]
    sender=[a.sender_python,"-B","-m","conductor_rt","--initial-bpm",str(a.initial_bpm),
            "--width",str(a.width),"--session-id",a.session,"--clock-domain",domain,"--transport","tcp",
            "--tcp-port",str(a.port),"--wait-client","10","--ack-wait","3","--output",str(a.output/"conductor")]
    sender += ["--meter",str(a.meter),'--conducting-hand',a.conducting_hand]
    if a.calibration: sender += ["--calibration",str(a.calibration.resolve())]
    elif a.chin_y is not None: sender += ["--chin-y",str(a.chin_y)]
    sender += ["--video",a.video] if a.video else ["--camera-index",str(a.camera),"--enable-camera"]
    sender += ['--startup',a.startup]
    if a.interactive:
        sender += ["--stdin-controls"]
    if a.stop_at is not None:
        sender += ["--stop-at",str(a.stop_at)]
    if a.start_at is not None:
        sender += ["--start-at",str(a.start_at),"--no-auto-gestures"]
    if a.duration is not None:
        sender += ["--duration",str(a.duration)]
    if a.record_video:
        if a.camera is None or a.duration is None:p.error("recording needs a camera and an explicit duration")
        sender += ["--record-video"]
    if a.beat_audio:sender += ['--beat-audio']
    if a.experimental_auto_stop:
        sender += ["--experimental-auto-stop"]
    if a.beat_only:
        receiver = None
        sender += ['--continuous-test']
        sender[sender.index('--transport')+1]='file'
        index=sender.index('--wait-client');del sender[index:index+2]
    preview_read=preview_write=None
    if a.preview:
        preview_read,preview_write=os.pipe()
        sender += ["--preview-fd",str(preview_write)]
    commands=dict(receiver=receiver,sender=sender,hardware_enabled=False,cello_enabled=not a.beat_only,camera_enabled=a.camera is not None,
                  session_id=a.session,clock_domain=domain,initial_bpm=a.initial_bpm,conducting_hand=a.conducting_hand,
                  port=a.port,dry_run=False,software_only=True)
    (a.output/"commands.json").write_text(json.dumps(commands,indent=2),encoding="utf-8")
    print(json.dumps({k:commands[k] for k in ("session_id","clock_domain","port","initial_bpm","dry_run","camera_enabled")}),flush=True)
    flags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
    rec=snd=None
    status_view=None
    dashboard=None
    result={}
    restart_requested=False
    dashboard_quit=False
    try:
        with (a.output/"receiver-console.log").open("w",encoding="utf-8") as rlog, (a.output/"sender-console.log").open("w",encoding="utf-8") as slog:
            if not a.beat_only:
                rec=subprocess.Popen(receiver,cwd=a.receiver_root,stdout=rlog,stderr=subprocess.STDOUT,creationflags=flags)
                time.sleep(.3)
            snd=subprocess.Popen(sender,cwd=Path(__file__).parent,stdin=subprocess.PIPE if a.interactive else None,
                                 stdout=slog,stderr=subprocess.STDOUT,creationflags=flags,text=True,
                                 **({"pass_fds":(preview_write,)} if preview_write is not None else {}))
            if preview_write is not None:
                os.close(preview_write);preview_write=None
            deadline=time.perf_counter()+a.timeout if a.video else None
            console_lines=queue.Queue(maxsize=8)
            def operator_command(command):
                nonlocal restart_requested,dashboard_quit
                if command=='restart':
                    restart_requested=True;command='quit'
                elif command=='quit':dashboard_quit=True
                if command in ('stop','quit'):
                    while not console_lines.empty():
                        try:console_lines.get_nowait()
                        except queue.Empty:break
                try:console_lines.put_nowait(command)
                except queue.Full:pass
            if a.dashboard:
                from conductor_rt.dashboard import Dashboard
                dashboard=Dashboard(a.output,operator_command,a.experimental_auto_stop,beat_only=a.beat_only,preview_fd=preview_read,allow_restart=a.keep_dashboard)
                preview_read=None
            if a.interactive:
                status_view=TerminalStatus(a.output/'conductor'/'stream.jsonl',a.startup)
                status_view.start()
                if os.name=='nt':
                    print("MOCK controls: Enter=prepare, S=start, Space=Stop, Q/Esc=quit. Automatic Stop is OFF unless explicitly selected.",flush=True)
                else:
                    print("MOCK controls: type prepare, start, stop, or quit then Enter. Automatic Stop is " +
                          ("ON UNDER TEST" if a.experimental_auto_stop else "OFF") + ". Terminal line input requires Enter.",flush=True)
                    def console_reader():
                        for line in sys.stdin:
                            command=line.strip().lower()
                            if command in ('prepare','start','stop','quit','record-stop'):
                                try:console_lines.put_nowait(command)
                                except queue.Full:
                                    if command in ('stop','quit'):
                                        while not console_lines.empty():
                                            try:console_lines.get_nowait()
                                            except queue.Empty:break
                                        console_lines.put_nowait(command)
                    threading.Thread(target=console_reader,daemon=True,name='operator-console').start()
            while snd.poll() is None:
                if dashboard is not None:dashboard.update(time.perf_counter())
                if deadline is not None and time.perf_counter()>deadline:
                    raise subprocess.TimeoutExpired(sender,a.timeout)
                if a.interactive and os.name=="nt":
                    import msvcrt
                    while msvcrt.kbhit():
                        key=msvcrt.getwch()
                        command={"\r":"prepare","s":"start","S":"start"," ":"stop","q":"quit","Q":"quit","\x1b":"quit"}.get(key)
                        if command:
                            try:
                                snd.stdin.write(command+"\n")
                                snd.stdin.flush()
                                print("operator:",command,flush=True)
                            except (BrokenPipeError,OSError):
                                pass
                elif a.interactive:
                    while not console_lines.empty():
                        try:command=console_lines.get_nowait()
                        except queue.Empty:break
                        try:
                            snd.stdin.write(command+'\n');snd.stdin.flush()
                            print('operator:',command,flush=True)
                        except (BrokenPipeError,OSError):pass
                time.sleep(.01)
            result["sender_exit"]=snd.returncode
            if status_view is not None:status_view.show_once()
            try:
                result["receiver_exit"]=rec.wait(timeout=5) if rec is not None else None
            except subprocess.TimeoutExpired:
                result["receiver_exit"]="still_waiting_no_connection"
            if a.keep_dashboard and dashboard is not None:
                if status_view is not None:status_view.close();status_view=None
                dashboard.finish(result)
                (a.output/'integration_result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                while not restart_requested and not dashboard_quit:
                    dashboard.update(time.perf_counter())
                    time.sleep(.02)
    except KeyboardInterrupt:
        result["operator_interrupt"]=True
        if snd is not None and snd.stdin is not None and snd.poll() is None:
            try:
                snd.stdin.write("quit\n")
                snd.stdin.flush()
                snd.wait(timeout=3)
            except (BrokenPipeError,OSError,subprocess.TimeoutExpired):
                pass
    finally:
        if dashboard is not None:dashboard.close()
        for preview_fd in (preview_read,preview_write):
            if preview_fd is not None:os.close(preview_fd)
        if status_view is not None:status_view.close()
        for label,process in (("sender",snd),("receiver",rec)):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
                result[label+"_owned_child_cleanup"]=True
        (a.output/"integration_result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result),flush=True)
    if restart_requested:return 75
    return 0 if result.get("sender_exit")==0 and (a.beat_only or result.get("receiver_exit")==0) else 1


if __name__=="__main__":
    raise SystemExit(main())
