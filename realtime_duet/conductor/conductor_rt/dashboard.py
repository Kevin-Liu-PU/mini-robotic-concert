"""Local diagnostics and RAM preview. No network or robot APIs."""
import json
import tkinter as tk
import math
import time
import io
from .status_view import latest_snapshot


def recording_feedback(recording, requested=False):
    """Button/status acknowledgement; never infer that a writer has finalized."""
    if recording.get('error'):
        return '保存失败', 'disabled', 'REC ERROR: '+str(recording['error'])
    if recording.get('finalized'):
        return '已停录 ✓', 'disabled', f"已停录并保存 {recording.get('written',0)} 帧"
    if recording.get('stopping') or requested:
        return '正在保存…', 'disabled', '录像正在停止并保存，请等待确认'
    if recording.get('active'):
        return 'Stop recording', 'normal', 'REC ● 本机录像'
    return '录像未开启', 'disabled', '录像 OFF'


class Dashboard:
    def __init__(self, output, send, auto_stop, beat_only=False, preview_fd=None, allow_restart=False):
        self.output, self.send = output, send
        self.finished=None
        self.prepare_button=None
        self.record_button=None
        self.record_stop_requested=False
        self.root = tk.Tk()
        self.root.title('Conductor BEAT TEST - automatic reference' if beat_only else 'Conductor + Cello MOCK - live diagnostics')
        self.root.geometry('1120x800' if preview_fd is not None else '820x590')
        self.text = tk.StringVar(value='Loading model; no music started.')
        tk.Label(self.root, text='CAMERA → AUTO REFERENCE → BEAT OUTPUT' if beat_only else 'CAMERA → AUTO REFERENCE → CELLO MOCK',
                 font=('Helvetica', 14, 'bold')).pack(pady=12)
        self.beat = tk.Label(self.root, text='初始化相机', font=('Helvetica', 54, 'bold'), bg='#e4e8ee')
        self.beat.pack(fill='x',padx=16,pady=8)
        self.capture_status=tk.Label(self.root,text='CAMERA OFF — 尚未收到相机帧',font=('Helvetica',15,'bold'))
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
            self.preview_label=tk.Label(preview_frame,text='等待相机预览（只在内存显示）')
            self.preview_label.pack(fill='both',expand=True)
            self.l_labels={str(side):tk.Label(preview_column,text=f'{name}：等待参考',wraplength=630,justify='left',font=('Helvetica',12)) for side,name in ((0,'身体右手'),(1,'身体左手'))}
            for label in self.l_labels.values():label.pack(fill='x',pady=4)
        tk.Label(content, textvariable=self.text, justify='left', anchor='w',wraplength=430,
                 font=('Menlo', 11)).pack(side='left',fill='both', expand=True, padx=8)
        tk.Label(self.root,text='绿色闪拍：视觉有支撑；橙色闪拍：时钟预测。REC 表示本机摄像头录像；叠加仅是系统判断。').pack()
        tk.Label(self.root, text='Stop 验证中：手腕高于同一下巴线，捏合、收指并停稳。STOP / 空格为兜底。' if auto_stop
                 else 'Auto Stop OFF. Space / Stop is the safety fallback.', fg='darkred').pack()
        tk.Label(self.root, text='固定所选手：L = Prepare（只准备）。完整一小节后起奏；短失踪保持节奏，连续丢失 2 秒暂停。').pack(pady=5)
        buttons=tk.Frame(self.root);buttons.pack(pady=10)
        controls=[('Prepare (P)','prepare'),('Stop recording','record-stop'), ('STOP (Space)','stop'), ('Quit (Q)','quit')]
        if allow_restart:controls.insert(2,('New test','restart'))
        for label,command in controls:
            callback=self.request_record_stop if command=='record-stop' else lambda c=command:self.send(c)
            button=tk.Button(buttons,text=label,command=callback,width=16)
            button.pack(side='left',padx=8)
            if command=='prepare':self.prepare_button=button
            if command=='record-stop':
                self.record_button=button
                button.configure(state='disabled')
        self.root.bind('<Key-p>',lambda e:self.send('prepare'))
        self.root.bind('<space>',lambda e:self.send('stop'))
        self.root.bind('<Key-q>',lambda e:self.send('quit'))
        self.root.protocol('WM_DELETE_WINDOW',lambda:self.send('quit'))
        self.next_update = 0.
        self.root.lift()

    def request_record_stop(self):
        if self.record_stop_requested:
            return
        self.record_stop_requested=True
        self.record_button.configure(text='正在保存…',state='disabled')
        self.send('record-stop')

    def update(self, now):
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
                        self.beat.configure(text=f'{index % meter+1} / {meter}    Bar {index//meter+1}')
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
                audio=s.get('audio',{})
                audio_text=('AUDIO ERROR: '+str(audio['error']) if audio.get('error') else
                    '节拍声 ON' if audio.get('active') else '节拍声 OFF')
                remaining=max(0,math.ceil(s.get('session_limit_s',0)-s.get('elapsed_s',0))) if s.get('session_limit_s') else None
                camera_on=s.get('camera_active',False) and self.finished is None
                record_label,record_state,recording_text=recording_feedback(rec,self.record_stop_requested)
                if self.record_button is not None:
                    self.record_button.configure(text=record_label,state=record_state)
                self.capture_status.configure(text=(f"CAMERA ON | {recording_text} | 剩余 {remaining}s" if camera_on else
                    f"CAMERA OFF — 本轮结束 / {s.get('stop_reason') or '尚未开始'}"),fg='red' if rec.get('active') or remaining is not None and remaining<15 else 'black')
                gate_names=dict(wrist_above_chin='手腕高于下巴',index_up='食指向上',thumb_crosswise='拇指横向',
                    index_straight='食指伸直',thumb_spread='拇指展开',thumb_index_apart='拇指食指分开',other_fingers_folded='其余三指收拢')
                for side,info in t.get('prepare_hands',{}).items():
                    if self.preview_label is not None:
                        text=('身体右手' if side=='0' else '身体左手')+(' [本轮固定指挥手]' if str(t.get('locked_side'))==side else ' [本轮不使用]')+': '
                        if not info['eligible']:text+='未达到可靠手部/身体参考条件'
                        else:
                            text+='  '.join(('✓' if value else '×')+gate_names[key] for key,value in info['conditions'].items())
                            text+='  '+('✓' if info['held_150ms'] else '×')+'持续 0.15s'
                        self.l_labels[side].configure(text=text,fg='darkgreen' if info['held_150ms'] else 'black')
                if self.prepare_button is not None:self.prepare_button.configure(state='normal' if s['state'] in ('idle','armed','paused') and t.get('tracking_ready') else 'disabled')
                if s['state']!='running':
                    label=('已停止' if s['state'] in ('stopped','ended') else
                           '跟踪丢失，已暂停' if s['state']=='paused' else
                           '准备拍不完整，继续下一小节' if s.get('count_in_reason') in ('incomplete_bar_resync','incoherent_bar_resync') else
                           f"准备拍 {s.get('count_in_cues',0)}/{s['meter']+1}" if s['state']=='armed' else
                           '可以准备' if t.get('tracking_ready') else
                           '等待指挥手入镜' if t.get('reference_state')=='tracked' else '等待身体识别')
                    self.beat.configure(text=label,bg='#e4e8ee')
                def number(v):return '—' if v is None else f'{v:.1f}'
                self.text.set(f"State: {s['state']}    {s['meter']}/4    BPM: {s['bpm']:.1f}    phase: {s['phase']:.2f}\n"
                    f"身体参考: {t.get('reference_state','waiting')} (髋部非必需)\n"
                    f"参考原因: {t.get('reference_reason','waiting')}\n"
                    f"指挥手: {t.get('association','waiting')}\n"
                    f"固定指挥手: {'右手' if s.get('conducting_hand')=='right' else '左手'}（镜像预览）；wrist visibility: {t.get('wrist_visibility',{})}\n"
                    f"Hands: {t.get('detected_hands',0)} selected: {t.get('selected_side')} reliable: {t.get('tracking_ready',False)}\n"
                    f"FPS: {number(s['recent_fps'])}    model: {number(s['model_ms'])} ms    pipeline: {number(s['input_to_detection_ms'])} ms\n"
                    f"Frames: {s['camera_frames']} capture / {s['inferred_frames']} inference / {s['dropped_frames']} dropped\n"
                    f"Recording: {rec.get('written',0)} written / {rec.get('dropped',0)} dropped\n"
                    f"{audio_text} | 已下发 {audio.get('rendered',0)} / underrun {audio.get('underflows',0)}\n"
                    f"Count-in: {s.get('count_in_cues',0)}/{s['meter']+1}    {s.get('count_in_reason','waiting')} (resync {s.get('count_in_resets',0)})\n"
                    f"Motion: {t.get('beat_stage','waiting')} down {t.get('downstroke_shoulder_units',0):.2f}/0.40 rebound {t.get('rebound_shoulder_units',0):.2f}/0.10\n"
                    f"Latest cue: {s.get('latest_visual_candidate') or 'none'}    clock accepted/rejected: {s['accepted']}/{s['rejected']}\n"
                    f"Stop reason: {s['stop_reason'] or 'none'}" + ('\n相机已释放。New test 开始新会话。' if self.finished is not None else ''))
            except (OSError,ValueError,KeyError):pass
            ui=dict(window_state=self.root.state(),viewable=bool(self.root.winfo_viewable()),
                    width=self.root.winfo_width(),height=self.root.winfo_height(),preview_frames_displayed=self.preview_sequence,
                    last_update_s=now,preview_image_files_saved=False)
            (self.output/'ui-state.json').write_text(json.dumps(ui),encoding='utf-8')
            self.next_update=now+.25
        self.root.update_idletasks()
        self.root.update()

    def finish(self,result):
        try:
            metrics=json.loads((self.output/'conductor/metrics.json').read_text())
            self.finished=dict(state=metrics['final_state'],bpm=metrics['final_bpm'],phase=metrics['final_phase'],
                               stop_reason=metrics.get('stop_reason'),camera_active=False,recording=metrics.get('recording',{}),
                               audio=metrics.get('audio',dict(enabled=False,active=False)))
        except (OSError,ValueError,KeyError):
            self.finished=dict(state='stopped',stop_reason=f"process exit {result.get('sender_exit')}",
                               audio=dict(enabled=False,active=False))
        if self.preview_label is not None:
            self.preview_label.configure(image='',text='相机 OFF — 本轮已经结束\n画面不再更新，手势不会被处理\n请点 New test 开始新一轮',font=('Helvetica',22,'bold'))
            self.preview_image=None
        self.next_update=0.

    def close(self):
        self.display_log.close()
        try:self.root.destroy()
        except tk.TclError:pass
