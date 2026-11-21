"""Launch ONLY the Conductor sender (this folder's conductor_rt) against an external TCP receiver.

The runtime is the conductor_rt package next to this file. --beats selects the beat chain:
gesture (beats from the wrist gesture, the default) or legacy (the earlier detector, count-in
and clock).

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
import signal
import threading
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE))
from conductor_rt.camera_setup import load_calibration,validate_calibration,camera_backend
from conductor_rt.status_view import TerminalStatus,session_failure



def process_event(output, event, **fields):
    """Keep small lifecycle evidence; log I/O errors must not abort cleanup.

    A missing terminal event means the outcome is unknown, not a gesture Stop.
    SIGKILL/power loss cannot run Python cleanup or faulthandler.
    """
    record=dict(event=event,monotonic_s=time.perf_counter(),supervisor_pid=os.getpid(),**fields)
    try:
        with (output/'process-lifecycle.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(record,allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except OSError:
        return False


def camera_check_command(index):
    """One line for the operator's own terminal. It opens the camera once from the main thread, which is
    where macOS can show its permission question (the sender opens the camera in a worker thread, and from
    there it cannot). Nothing is recorded or saved."""
    code=("import cv2; c=cv2.VideoCapture(%d); ok=c.isOpened(); c.release(); "
          "ok or input('If macOS asks about the camera, click Allow. Then press Enter here: '); "
          "c=cv2.VideoCapture(%d); print('camera opened:', c.isOpened()); c.release()")%(index,index)
    return '"%s" -B -c "%s"'%(sys.executable,code)


def exit_evidence(returncode):
    if not isinstance(returncode,int):return dict(outcome='unknown',returncode=returncode)
    if returncode<0:
        try:name=signal.Signals(-returncode).name
        except ValueError:name='unknown_signal'
        return dict(outcome='signal',returncode=returncode,signal=name)
    return dict(outcome='clean_exit' if returncode==0 else 'error_exit',returncode=returncode)


def stop_owned_process(process, graceful_s=2.0, terminate_s=2.0, kill_s=2.0):
    """Bounded cleanup of an exact child handle; never enumerate other apps."""
    if process is None:
        return dict(exited=True, stage='not_started')
    stage = 'already_exited'
    if process.poll() is None:
        stage = 'graceful'
        if process.stdin is not None:
            try:
                process.stdin.write('quit\n')
                process.stdin.flush()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=graceful_s)
        except subprocess.TimeoutExpired:
            stage = 'terminate'
            process.terminate()
            try:
                process.wait(timeout=terminate_s)
            except subprocess.TimeoutExpired:
                stage = 'kill'
                process.kill()
                try:
                    process.wait(timeout=kill_s)
                except subprocess.TimeoutExpired:
                    pass
    return dict(exited=process.poll() is not None, returncode=process.returncode, stage=stage)


def interrupt_on_term(signum, frame):
    raise KeyboardInterrupt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument("--video")
    source.add_argument("--camera",type=int,help="Explicitly select a local camera trial")
    p.add_argument("--output",type=Path)
    p.add_argument("--initial-bpm",required=True,type=float)
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
    p.add_argument("--port",type=int,default=8766)
    p.add_argument("--wait-client",type=float,default=20)
    p.add_argument("--ack-wait",type=float,default=3)
    p.add_argument("--dry-run",action="store_true")
    p.add_argument("--beats",choices=("gesture","legacy"),default="gesture")
    p.add_argument("--auto-prepare",action="store_true",help="gesture beats: no Prepare gesture or button needed")
    default_python=sys.executable
    p.add_argument("--sender-python",default=default_python)
    p.add_argument("--interactive",action="store_true",help="Console Enter=prepare, S=start, Space=Stop, Q=quit; auto-enabled for future camera trial")
    p.add_argument("--timeout",type=float,default=120,help="Software video test teardown deadline; camera mode runs until Stop/quit")
    a=p.parse_args()
    a.beat_only=False
    if a.wait_client<15 or a.ack_wait<=0:p.error("wait-client must be >=15 and ack-wait positive")
    if a.experimental_auto_stop:p.error("Gesture Stop stays OFF for this demo; use STOP/Space")
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
    domain=a.clock_domain or "local-"+a.session
    # This launcher never imports or starts any cello/robot receiver.
    receiver=None
    sender=[a.sender_python,"-B","-X","faulthandler","-m","conductor_rt","--initial-bpm",str(a.initial_bpm),
            "--width",str(a.width),"--session-id",a.session,"--clock-domain",domain,"--transport","tcp",
            "--tcp-port",str(a.port),"--wait-client",str(a.wait_client),"--ack-wait",str(a.ack_wait),"--tracking-timeout","0","--output",str(a.output/"conductor")]
    sender += ["--meter",str(a.meter),'--conducting-hand',a.conducting_hand,"--beats",a.beats]
    if a.auto_prepare:
        if a.beats!="gesture":p.error("--auto-prepare needs --beats gesture")
        sender += ["--auto-prepare"]
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
    if a.dry_run:
        print(json.dumps(dict(sender=sender,cwd=str(BASE),receiver=None,camera_opened=False,tcp_opened=False),indent=2))
        return 0
    a.output.mkdir(parents=True,exist_ok=False)
    preview_read=preview_write=None
    if a.preview:
        preview_read,preview_write=os.pipe()
        sender += ["--preview-fd",str(preview_write)]
    commands=dict(receiver=receiver,sender=sender,hardware_enabled=False,cello_enabled=False,external_receiver=True,camera_enabled=a.camera is not None,
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
    quit_deadline=None
    old_term = signal.signal(signal.SIGTERM, interrupt_on_term)
    old_int = signal.getsignal(signal.SIGINT)
    process_event(a.output,'supervisor_started',session_id=a.session)
    try:
        with (a.output/"receiver-console.log").open("w",encoding="utf-8") as rlog, (a.output/"sender-console.log").open("w",encoding="utf-8") as slog:
            snd=subprocess.Popen(sender,cwd=BASE,stdin=subprocess.PIPE if a.interactive else None,
                                 stdout=slog,stderr=subprocess.STDOUT,creationflags=flags,text=True,
                                 **({"pass_fds":(preview_write,)} if preview_write is not None else {}))
            process_event(a.output,'sender_started',pid=getattr(snd,'pid',None))
            if preview_write is not None:
                os.close(preview_write);preview_write=None
            deadline=time.perf_counter()+a.timeout if a.video else None
            console_lines=queue.Queue(maxsize=8)
            def operator_command(command):
                nonlocal restart_requested,dashboard_quit,quit_deadline
                process_event(a.output,'operator_command',command=command)
                if command=='restart':
                    restart_requested=True;command='quit'
                elif command=='quit':dashboard_quit=True
                if command == 'quit' and quit_deadline is None:
                    quit_deadline = time.perf_counter() + 2.0
                if command in ('stop','quit'):
                    while not console_lines.empty():
                        try:console_lines.get_nowait()
                        except queue.Empty:break
                try:console_lines.put_nowait(command)
                except queue.Full:pass
            if a.dashboard:
                from conductor_rt.dashboard import Dashboard
                dashboard=Dashboard(a.output,operator_command,a.experimental_auto_stop,beat_only=a.beat_only,preview_fd=preview_read,allow_restart=False,recording_required=a.record_video,initial_meter=a.meter,initial_bpm=a.initial_bpm,rolling_count_in=a.beats=='gesture')
                preview_read=None
                dashboard.root.title('Conductor · '+('gesture beats' if a.beats=='gesture' else 'legacy beats')+' · LIVE TCP '+str(a.port)+' · external receiver')
                dashboard.root.tk_setPalette(background='#f4f5f7',foreground='#18222e',activeBackground='#dfe5ef',activeForeground='#18222e',highlightBackground='#f4f5f7')
                import tkinter as tk
                def relabel(widget):
                    if isinstance(widget,tk.Label):
                        value=widget.cget('text')
                        if value=='CAMERA → AUTO REFERENCE → CELLO MOCK':
                            widget.configure(text='CAMERA → CONDUCTOR ('+a.beats+' beats) → EXTERNAL TCP RECEIVER')
                        elif value=='(change with New test)':widget.configure(text='(fixed for this run)')
                        elif value.startswith('After L / Prepare you may listen to the beat first'):
                            widget.configure(text='Losing tracking does not stop the run: it waits during the count-in and keeps the last tempo during the piece. STOP / Space stops.')
                    for child in widget.winfo_children():relabel(child)
                relabel(dashboard.root)
                tk.Label(dashboard.root,text=f'EXTERNAL TCP 127.0.0.1:{a.port} | {a.meter}/4 | Initial {a.initial_bpm:g} BPM | No receiver started here',fg='darkblue').pack()
                if a.startup=='explicit':
                    tk.Button(dashboard.root,text='Manual START (S)',command=lambda:operator_command('start'),width=24).pack(pady=4)
                    dashboard.root.bind('<Key-s>',lambda e:operator_command('start'))
                else:
                    tk.Label(dashboard.root,text=(f'START: Prepare, then just conduct. The first bar that shows beat 1 and the lift counts; the piece starts on the next downbeat.' if a.beats=='gesture' else f'START: Prepare, conduct {a.meter} beats, then predicted next downbeat. Manual Start is not used.')).pack()
            # GUI sessions use their own current-session callbacks. Do not leave
            # stdin reader threads behind when New test re-enters this function.
            if a.interactive and not a.dashboard:
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
                            if command in ('q', 'exit'):
                                command = 'quit'
                            if command in ('prepare','start','stop','quit','record-stop'):
                                operator_command(command)
                        # A controlled pipe closing means the supervisor went
                        # away. Never leave the camera sender behind.
                        if not sys.stdin.isatty():
                            operator_command('quit')
                    threading.Thread(target=console_reader,daemon=True,name='operator-console').start()
            while snd.poll() is None:
                if dashboard is not None:dashboard.update(time.perf_counter())
                if quit_deadline is not None and time.perf_counter() >= quit_deadline:
                    result['quit_grace_expired'] = True
                    break
                if deadline is not None and time.perf_counter()>deadline:
                    raise subprocess.TimeoutExpired(sender,a.timeout)
                if a.interactive and os.name=="nt":
                    import msvcrt
                    while msvcrt.kbhit():
                        key=msvcrt.getwch()
                        command={"\r":"prepare","s":"start","S":"start"," ":"stop","q":"quit","Q":"quit","\x1b":"quit"}.get(key)
                        if command:
                            if command == 'quit':
                                operator_command(command)
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
            process_event(a.output,'sender_exited',**exit_evidence(snd.returncode))
            failure=session_failure(a.output,snd.returncode)
            if failure:
                result['failure']=failure['kind']
                print('\n'+failure['title']+'\n'+failure['advice'],flush=True)
                if failure['kind']=='camera_permission':
                    print('Camera permission check command:\n'+camera_check_command(a.camera or 0),flush=True)
            if status_view is not None:status_view.show_once()
            try:
                result["receiver_exit"]=rec.wait(timeout=5) if rec is not None else None
            except subprocess.TimeoutExpired:
                result["receiver_exit"]="still_waiting_no_connection"
            if a.keep_dashboard and dashboard is not None:
                if status_view is not None:status_view.close();status_view=None
                dashboard.finish(result,failure)
                if dashboard.preview_label is not None:
                    dashboard.preview_label.configure(text=(failure['title'] if failure else 'Camera closed, run ended')+
                        '\nFor another run: restart the receiver in terminal 1 first, then the Conductor',wraplength=620,justify='center')
                (a.output/'integration_result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
                while not restart_requested and not dashboard_quit:
                    dashboard.update(time.perf_counter())
                    time.sleep(.02)
    except KeyboardInterrupt:
        result["operator_interrupt"]=True
        process_event(a.output,'supervisor_interrupted')
    except Exception as exc:
        result['supervisor_error']=dict(type=type(exc).__name__,message=str(exc))
        process_event(a.output,'supervisor_exception',**result['supervisor_error'])
        raise
    finally:
        # A second external signal must not abandon owned children halfway
        # through cleanup. Escalation below is already bounded.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        process_event(a.output,'cleanup_started')
        # Cleanup must not depend on native model startup or a healthy GUI.
        # Each child gets a bounded graceful/terminate/kill sequence.
        for label,process in (("sender",snd),("receiver",rec)):
            try:
                cleanup = stop_owned_process(process)
                result[label+'_cleanup'] = cleanup
                if process is not None:
                    result[label+'_exit'] = process.poll()
            except (OSError, ValueError) as exc:
                result[label+'_cleanup_error'] = repr(exc)
        for label, resource in (("dashboard",dashboard),("status_view",status_view)):
            if resource is not None:
                try:resource.close()
                except Exception as exc:result[label+'_close_error']=repr(exc)
        for preview_fd in (preview_read,preview_write):
            if preview_fd is not None:
                try:os.close(preview_fd)
                except OSError:pass
        (a.output/"integration_result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        process_event(a.output,'cleanup_finished',sender=exit_evidence(result.get('sender_exit')),
                      receiver=exit_evidence(result.get('receiver_exit')))
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    print(json.dumps(result),flush=True)
    if restart_requested:return 75
    return 0 if result.get("sender_exit")==0 else 1


if __name__=="__main__":
    raise SystemExit(main())
