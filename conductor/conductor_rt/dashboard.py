"""Local diagnostics and RAM preview. No network or robot APIs."""
import json
import tkinter as tk
import math
import time
import io
from .status_view import latest_snapshot
from .metronome import MetronomeController


def recording_feedback(recording, requested=False):
    """Button/status acknowledgement; never infer that a writer has finalized."""
    if recording.get('error'):
        return 'Save failed', 'disabled', 'REC ERROR: '+str(recording['error'])
    if recording.get('finalized'):
        return 'Saved ✓', 'disabled', f"Recording stopped and saved, {recording.get('written',0)} frames"
    if recording.get('stopping') or requested:
        return 'Saving…', 'disabled', 'Recording is stopping and saving; please wait'
    if recording.get('active'):
        return 'Stop recording', 'normal', 'REC ● local recording'
    return 'Recording off', 'disabled', 'Recording OFF'


def formal_recording_feedback(recording, required, finished=False, new_test=True):
    """Readiness is a written local container, not a promise of final validity.
    `new_test` is False where the window has no New test button (the live TCP launcher): the texts
    then do not point at a button that is not there."""
    if not required:
        return True, 'Preview mode: recording was not requested', '#555555'
    if recording.get('error'):
        return False, 'Recording failed: '+str(recording['error'])+'; this run cannot be used for formal evaluation.'+(' Use New test.' if new_test else ''), 'red'
    if recording.get('finalized'):
        return False, f"Recording stopped and saved, {recording.get('written',0)} frames"+('; use New test for the next run.' if new_test else '.'), 'darkgreen'
    if recording.get('stopping'):
        return False, 'Recording is being saved; wait for the result before starting the next run.', 'darkorange'
    if recording.get('enabled') and recording.get('active') and recording.get('save_ready'):
        return True, f"● recording locally · written {recording.get('written',0)} frames · no microphone", 'red'
    if finished:
        return False, 'This run has no recording ready and cannot be used for formal evaluation'+('; use New test.' if new_test else '.'), 'red'
    return False, 'Formal test: recording is starting; the metronome can start once frames are being written.', 'darkorange'


def metronome_running_note(status):
    """The line under the metronome button while it runs. It shows no bar number: the metronome counts
    bars from the moment its button was pressed, which is not the bar of the piece."""
    return f"{status.get('bpm',70)} BPM · accent on beat 1"


def camera_off_text(s, finished):
    """The camera line while the camera is off: either it has not been opened yet, or the run has
    ended, and then why."""
    if not finished and not s.get('camera_frames'):return 'CAMERA OFF — camera not opened yet'
    reason=s.get('stop_reason')
    return 'CAMERA OFF — run ended / '+(str(reason) if reason else 'piece complete' if s.get('state')=='ended' else 'Stopped')


KEEP_GOING_S=8.
FRESH_PREPARE=('auto_prepare','operator_prepare','visual_prepare','explicit_prepare')


def bar_not_counted(s, memory, now):
    """Rolling (gesture) count-in only. There a bar that did not count needs no repeat and no still
    hand: the next bar counts by itself. So the prompt must not fall back to the first "ready" text,
    which reads as "begin again". `memory` is None or (time of the last sign of conducting, cues seen
    by then); the return value is the memory for `now`. It begins when a bar is being counted, lives
    on while cues keep coming, and ends KEEP_GOING_S after the last one, at a fresh Prepare, and when
    the count-in is over."""
    if s.get('state')!='armed' or s.get('count_in_reason') in FRESH_PREPARE:return None
    cues=s.get('visual_candidates',0)
    if s.get('count_in_cues',0) or memory is not None and cues!=memory[1]:return (now,cues)
    if memory is not None and now-memory[0]>KEEP_GOING_S:return None
    return memory


