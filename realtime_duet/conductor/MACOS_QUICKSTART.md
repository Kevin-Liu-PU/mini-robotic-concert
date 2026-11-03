# Mac 环境预检（0.2.3.3）

用户已授权目标 Mac 相机 + mock 试用，机器人未授权。此 Windows 执行环境没有打开相机。以下预检不打开相机、不加载视觉模型、不播放音频、不连接机器人。

在解压目录选择已有 Python 环境，不复制 Windows venv，不自动安装依赖或改设备校准：

```sh
conductor_python='/absolute/path/to/existing/vision/environment/bin/python'
cello_python='/absolute/path/to/existing/cello/environment/bin/python'
cello_root='/absolute/path/to/reviewed-e054-cello-package'
"$conductor_python" -B preflight.py
"$conductor_python" -B check_clocks.py --other-python "$cello_python"
"$conductor_python" -B -m unittest discover -s tests -v
```

至少需要 Python 3.10、可用的 `mediapipe.solutions.holistic`、OpenCV（含本地 GUI 支持）和 NumPy。Windows 既有验证环境为 Python 3.10.11、MediaPipe 0.10.21、opencv-python 4.11.0.86、NumPy 1.26.4；这不是用户 Mac 的 wheel 或架构适用保证。优先核对已有环境，缺项交由目标机准备。不能同时混装多个冲突的 OpenCV 包来碰运气。

两进程必须运行在同一台 Mac；`check_clocks` 是父/子单调时间戳的往返区间检查，不保证精确时钟偏移或实时性。不要把 Windows 单调时间或视频 PTS 当成 Mac 时间戳。

环境通过后，按照 [CALIBRATION_QUICKSTART.md](CALIBRATION_QUICKSTART.md) 的两条命令，先进行独立 RAM 校准、释放相机，再用数字文件启动正式 mock。不要复用 Windows 录像的下巴线，也不要同时让预览软件争用相机。

Mac 终端命令为 `prepare`、`stop`、`quit` 后按 Enter。count-in 模式的 `start` 无效，不能跳过预备小节。状态每秒刷新两次，显示的是 conductor 软件状态，不是机械臂物理状态。Mac/其他系统等待使用 `time.sleep`，未修改系统设置；Windows 数字不能替代 Mac 现场时延测量。
