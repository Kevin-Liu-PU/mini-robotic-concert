"""Camera→automatic reference→beat GUI. Never launches a cello receiver."""
from pathlib import Path
import sys
import json
import uuid
from integration_replay import main


def set_option(argv,name,value):
    if name in argv:argv[argv.index(name)+1]=str(value)
    else:argv.extend([name,str(value)])


def choose_test_settings(default=4,default_duration=300,default_hand='right'):
    import tkinter as tk
    root=tk.Tk();root.title('Conductor 测试设置 — 单手与拍号')
    root.geometry('680x560')
    choice=tk.IntVar(value=default)
    duration=tk.IntVar(value=int(default_duration) if default_duration in (180,300,600) else 300)
    record=tk.BooleanVar(value=True)
    audio=tk.BooleanVar(value=True)
    hand=tk.StringVar(value=default_hand)
    selected=[]
    tk.Label(root,text='先选择这轮要指挥的拍号',font=('Helvetica',20,'bold')).pack(pady=15)
    for meter in (3,4):
        tk.Radiobutton(root,text=f'{meter} 拍（{meter}/4）— 完整准备小节后，第 {meter+1} 个观测起奏',
                       variable=choice,value=meter,font=('Helvetica',14)).pack(anchor='w',padx=30,pady=5)
    hand_row=tk.Frame(root);hand_row.pack(pady=8)
    tk.Label(hand_row,text='固定指挥手（身体左右）：').pack(side='left')
    for key,label in (('right','右手'),('left','左手')):
        tk.Radiobutton(hand_row,text=label,variable=hand,value=key).pack(side='left')
    tk.Label(root,text='镜像预览；左右按你的身体定义，标记跟随手移动。\n所选手同时负责 L、拍点和 Stop；本轮不会自动换手。').pack(pady=5)
    duration_row=tk.Frame(root);duration_row.pack(pady=8)
    tk.Label(duration_row,text='本轮时限：').pack(side='left')
    for seconds in (180,300,600):tk.Radiobutton(duration_row,text=f'{seconds//60} 分钟',variable=duration,value=seconds).pack(side='left')
    tk.Checkbutton(root,text='保存本机摄像头视频用于复盘（不录麦克风，不自动上传）',variable=record).pack(pady=5)
    tk.Checkbutton(root,text='实时节拍声（第一拍重音；不打开麦克风）',variable=audio).pack(pady=5)
    tk.Label(root,text='所选手比 L：进入准备；随后指挥完整准备小节，下一下拍起奏。\nStop：手腕高于同一下巴线，捏合、收指并停稳。\nPrepare/P 是人工备用，不算 L 识别成功。\n时限到后相机与录像自动停止，结果保留；随时可 Stop recording / Quit。\n只测相机 → beat，不录音、不启动大提琴。',
             wraplength=540,justify='left').pack(pady=15)
    def begin():selected.append(dict(meter=choice.get(),duration=duration.get(),record=record.get(),hand=hand.get(),audio=audio.get()));root.destroy()
    tk.Button(root,text='打开相机，开始这轮测试',command=begin,font=('Helvetica',14)).pack(pady=8)
    root.protocol('WM_DELETE_WINDOW',root.destroy)
    root.after(200,lambda:print(json.dumps(dict(setup_window_viewable=bool(root.winfo_viewable()),
        setup_window_state=root.state(),default_meter=default,camera_opened=False)),flush=True))
    root.mainloop()
    return selected[0] if selected else None

def run():
    original=list(sys.argv)
    no_setup="--no-setup" in original
    if no_setup:original.remove("--no-setup")
    output=(Path(original[original.index('--output')+1]) if '--output' in original else
            Path(__file__).resolve().parents[1]/'runs'/('beat-'+uuid.uuid4().hex[:12]))
    attempt=0
    while True:
        args=list(original)
        if not no_setup:
            default=int(args[args.index('--meter')+1]) if '--meter' in args else 4
            settings=choose_test_settings(default,float(args[args.index('--duration')+1]) if '--duration' in args else 300,
                args[args.index('--conducting-hand')+1] if '--conducting-hand' in args else 'right')
            if settings is None:return 0
            set_option(args,'--meter',settings['meter'])
            set_option(args,'--duration',settings['duration'])
            set_option(args,'--conducting-hand',settings['hand'])
            args=[a for a in args if a!='--record-video']
            if settings['record']:args.append('--record-video')
            args=[a for a in args if a!='--beat-audio']
            if settings.get('audio',False):args.append('--beat-audio')
        args.extend(['--beat-only','--dashboard','--preview','--keep-dashboard'])
        if attempt:
            session='beat-'+uuid.uuid4().hex[:12]
            set_option(args,'--session',session)
            set_option(args,'--clock-domain','local-'+session)
            set_option(args,'--output',output.parent/(output.name+'-restart-'+session))
        else:set_option(args,'--output',output)
        sys.argv=args
        code=main()
        if code!=75:return code
        attempt+=1


if __name__ == '__main__':raise SystemExit(run())