def preparation_feedback(s, keep_going=False, new_test=True):
    state=s['state'];tracking=s.get('tracking',{});ready=tracking.get('tracking_ready',tracking.get('visible',False))
    if state in ('stopped','ended'):return 'Stopped','Run ended; New test starts a new run.' if new_test else 'Run ended.'
    if state=='paused':return 'Paused','When the hand is back in the frame, show L or click Prepare, then count one full bar again.'
    if not ready:
        hint=('Keep your face and both shoulders in the frame.' if tracking.get('reference_state')!='tracked'
              else 'Bring the conducting hand fully back into the frame.')
        return ('Prepared · waiting for you in the frame' if state=='armed' else 'Waiting for you in the frame'),hint+' The piece has not started.'
    if state=='armed':
        stage=s.get('count_in_stage','counting' if s.get('count_in_cues') else 'waiting')
        if stage=='recovering':
            reason=s.get('count_in_reason','')
            why=('The count-in was interrupted; earlier beats were cleared.' if reason in ('preparation_gap','preparation_tracking_lost')
                 else 'The count-in was incomplete; the piece has not started.')
            if not s.get('count_in_quiet_recovery',True):
                return 'Prepare again',why+' In manual calibration mode, show the L gesture or click Prepare again.'
            return 'Pause, then start again from beat one',why+' Hold the hand still in the frame for about 0.6 s, or show L / click Prepare again.'
        count=s.get('count_in_cues',0);meter=s['meter']
        if stage=='scheduled':return f'Count-in complete {meter} / {meter}','The start is predicted on the next beat at that tempo; the next real downbeat has not been seen yet.'
        if count:return f'Count-in beat {count} / {meter}',f'Only {meter} beats needed; after that the start is predicted at the observed tempo.'
        if keep_going:return 'Keep conducting','The last bar was not counted; the next bar is counted automatically. No need to prepare again.'
        return 'Prepared · you may listen first',f'You may listen to the beat first. When ready, conduct {meter} beats from beat one; the first beat after that is predicted.'
    return 'Ready to prepare','Show L or click Prepare. Then you may listen to the beat before conducting.'


def running_feedback(s):
    tracking=s.get('tracking',{})
    if s.get('tracking_timeout_s') == 0 and not s.get('tracking_fresh',tracking.get('tracking_ready',tracking.get('visible',False))):
        return 'Tracking lost: continuing at the last tempo (predicted). Updates when tracking returns; STOP / Space stops.'
    return ('The first beat is predicted; waiting for a real downbeat to confirm it.' if s.get('start_evidence')=='predicted'
            else 'The predicted first beat is supported by a real downbeat.' if s.get('start_evidence')=='visually_confirmed' else '')


