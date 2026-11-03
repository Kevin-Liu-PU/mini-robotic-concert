# Conductor → beat：0.3.8 实验版

**完整真人验收尚未通过。** 当前路径是相机/本地视频 → 自动身体与下巴参考 → L/拍点/实验性 Stop → 连续节拍时钟 → 可视节拍及可选合成 click。当前入口不启动 cello 接收端或真实硬件。

0.3.8 修复重复 L 重置 count-in、短手指跟踪间断清空全部运动证据，以及准备拍强制末拍必须指尖越线的问题。参考视频的三拍和四拍启动都有改善，但三拍仍漏 Stop，另有一处疑似假 Stop 未确认动作意图。不能将测试通过数、出声或 FPS 当作正确跟拍的证明。见 [VALIDATION.md](VALIDATION.md)。

## 安装与离线测试

支持 macOS 13+、原生 CPython 3.11（Apple Silicon / Intel），使用带 Tkinter 的现有 Python。不运行仓库根目录旧演示或 robot_runner_simple.py。

```sh
cd realtime_duet
python3.11 -m venv .venv
trial_python="$PWD/.venv/bin/python"
"$trial_python" -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r requirements-macos.txt
"$trial_python" -m pip check
"$trial_python" -B conductor/preflight.py
"$trial_python" -B checks/check_models.py
(cd conductor && ../.venv/bin/python -B -m unittest discover -s tests -v)
```

MediaPipe 模型来自官方 wheel。只安装一个 OpenCV 分发包。声音是 output stream，不启用麦克风。

```sh
cd conductor
../.venv/bin/python -B -m conductor_rt --video /absolute/path/to/authorized-local-video.mp4 --initial-bpm 90 --startup count-in --meter 3 --conducting-hand right --transport file --width 640 --private-input --experimental-auto-stop --continuous-test --session-id unique-test --clock-domain local-unique-test --output ../runs/unique-test
```

输出目录必须不存在。拍号由已知曲目选择 3 或 4，不自动识别。90 只是检测提示，起始速度由已观测准备小节估算。加 `--beat-audio` 才会播放实际发布 beat 的合成 click。文件回放是新的时钟，不是原相机延迟测量。

`--private-input` 只保存必要数值/事件，不保存完整逐帧人脸、身体、手部关键点数组。个人录像、截图和日志放在被忽略的 `runs/`，禁止上传仓库。`checks/paired_evidence_replay.py` 支持新旧版本共享推理结果；其模拟时钟不能证明实时性能。

## 相机测试操作

`./run_beat_test.command` 先打开设置界面。选择 3/4 拍、身体左/右手、3/5/10 分钟时限、是否本机录像和是否出声，点击“打开相机，开始这轮测试”才开启相机。GUI 强制 beat-only，不启动接收端或 TCP。

保持脸、双肩和所选手入镜，不要求髋部入镜或固定位置。预览是镜像，左右按身体定义。所选手负责 L、拍点和 Stop，整轮不自动换手。

1. 等待身体参考和手部可靠。
2. 所选手比 L：手腕高于下巴，食指向上、拇指横向、其余手指收拢，保持约 0.15 秒。Prepare/P 是人工备用，不能算 L 识别成功。
3. 指挥完整准备小节，下一次新鲜下拍自动启动。每一拍须有视觉证据，不补造缺拍。末拍可以是观测到的手腕端点/转折或指尖上穿下巴线；仍检查序列和节奏一致性。已 armed 时重复 L 不重置。
4. 大字拍序、小节和 click 来自连续时钟；短时漏观测会 holdover。出声不代表节拍准确或大提琴已演奏。
5. 实验性 Stop：抬手，手腕高于同一下巴线，拇指与食指捏合、其余手指收拢并停稳。仍有漏检/疑似误停，保留 **Space / Stop**、**Q / Quit** 安全兜底。
6. Stop recording 只停录制；Stop/Quit/时限结束停止相机和录像，显示 Camera OFF。不要对已结束的画面继续做动作。

录像只保存相机画面及时间索引，不录麦克风或桌面。编码队列有界，掉帧记录在日志；视频叠加和算法标签不是 ground truth。

## 跟踪边界

身体参考失效、手腕低可见或离画面，不从旧坐标产生事件。手指 mesh 缺失但同手腕可靠时，最多 0.30 s 允许运动回退；到 0.60 s 仅维护运动历史，不输出事件、不标 tracking ready。更长缺失清空。L/Stop 始终需要真实手形。

短手指间断后，跨线须由可靠手腕上抬支持，时间取重捕获的新观察，不伪造缺失帧事件。Stop 保留 6 样本、至少 220 ms、至少 75% 强手形和稳定性门槛；新的下行动作撤销旧 approach。15 FPS 等低帧率可能无法满足门槛。

相机 beat-only 连续 2 秒无可靠跟踪会暂停，原因 tracking_lost_pause，不是 Stop 手势。恢复后须重新 L/Prepare 和完整准备拍。明显转头、遮挡、快速距离变化、模糊和远景手指跟踪仍有限制。

`--chin-y` / `--calibration` 仅为显式手动调试备用。旧基线在 Git 提交 786283db2e845ceffba4f95d49d5945ca7b7d75e。本次不更新或验收大提琴端。旧 mock 配对入口仅支持 4/4；3 拍必须使用 beat-only。
