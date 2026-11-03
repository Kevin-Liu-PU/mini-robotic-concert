# 0.2.3.3：现场下巴线校准与相机 mock

此包是独立补充，不修改 0.2.3.1；拍位、tempo、Start/count-in、Stop、协议算法未改。新增独立内存预览、数字校准文件、相机启动前校准门控，以及启动器中的低频文字状态。Mac 相机试用已获用户授权，但本轮仍只进行了假相机/鼠标/键盘和其他纯软件测试，没有打开 Windows 或 Mac 相机。

## 两条实际命令

先在解压目录设置已有环境路径：`conductor_python` 为能导入 OpenCV/MediaPipe 的视觉 Python，`cello_python` 为现有大提琴 Python，`cello_root` 为已复核 e054 大提琴软件包目录。沿用已有预检，不自动安装包。

**第一条：只校准，不启动音乐或机器人。**

```sh
"$conductor_python" -B calibrate_chin.py --camera 0 --width 640 --output ./chin-camera0.json
```

保持实际指挥姿势与构图，点击预览中下巴的最低点。绿色水平线应穿过该点，窗口会显示数值；可重新点选。确认线已显示且位置正确后按 Enter，仅保存数字 JSON，然后释放相机、关闭窗口。Esc 或关闭窗口取消，不保存任何文件。越界点击无效，没有有效选择时 Enter 不保存。已有同名文件不会覆盖，重新校准需选新文件名。

预览仅在 RAM，使用同一相机编号、相同平台后端（Mac 为 AVFoundation）、相同相机请求参数和同一个 `prepare_frame` 函数：等比例缩放到宽 640，再左右镜像，不裁剪。无模型、无视频/截图/关键点文件、无音频、无 TCP 或机器人连接。实际 Mac GUI、Retina 点击映射与相机权限仍需在目标机核实；必须看见绿色线确实位于下巴，不能盲按 Enter。

**第二条：释放校准相机后，用同一文件启动相机 + mock。**

```sh
"$conductor_python" -B launch_rehearsal.py \
  --receiver-root "$cello_root" --receiver-python "$cello_python" \
  --sender-python "$conductor_python" --camera 0 --width 640 \
  --calibration ./chin-camera0.json --initial-bpm 90 --startup count-in --interactive
```

这里 90 是显式的数拍前视觉去重提示，不是从整个录像反算的音乐速度，也不是用户已选择的目标速度。音乐初速来自已完成的四拍数拍。终端输入 `prepare` 后按 Enter，再做完整的预备 1、2、3、4；第五个新鲜合格真实下拍才开始曲目。`stop` 后按 Enter 停止，`quit` 后按 Enter 退出。count-in 模式不允许 `start` 命令跳过数拍。自动 Stop 仍不可靠，临时操作方式的接受由根任务与用户确认；本工具没有机器人执行模式。

也可以明确给出 `--chin-y` 数值替代文件，但不能同时给两者。相机模式缺少二者时，在创建运行目录或启动子进程/摄像头之前退出，绝不沿用旧录像值。录像模式的既有 `--chin-y` 用法不变；旧默认仅保留在录像 launcher 路径，直接 runner 仍需显式校准参数。

## JSON 单位与一致性

文件仅含九个数字：

| 字段 | 含义 |
|---|---|
| `chin_y` | 点击的预览像素 y / 预览图像高度；顶部 0、底部 1，必须严格在两者之间 |
| `camera_index` | 本机相机编号 |
| `camera_backend` | OpenCV 平台后端编号 |
| `capture_width`, `capture_height` | 实际读取的原始帧尺寸，像素 |
| `processing_width`, `preview_height` | 等比例缩放后的尺寸，像素 |
| `mirror_horizontal` | 1，表示水平镜像 |
| `schema_version` | 1 |

启动器在创建运行目录或启动任何子进程之前校验 JSON、有限数值、相机编号、处理宽度、比例及当前平台后端；相机 reader 在打开设备前再用已安装 OpenCV 的常量复核后端。实际帧尺寸只能在读取首帧后得知：若与文件不同，立即拒绝并释放相机，不送往推理队列；运行中尺寸改变也同样拒绝。不会自行缩放或猜测旧校准的有效性。

数字校验不能检测座位/头部姿势改变、镜头物理移动，或系统把相机编号重新分配给另一设备；发生这些变化需重新现场校准。`chin_y` 是固定水平线，不是自动人脸跟踪。

## 实时文字状态

正式试用无视频预览窗口。启动器的独立只读线程每秒两次显示 READY/RUNNING/STOPPED/ENDED、BPM、小节/拍位、连续 phase 和 TRACKED/PREDICTED/UNRELIABLE；数据超过一秒未更新会显示 STALE。`BPM` 是速度估计，`rate` 是包含未来相位校正的瞬时速度。count-in 尚未开始音乐时明确显示 `hint`，不冒充测得速度。

该线程只读取已有时钟日志末尾最多 16 KB；它不修改或阻塞子进程音乐时钟，不增加音频或录像。文字刷新可能与终端输入交错，但命令仍需整行加 Enter。状态表示 conductor 软件时钟，不代表机械臂物理状态或每拍检测全部正确。

相机正式运行仍不保存图像、逐帧关键点、手形数值或身体/指尖坐标，只保留时序、性能和事件日志。软件 ACK 不是物理停止。

64 项软件测试通过：原有 43 项保障保留（相机正例补上现在必需的显式校准值），新增 14 项校准/门控/释放测试、5 项终端状态测试及 2 项平台预检测试。另验证共享缩放/镜像结果与原 runner 完全相同。本轮没有重跑模型或真实相机，因此不新增 Mac 实时性或音乐准确率通过声明；原有约 0.2 秒新片起弓偏差仍保留。
