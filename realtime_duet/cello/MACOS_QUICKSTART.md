# macOS 大提琴端准备（cello deployment）

目标是用户的 Mac。此包提供可迁移软件与 mock；**尚未在 Mac 执行，尚未验证 Mac 实时性能、音频、摄像头或机器人连接。** Windows 测试不能代替这些检查。本包没有复制 Windows venv、机器人地址、有效安装/校准或批准记录。所有模板仍为 UNAPPROVED。

## 1. 选择已有环境（existing environment）

在解压目录打开 Terminal。优先使用现有大提琴项目的 CPython 3.11/3.12 环境；路径必须是 Mac 的真实解释器，不能照抄 Windows 路径。上游源码提到 `~/venvs/cello311/bin/python`，这里只是可核实的线索，不代表它在这台 Mac 上存在或有效。

```sh
cd '/absolute/path/to/unpacked/cello-package'
cello_python='/absolute/path/to/existing/cello/environment/bin/python'
"$cello_python" -B tools/environment_probe.py --report runs/mac-environment.json
"$cello_python" -B -m unittest discover -s tests -q
```

不要 export 这些 shell 变量：运行门控会核对已有 `CELLO_*` 环境覆盖项。上述小写变量仅用于 shell 命令。

检查报告中的解释器版本、进程架构（process architecture）、依赖版本及共享时钟检查。`arm64` 通常对应原生 Apple Silicon，`x86_64` 也可能是 Rosetta 进程；仍需确认实际芯片和既有环境，不能混装两种架构。调度/mock 测试只需标准库；新增纯几何测试需要 NumPy，缺少时会明确标记 skipped，不能算作通过。测试不导入真实设备模块。两项针对公开上游原文件的静态测试未打包，几何与原源码等价测试在不携带上游源码的包内也明确 skipped；完整开发环境的等价验证另外保留。

当前桥接的直接依赖（direct dependencies）只有 numpy 与 ur-rtde。适配器现在通过受限 AST 解析读取校准数据，并在自己的 geometry.py 中进行经等价测试的纯几何计算；不执行上游模块，不导入 mido/sounddevice/soundfile，不初始化 PortAudio。mock、环境探针（probe）和静态预检仍不会导入真实驱动。

