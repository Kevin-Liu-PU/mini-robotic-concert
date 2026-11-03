# 模型与依赖（models and dependencies）

本目录独立于仓库根目录的旧 conductor。安装本目录的 `requirements-macos.txt`，不用根目录的 `download_model.py` 或其他依赖清单。

新建环境的目标（target）：原生 CPython 3.11、macOS 13+、Apple Silicon arm64 或 Intel x86_64。两端共享一个环境。Windows 软件测试使用 CPython 3.10.11；Mac 相机、GUI、性能尚未验证。已有环境可先预检，不要将 Windows venv 复制到 Mac。

直接依赖（direct dependencies）固定为 MediaPipe 0.10.21、OpenCV contrib 4.11.0.86、NumPy 1.26.4。MediaPipe 使用旧 `solutions.holistic` API，不能盲目升级到已移除该 API 的版本。只安装一个提供 cv2 的 GUI 包；headless 不能显示点选校准窗口。迁移中仅修正预检对 OpenCV distribution 名称的识别，控制算法不变。

传递依赖（transitive dependencies）在清单内固定版本。JAX/JAXlib 固定 0.4.38，使 Python 3.11 的 Intel 与 Apple Silicon 均有官方 wheel；已有 Windows 环境用 0.6.2，JAX 不参与本项目的运弓时钟或推理调用。MediaPipe 元数据要求 sounddevice 等传递依赖；本项目不提供录音、播放或音频控制入口。不要把“依赖存在”理解为启用音频。mock 接收端本身只使用标准库，不需要 UR-RTDE。

版本和 wheel 平台已从官方 PyPI 元数据核对：

- [MediaPipe 0.10.21](https://pypi.org/project/mediapipe/0.10.21/)：CPython 3.11 macOS universal2/x86_64。
- [OpenCV contrib 4.11.0.86](https://pypi.org/project/opencv-contrib-python/4.11.0.86/)：macOS 13+ arm64/x86_64；只选一种 OpenCV distribution。
- [NumPy 1.26.4](https://pypi.org/project/numpy/1.26.4/) 与 [JAXlib 0.4.38](https://pypi.org/project/jaxlib/0.4.38/) 提供相应架构的 CPython 3.11 wheel。

安装命令限定官方 PyPI 与二进制包（binary wheels）。26 个固定版本已分别通过 macOS 13 / CPython 3.11 的 arm64 和 x86_64 跨平台 dry-run 解析；清单包含对应官方 wheel 的 SHA-256，安装时使用 `--require-hashes`。该清单专用于上述 Mac 平台，不用于 Windows 安装。若系统、Python 或架构不匹配，停止并报告错误，不自动转为系统驱动安装或源码编译。依赖解析成功不代表 Mac 运行验证成功。

## 官方模型获取（official model acquisition）

执行 README 的 pip 安装会从官方 PyPI 获取 MediaPipe 0.10.21 wheel；Holistic 图和模型资源随包安装。此实现使用 `model_complexity=1`、`refine_face_landmarks=False`，不使用仓库根目录旧程序的 hand_landmarker.task。

模型文件不复制到 Git。运行 `python checks/check_models.py` 验证八个所需资源存在；该检查只读取包元数据和文件大小，不导入 MediaPipe、不加载模型、不开设备。

官方 [Holistic 源码](https://github.com/google-ai-edge/mediapipe/blob/v0.10.21/mediapipe/python/solutions/holistic.py) 只为 complexity 0/2 请求额外 pose 模型；本项目固定为 1。若检查发现文件缺失，应在隔离环境重新安装同一官方 wheel，然后复查，不从不明链接下载权重。MediaPipe/模型保留上游许可证（license），参见其 [官方许可证](https://github.com/google-ai-edge/mediapipe/blob/v0.10.21/LICENSE)。此迁移不重新分发模型权重。

真实硬件部署（hardware deployment）另需目标 Mac 上有效的 UR-RTDE 环境、机器人安装/校准和现场授权。本次安装清单不安装驱动，硬件入口仍默认禁止。
