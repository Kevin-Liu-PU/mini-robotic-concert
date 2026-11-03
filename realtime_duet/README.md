# 指挥＋模拟大提琴（conductor + mock cello）

可迁移的 conductor 0.2.3.3 与 cello e054：连续拍位（continuous beat phase）通过本机 TCP 驱动 12 小节、48 拍的模拟弓事件。漏视觉观测时预测节拍，有效加速/减速时调整速度。启动器固定 mock，不连接或移动机器人。现有仓库根目录的旧实现保持不变。

## 在 Mac 获取代码（clone）

```sh
git clone --branch feat/conductor-cello-realtime-mac --single-branch \
  https://github.com/Kevin-Liu-PU/mini-robotic-concert.git
cd mini-robotic-concert/realtime_duet
```

如果 Mac 已有这个仓库，在保留本地修改后 fetch 并切到该分支；不要用 reset/clean 覆盖自己的工作。后续工作在本目录进行。

## 建立一个视觉环境（environment）

新环境使用 **CPython 3.11、macOS 13+，原生 arm64 或 x86_64**。先确认 `python3.11` 已可用。只安装本目录清单，两端共用同一解释器（interpreter），不需要机器人环境。

```sh
python3.11 -m venv .venv
trial_python="$PWD/.venv/bin/python"
"$trial_python" -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple \
  -r requirements-macos.txt
"$trial_python" -m pip check
"$trial_python" -B conductor/preflight.py
"$trial_python" -B checks/check_models.py
"$trial_python" -B conductor/check_clocks.py --other-python "$trial_python"
```

若已有兼容环境，可将 `trial_python` 指向其绝对路径，直接执行检查。预检应无错误；时钟检查的 `shared_clock_bracket_ok` 应为 true。模型由官方 MediaPipe wheel 提供，详见 [模型与依赖](MODELS_AND_DEPENDENCIES.md)。不要安装两套 OpenCV，也不要使用 headless 包。安装或检查失败时保留错误信息，不启动相机。

## 第一步：点选下巴线（chin-line calibration）

以下命令显式打开相机预览，macOS 可能请求相机权限。保持实际指挥时的姿势和构图，点击下巴最低点，确认绿色水平线正确后按 Enter，只保存数字 JSON；Esc/关闭窗口取消。Mac GUI/Retina 点击映射尚未实测，请看清绿线后确认。

```sh
duet_root="$PWD"
mkdir -p "$duet_root/runs"
session="$("$trial_python" -c 'import uuid; print(uuid.uuid4())')"
camera_index=0
calibration="$duet_root/runs/chin-$session.json"
"$trial_python" -B conductor/calibrate_chin.py \
  --camera "$camera_index" --width 640 --output "$calibration"
```

预览仅在内存（RAM），不保存画面。校准结束会释放相机；文件已存在时拒绝覆盖。改变相机、座位或头部姿势后重新校准，不沿用旧视频的下巴参数。

## 第二步：相机与 mock 联动（camera trial）

把 `initial_bpm` 替换为本次明确选择的 30–180 BPM。默认明确起拍（explicit start）；如选择数拍起奏（count-in），改成 `startup_mode='count-in'`，仍须提供 BPM 作为检测提示，不能称为完全自动估速。

```sh
initial_bpm='REPLACE_WITH_YOUR_BPM'
startup_mode='explicit'
"$trial_python" -B conductor/launch_rehearsal.py \
  --receiver-root "$duet_root/cello" \
  --receiver-python "$trial_python" --sender-python "$trial_python" \
  --session "$session" --clock-domain "local-mac-$session" --port 18766 \
  --journal "$duet_root/runs/mock-journal" --output "$duet_root/runs/$session" \
  --camera "$camera_index" --width 640 --calibration "$calibration" \
  --initial-bpm "$initial_bpm" --startup "$startup_mode" --interactive
```

同一终端输入并按 Enter：`prepare` 准备、`start` 明确起拍、`stop` 停止、`quit` 退出。count-in 模式等待有效数拍，不用 start 跳过。终端约 2 Hz 显示状态、BPM、拍位与跟踪质量（tracking quality），运行期间没有视频预览窗口。

**自动 Stop 不可靠且默认关闭，本轮使用手动停止（manual Stop）。** 不开启 `--experimental-auto-stop`。每次重启新建 session；不删除 journal 来恢复中断会话。缺少校准或平台/相机参数不匹配会拒绝启动。

相机模式默认不保存画面、音频、逐帧关键点（landmarks）或身体坐标；会保存性能、状态和节拍事件日志。`runs/` 已忽略，不应提交。视频回放的日志规则不同，不要将私人录像或输出加入 Git。

## 软件检查与边界（validation）

无设备测试：

```sh
(cd conductor && "$trial_python" -B -m unittest discover -s tests -q)
(cd cello && "$trial_python" -B -m unittest discover -s tests -q)
"$trial_python" -B checks/smoke_pair.py --receiver-python "$trial_python" \
  --port 18766 --output "$duet_root/runs/smoke-$session"
```

最后一条只使用合成 60 BPM 时钟和 mock 接收端，不加载视觉模型，输出目录须尚不存在。详情见 [VALIDATION.md](VALIDATION.md)。

Mac 相机、网络、GUI、性能与机械臂未验证；Windows 软件通过不等于 Mac 通过。已知约 0.2 秒片段时序偏差和自动 Stop 限制未在此迁移修复。单空 A3 不能还原原谱低音，音高简化尚未批准；当前只验证节奏，不合成声音。

硬件代码保留原门控（hardware gate），未批准模板全部保持 false/null。真实运动需有效现场校准、安装与限值、值守和独立停止路径，本轮没有执行授权或安装驱动。[来源与迁移范围](PROVENANCE.md) 列出保留与排除内容。