不自动安装任何包。先读取现有环境版本，若确有缺项，再针对实际 macOS、Python 与架构准备隔离安装。不能使用先前 Windows 安装计划。当前官方 `ur-rtde 1.6.5` 发布页支持矩阵将 Apple Silicon wheel 标为未提供，实际下载列表没有 macosx wheel；即使是 Intel 也应核对实际可用文件，不能只凭支持矩阵承诺一条 pip 命令成功。[官方发布信息](https://pypi.org/project/ur-rtde/1.6.5/) 不自动启用 Rosetta、不用 sudo/Homebrew、不安装编译工具链或交叉驱动，也不把 Windows venv 复制过来。

如果现有环境缺包，先确认架构与 Python，再仅在新的隔离环境讨论下面的**解析计划（resolution plan）**；本任务没有执行它。NumPy 2.2.6 的官方发布包含 Intel 与 Apple Silicon 对应文件。[NumPy 发布页](https://pypi.org/project/numpy/2.2.6/) 以下命令仅解析，不安装；ur-rtde 没有匹配 wheel 时必须停止，不自动转为编译或修改既有环境：

```sh
# Use the separately selected, new isolated interpreter only if needed:
cello_candidate_python='/absolute/path/to/new/isolated/environment/bin/python'
"$cello_candidate_python" -m pip --isolated install --dry-run --only-binary=:all: \
  --index-url https://pypi.org/simple --report runs/mac-dependency-plan.json \
  'numpy==2.2.6' 'ur-rtde==1.6.5'
```

版本固定只是候选计划，不表示这些版本已在用户 Mac 或机械臂上验证。已有可用版本优先保留；不要为了跟这个候选组合一致而升级现有驱动。

## 2. 同机 mock 联调（same-host integration）

两端必须在同一台 Mac，使用同一会话（session）与时钟域（clock domain）。开始前先确认 `mac-environment.json` 中 `shared_clock_bracket_ok=true`。该检查只采样父/子进程的时钟区间，不能证明硬实时保证。

窗口 A（receiver）：

```sh
mkdir -p runs
cello_session="$("$cello_python" -c 'import uuid; print(uuid.uuid4())')"
cello_domain="local-mac-$cello_session"
printf 'session=%s\ndomain=%s\n' "$cello_session" "$cello_domain"
"$cello_python" -B -m cello_adapter --backend mock \
  --session "$cello_session" --clock-domain "$cello_domain" \
  --listen 8766 --arm --journal runs/journal --log "runs/$cello_session.jsonl"
```

窗口 B：进入同一解压目录、设置同一个 `cello_python`，复制窗口 A 的 session/domain，然后运行合成时钟（synthetic clock）：

```sh
cello_session='COPY_SESSION_FROM_WINDOW_A'
cello_domain='COPY_DOMAIN_FROM_WINDOW_A'
"$cello_python" -B -m cello_adapter.demo --session "$cello_session" \
  --clock-domain "$cello_domain" --port 8766 --bpm 60
```

60 BPM 仅为显式测试参数；预期 12 弓、48 拍、约 48 秒后结束。每次重启用新 session 和新日志。第二次可加 `--tempo-change` 验证连续变速；不要重复使用旧 session。连接 conductor 时以其真实实时 CLOCK/STOP 发送端替代 demo，接口见 INTERFACE_CONTRACT.md。不要把视频 PTS 或另一台电脑的单调时间当作本机 `source_monotonic_ns`。

等待实现（waiting）：接收端循环 `time.sleep(0.005)`、合成发送端 `time.sleep(0.02)`；硬件流由驱动 `initPeriod/waitPeriod` 等待。适配器没有 ctypes/kernel32 或 Windows 私有等待计时器，因此不存在 Mac 导入 Windows API 的路径。Python 的 Unix 等待实现可以使用 nanosleep/select；实际唤醒可能晚于请求。[Python time 文档](https://docs.python.org/3.11/library/time.html#time.sleep) Mac 的延迟、负载和驱动节奏必须实测，不能沿用 Windows 的毫秒数据。conductor 若含平台专用等待代码，由 conductor 组件单独隔离。

## 3. 指向现有校准，生成未批准候选（unapproved candidate）

优先指向 Mac 上现有、当前安装对应的项目；不要把公开仓库旧坐标当有效校准。工具需要已有源码中的三个数据/来源文件，详见 SOURCE_NOTES.md。

```sh
cello_repo='/absolute/path/to/existing/robotic-cellist'
"$cello_python" -B tools/prepare_candidate_inventory.py \
  --repo-root "$cello_repo" --output-dir runs/mac-candidate
"$cello_python" -B -m cello_adapter.preflight_check \
  --review runs/mac-candidate/hardware-review.CANDIDATE-UNAPPROVED.json \
  --report runs/mac-preflight.json
```

已有经过审查的运行范围 JSON 可额外通过 `--operating-profile '/actual/path/profile.json'` 指定。不会自动选择、编造或批准物理范围。候选生成器只读源文件、自动计算路径/版本/哈希和四项来源参数，五类现场确认仍为 false，robot_ip 仍空；预检应该返回 BLOCKED（退出码 2）。如果源文件不兼容，输出确切缺项而不执行它们。

模板相对路径与 pins 均以**清单文件所在目录**解析，支持含空格路径、绝对路径和 `~`；不依赖启动时 cwd。CLI 的日志/清单参数可指定任意本机路径。生成的候选含本机绝对路径，必须在 Mac 重新生成，不携带 Windows 候选。批准记录和候选不同；哈希变化需要重新核对，工具拒绝覆盖已审查记录。移动整个包后，重新生成本机候选。

## 4. 目标机阶段（target-machine stages）

1. 确认 Mac 芯片、现有解释器/驱动版本；运行无设备环境探针与软件测试。
2. 同机 conductor + mock 联调：实际视频阶段、变速、Stop、超时、完整合成 48 拍；记录 Mac 的观测延迟。
3. 提供有效 A 弦校准与一致安装、空走/接触和已有范围、安装/测力/控制归属、有人值守/独立停止/本次授权、适用现场规则。空 A3 音高替换仍需用户明确接受。
4. 现场授权和预检齐备后，才安排真实网络连接、安装读回、停止路径验证及空走；实际接触另按现场程序进行。本任务未执行这些阶段，不包含可直接运动的批准配置。

`stop_wait_s` 仅约束软件对驱动所有者线程的等待；超时为 pending/incomplete，退出码 4。驱动返回也仅为 physical stop unverified；不以软件 ACK 或线程返回证明机械臂已停。