class Dashboard:
    def __init__(self, output, send, auto_stop, beat_only=False, preview_fd=None, allow_restart=False, recording_required=False, initial_meter=3, initial_bpm=70, rolling_count_in=False):
        self.output, self.send = output, send
        self.allow_restart=allow_restart          # whether this window has a New test button
        self.finished=None
        self.initial_meter=initial_meter
        self.initial_bpm=initial_bpm
        self.rolling_count_in=rolling_count_in    # gesture count-in: a bar that did not count needs no repeat
        self.bar_memory=None
        self.failure=None                         # why the sender ended badly: dict(title, advice), set by finish()
        self.recording_required=recording_required
        self.recording_state={}
        self.recording_gate_ready=not recording_required
        self.metronome=MetronomeController(output)
        self.metronome_status={"state":"stopped","expected_beat":None,"meter":initial_meter,"bpm":initial_bpm}
        self.prepare_button=None
        self.record_button=None
        self.record_stop_requested=False
        self.root = tk.Tk()
        self.root.title('Conductor · one-bar count-in' if beat_only else 'Conductor + Cello MOCK - live diagnostics')
        self.root.geometry('1120x900' if preview_fd is not None else '900x720')
        self.text = tk.StringVar(value='Loading model; no music started.')
        tk.Label(self.root, text='CAMERA → AUTO REFERENCE → BEAT OUTPUT' if beat_only else 'CAMERA → AUTO REFERENCE → CELLO MOCK',
                 font=('Helvetica', 14, 'bold')).pack(pady=12)
        self.recording_banner=tk.Label(self.root,text=formal_recording_feedback({},recording_required)[1],
            font=('Helvetica',14,'bold'),fg='darkorange' if recording_required else '#555555',wraplength=1080)
        self.recording_banner.pack(fill='x',padx=16,pady=(0,6))
        self.build_metronome_controls()
        tk.Label(self.root,text='Detected by the system (not the expected beat)',font=('Helvetica',12)).pack()
        self.beat = tk.Label(self.root, text='Starting camera', font=('Helvetica', 34, 'bold'), bg='#e4e8ee',fg='#18222e')
        self.beat.pack(fill='x',padx=16,pady=8)
        self.prepare_hint=tk.Label(self.root,text='',font=('Helvetica',15),wraplength=1080,bg='#e4e8ee',fg='#18222e')
        self.prepare_hint.pack(fill='x',padx=16,pady=(0,4))
        self.capture_status=tk.Label(self.root,text='CAMERA OFF — no camera frame yet',font=('Helvetica',15,'bold'))
        self.capture_status.pack(fill='x',padx=16,pady=5)
        self.last_beat=None
        self.flash_until=0.
        self.next_beat_poll=0.
        self.display_log=(output/'beat-display.jsonl').open('x',encoding='utf-8')
        content=tk.Frame(self.root);content.pack(fill='both',expand=True,padx=12)
        self.preview=None
        self.preview_sequence=0
        self.preview_image=None
        self.preview_label=None
        if preview_fd is not None:
            from .preview import PreviewReader
            self.preview=PreviewReader(preview_fd)
            preview_column=tk.Frame(content);preview_column.pack(side='left',padx=6)
            preview_frame=tk.Frame(preview_column,width=640,height=360)
            preview_frame.pack();preview_frame.pack_propagate(False)
            self.preview_label=tk.Label(preview_frame,text='Waiting for camera preview (shown from memory only)')
            self.preview_label.pack(fill='both',expand=True)
            self.l_labels={str(side):tk.Label(preview_column,text=f'{name}: waiting for reference',wraplength=630,justify='left',font=('Helvetica',12)) for side,name in ((0,'Right hand (body)'),(1,'Left hand (body)'))}
            for label in self.l_labels.values():label.pack(fill='x',pady=4)
        tk.Label(content, textvariable=self.text, justify='left', anchor='w',wraplength=430,
                 font=('Menlo', 11)).pack(side='left',fill='both', expand=True, padx=8)
        tk.Label(self.root,text='Green flash: beat supported by vision; orange flash: predicted by the clock. REC means the local camera recording; overlays are only the system estimate.').pack()
        tk.Label(self.root, text='Stop gesture under test: wrist above the chin line, pinch, fold the fingers and hold still. STOP / Space is the fallback.' if auto_stop
                 else 'Auto Stop OFF. Space / Stop is the safety fallback.', fg='darkred').pack()
        tk.Label(self.root, text='After L / Prepare you may listen to the beat first; if the count-in is incomplete, hold still and start again. Losing the hand for 2 s during the piece still pauses; the Stop rule is unchanged.').pack(pady=5)
        buttons=tk.Frame(self.root);buttons.pack(pady=10)
        controls=[('Prepare (P)','prepare'),('Stop recording','record-stop'), ('STOP (Space)','stop'), ('Quit (Q)','quit')]
        if allow_restart:controls.insert(2,('New test','restart'))
        for label,command in controls:
            callback=(self.request_record_stop if command=='record-stop' else self.request_quit if command=='quit' else lambda c=command:self.send(c))
            button=tk.Button(buttons,text=label,command=callback,width=16)
            button.pack(side='left',padx=8)
            if command=='prepare':self.prepare_button=button
            if command=='record-stop':
                self.record_button=button
                button.configure(state='disabled')
        self.root.bind('<Key-p>',lambda e:self.send('prepare'))
        self.root.bind('<space>',lambda e:self.send('stop'))
        self.root.bind('<Key-q>',lambda e:self.request_quit())
        self.root.protocol('WM_DELETE_WINDOW',self.request_quit)
        self.next_update = 0.
        self.root.lift()

    def build_metronome_controls(self):
        frame=tk.LabelFrame(self.root,text='Independent metronome · expected beats',font=('Helvetica',15,'bold'),bg='#f5f3ff')
        frame.pack(fill='x',padx=16,pady=(0,8))
        self.expected_beat=tk.Label(frame,text='—',font=('Helvetica',48,'bold'),fg='#5b21b6',bg='#f5f3ff',width=5)
        self.expected_beat.pack(side='left',padx=16,pady=3)
        controls=tk.Frame(frame,bg='#f5f3ff');controls.pack(side='left',fill='x',expand=True,padx=8,pady=5)
        row=tk.Frame(controls,bg='#f5f3ff');row.pack(anchor='w')
        tk.Label(row,text='Meter for this run',bg='#f5f3ff').pack(side='left')
        self.metronome_meter=tk.StringVar(value=str(self.initial_meter))
        self.metronome_meter_input=tk.Label(row,text=f'{self.initial_meter}/4',font=('Helvetica',17,'bold'),bg='#f5f3ff',disabledforeground='#5b21b6')
        self.metronome_meter_input.pack(side='left',padx=4)
        self.metronome_meter_input.configure(state='disabled')
        tk.Label(row,text='(change with New test)',bg='#f5f3ff').pack(side='left')
        tk.Label(row,text='BPM',bg='#f5f3ff').pack(side='left')
        self.metronome_bpm=tk.StringVar(value=f'{self.initial_bpm:g}')
        self.metronome_bpm_input=tk.Spinbox(row,from_=30,to=180,textvariable=self.metronome_bpm,width=5,font=('Helvetica',17))
        self.metronome_bpm_input.pack(side='left',padx=6)
        self.metronome_button=tk.Button(row,text='Start metronome',font=('Helvetica',17,'bold'),command=self.toggle_metronome,width=14)
        self.metronome_button.pack(side='left',padx=12)
        if self.recording_required:self.metronome_button.configure(state='disabled')
        self.metronome_notice=tk.Label(controls,text='Click to start; no L gesture needed. Accent on beat 1.',font=('Helvetica',13),bg='#f5f3ff',anchor='w')
        self.metronome_notice.pack(fill='x',pady=(4,0))

    def toggle_metronome(self):
        try:
            state=self.metronome.poll().get('state','stopped')
            if state in ('starting','running','stopping'):
                self.metronome.request_stop()
            else:
                if not getattr(self,'recording_gate_ready',True):
                    self.update_metronome()
                    self.metronome_notice.configure(text='Recording is not ready, so the formal test cannot start. See the recording status above.')
                    return
                self.metronome.start(meter=int(self.metronome_meter.get()),bpm=float(self.metronome_bpm.get()))
        except (ValueError,RuntimeError,OSError) as exc:
            self.update_metronome()
            self.metronome_notice.configure(text='Metronome did not start: '+str(exc))
            return
        # This control never sends Prepare, Stop or Quit to the detector.
        self.update_metronome()

    def update_metronome(self):
        try:
            status=self.metronome.poll()
        except Exception as exc:
            status=dict(state='error',error=str(exc))
        self.metronome_status=status
        state=status.get('state','stopped')
        active=state in ('starting','running','stopping')
        self.metronome_meter_input.configure(state='disabled')  # Same meter as detector; New test reopens setup.
        self.metronome_bpm_input.configure(state='disabled' if active else 'normal')
        self.metronome_button.configure(text='Stop metronome' if active else 'Start metronome',state='disabled' if state=='stopping' or not active and not getattr(self,'recording_gate_ready',True) else 'normal')
        if state=='running':
            beat=status.get('expected_beat');meter=status.get('meter',3)
            self.expected_beat.configure(text=f'{beat} / {meter}' if beat is not None else '…')
            note=metronome_running_note(status)
        else:
            self.expected_beat.configure(text='…' if state=='starting' else '—')
            note=('Opening audio output…' if state=='starting' else
                  'Stopping the metronome; detection continues.' if state=='stopping' else
                  'Metronome error: '+str(status.get('error','unknown error')) if state=='error' else
                  'Available once recording is ready; no L gesture needed.' if not getattr(self,'recording_gate_ready',True) else
                  'Click to start; no L gesture needed. Stopping the metronome does not close the camera.')
        self.metronome_notice.configure(text=note)

    def request_quit(self):
        try:self.metronome.request_stop()
        finally:self.send('quit')

    def request_record_stop(self):
        if self.record_stop_requested:
            return
        self.record_stop_requested=True
        self.record_button.configure(text='Saving…',state='disabled')
        self.send('record-stop')

    def update(self, now):
        self.update_metronome()
        if self.preview is not None and self.finished is None:
            data,sequence,received=self.preview.latest()
            if data is not None and sequence!=self.preview_sequence:
                from PIL import Image, ImageTk
                self.preview_image=ImageTk.PhotoImage(Image.open(io.BytesIO(data)))
                self.preview_label.configure(image=self.preview_image,text='',width=640,height=360)
                self.preview_sequence=sequence
        if now >= self.next_beat_poll:
            snapshot=latest_snapshot(self.output/'conductor/stream.jsonl')
            if snapshot:
                state=snapshot.get('state')
                if state=='running':
                    index=math.floor(snapshot['beat_position'])
                    meter=snapshot['meter'][0]
                    if index != self.last_beat:
                        skipped=0 if self.last_beat is None else max(0,index-self.last_beat-1)
                        self.last_beat=index
                        self.flash_until=now+.12
                        uncertain = snapshot.get('bar_position_status') == 'ambiguous'
                        self.beat.configure(text=(f'Beat number unconfirmed · estimate {index % meter+1} / {meter}' if uncertain
                                                  else f'{index % meter+1} / {meter}    Bar {index//meter+1}'))
                        self.display_log.write(json.dumps(dict(kind='beat_display',beat_index=index,
                            source_monotonic_ns=snapshot['source_monotonic_ns'],display_requested_s=now,
                            quality=snapshot['quality'],skipped_visual_beats=skipped,
                            claim='GUI request time, not measured screen/photon latency'))+'\n')
                        self.display_log.flush()
                    self.beat.configure(bg=('#60d394' if snapshot['quality']=='tracked' else '#ffd166') if now < self.flash_until else '#e4e8ee')
                else:
                    self.beat.configure(bg='#e4e8ee')
            self.next_beat_poll=now+.016
        if now >= self.next_update:
            try:
                s=json.loads((self.output/'conductor/live-status.json').read_text())
                if self.finished is not None:s.update(self.finished)
                t=s['tracking']
                rec=s.get('recording',{})
                self.recording_state=rec
                new_test=getattr(self,'allow_restart',True)
                self.recording_gate_ready,record_notice,record_color=formal_recording_feedback(rec,self.recording_required,self.finished is not None,new_test)
                self.recording_banner.configure(text=record_notice,fg=record_color)
                audio=s.get('audio',{})
                audio_text=('AUDIO ERROR: '+str(audio['error']) if audio.get('error') else
                    'Detector feedback sound ON' if audio.get('active') else 'Detector feedback sound OFF')
                remaining=max(0,math.ceil(s.get('session_limit_s',0)-s.get('elapsed_s',0))) if s.get('session_limit_s') else None
                camera_on=s.get('camera_active',False) and self.finished is None
                record_label,record_state,recording_text=recording_feedback(rec,self.record_stop_requested)
                if self.record_button is not None:
                    self.record_button.configure(text=record_label,state=record_state)
                self.capture_status.configure(text=(f"CAMERA ON | {recording_text} | remaining {remaining}s" if camera_on else
                    'CAMERA OFF — '+self.failure['title'] if self.failure else camera_off_text(s,self.finished is not None)),
                    fg='red' if rec.get('active') or self.failure or remaining is not None and remaining<15 else 'black')
                gate_names=dict(wrist_above_chin='wrist above chin',index_up='index up',thumb_crosswise='thumb sideways',
                    index_straight='index straight',thumb_spread='thumb spread',thumb_index_apart='thumb and index apart',other_fingers_folded='other three fingers folded')
                for side,info in t.get('prepare_hands',{}).items():
                    if self.preview_label is not None:
                        text=('Right hand (body)' if side=='0' else 'Left hand (body)')+(' [conducting hand for this run]' if str(t.get('locked_side'))==side else ' [not used in this run]')+': '
                        if not info['eligible']:text+='no reliable hand/body reference yet'
                        else:
                            text+='  '.join(('✓' if value else '×')+gate_names[key] for key,value in info['conditions'].items())
                            text+='  '+('✓' if info['held_150ms'] else '×')+'held 0.15 s'
                        self.l_labels[side].configure(text=text,fg='darkgreen' if info['held_150ms'] else 'black')
                if self.prepare_button is not None:self.prepare_button.configure(state='normal' if s['state'] in ('idle','armed','paused') and t.get('tracking_ready',t.get('visible',False)) else 'disabled')
                if s['state']!='running':
                    self.bar_memory=bar_not_counted(s,self.bar_memory,now) if self.rolling_count_in else None
                    label,hint=preparation_feedback(s,self.bar_memory is not None,new_test)
                    if self.failure:label,hint=self.failure['title'],self.failure['advice']
                    self.beat.configure(text=label,bg='#fde2e2' if self.failure else '#e4e8ee',fg='#8a1c1c' if self.failure else '#18222e')
                    self.prepare_hint.configure(text=hint)
                else:self.prepare_hint.configure(text=running_feedback(s))
                if self.prepare_button is not None:
                    self.prepare_button.configure(text='Prepare again (P)' if s['state']=='armed' else 'Prepare (P)')
                def number(v):return '—' if v is None else f'{v:.1f}'
                self.text.set(f"State: {s['state']}    {s['meter']}/4    BPM: {s['bpm']:.1f}    phase: {s['phase']:.2f}\n"
                    f"Beat number: {'may be shifted; prepare again to confirm' if s.get('bar_position_status') == 'ambiguous' else 'estimated from the count-in'}\n"
                    f"Body reference: {t.get('reference_state','waiting')} (hips not required)\n"
                    f"Reference reason: {t.get('reference_reason','waiting')}\n"
                    f"Conducting hand: {t.get('association','waiting')}\n"
                    f"Fixed conducting hand: {'right hand' if s.get('conducting_hand')=='right' else 'left hand'} (mirrored preview); wrist visibility: {t.get('wrist_visibility',{})}\n"
                    f"Hands: {t.get('detected_hands',0)} selected: {t.get('selected_side')} reliable: {t.get('tracking_ready',False)}\n"
                    f"FPS: {number(s['recent_fps'])}    model: {number(s['model_ms'])} ms    pipeline: {number(s['input_to_detection_ms'])} ms\n"
                    f"Frames: {s['camera_frames']} capture / {s['inferred_frames']} inference / {s['dropped_frames']} dropped\n"
                    f"Recording: {rec.get('written',0)} written / {rec.get('dropped',0)} dropped\n"
                    f"{audio_text} | rendered {audio.get('rendered',0)} / underrun {audio.get('underflows',0)}\n"
                    f"Count-in: {s.get('count_in_cues',0)}/{s['meter']}    {s.get('count_in_reason','waiting')} (resync {s.get('count_in_resets',0)})\n"
                    f"Motion: {t.get('beat_stage','waiting')} down {t.get('downstroke_shoulder_units',0):.2f}/0.40 rebound {t.get('rebound_shoulder_units',0):.2f}/0.10\n"
                    f"Latest cue: {s.get('latest_visual_candidate') or 'none'}    clock accepted/rejected/deferred: {s['accepted']}/{s['rejected']}/{s.get('deferred',0)}\n"
                    f"Stop reason: {s['stop_reason'] or 'none'}" + (('\nCamera released. New test starts a new session.' if new_test else '\nCamera released.') if self.finished is not None else ''))
            except (OSError,ValueError,KeyError):pass
            ui=dict(window_state=self.root.state(),viewable=bool(self.root.winfo_viewable()),
                    width=self.root.winfo_width(),height=self.root.winfo_height(),preview_frames_displayed=self.preview_sequence,
                    last_update_s=now,preview_image_files_saved=False,metronome=self.metronome_status,
                    expected_label=self.expected_beat.cget('text'),metronome_button=self.metronome_button.cget('text'),
                    recording_required=self.recording_required,recording_ready=self.recording_gate_ready,
                    recording_notice=self.recording_banner.cget('text'))
            (self.output/'ui-state.json').write_text(json.dumps(ui),encoding='utf-8')
            self.next_update=now+.25
        self.root.update_idletasks()
        self.root.update()

    def finish(self,result,failure=None):
        self.failure=failure
        if failure:
            # the sender may have ended before it wrote any status, and then nothing else updates these
            self.beat.configure(text=failure['title'],bg='#fde2e2',fg='#8a1c1c')
            self.prepare_hint.configure(text=failure['advice'],fg='#8a1c1c')
            self.capture_status.configure(text='CAMERA OFF — '+failure['title'],fg='red')
        try:
            metrics=json.loads((self.output/'conductor/metrics.json').read_text())
            self.finished=dict(state=metrics['final_state'],bpm=metrics['final_bpm'],phase=metrics['final_phase'],
                               stop_reason=metrics.get('stop_reason'),camera_active=False,recording=metrics.get('recording',{}),
                               audio=metrics.get('audio',dict(enabled=False,active=False)))
        except (OSError,ValueError,KeyError):
            self.finished=dict(state='stopped',stop_reason=f"process exit {result.get('sender_exit')}",
                               audio=dict(enabled=False,active=False))
        if self.preview_label is not None:
            self.preview_label.configure(image='',text='Camera OFF — this run has ended\nThe picture no longer updates and gestures are not processed'+
                ('\nClick New test to start a new run' if getattr(self,'allow_restart',True) else ''),font=('Helvetica',22,'bold'))
            self.preview_image=None
        self.next_update=0.

    def close(self):
        try:
            self.metronome.close()
        finally:
            self.display_log.close()
            try:self.root.destroy()
            except tk.TclError:pass
