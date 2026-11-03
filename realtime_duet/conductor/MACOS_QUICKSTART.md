# Mac conductor → beat 实验版

以 [项目 README](../README.md) 为安装和操作入口，[验证状态](../VALIDATION.md) 列出未通过项。使用 macOS 13+、原生 CPython 3.11 和独立 venv。

先跑软件回归或授权本地视频重放。相机须在设置界面明确点击开启，默认自动下巴/身体参考，不要求手动校准。选择固定身体左/右手与 3/4 拍；视频日志只留本机。

0.3.8 为 experimental，不能依赖自动 Stop 作为唯一停止机制。当前入口只启动 conductor，不启动 cello 或机械臂，不启用麦克风。
