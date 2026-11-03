# 软件验证（software validation）

2026-11-03，迁移到隔离目录后完成：

| 项目 | 结果 | 边界 |
| --- | --- | --- |
| conductor unittest | 68/68 通过 | 原 64 项加 4 项 OpenCV 预检回归；fake camera/输入，不开设备 |
| cello unittest | 83 通过、1 跳过 | 几何与 fake-driver；跳过需要未携带上游原文件的等价检查 |
| 移目录 TCP mock | 通过 | 同一解释器，127.0.0.1:18766，合成 60 BPM、一次起弓、明确 Stop ACK、退出码 0 |
| MediaPipe 模型资源 | 八个文件齐全 | 只检查已安装包文件，不加载模型 |
| macOS arm64 依赖解析 | 26 固定包成功 | CPython 3.11、macOS 13、官方 PyPI binary-only dry-run |
| macOS x86_64 依赖解析 | 26 固定包成功 | 同上；清单记录两架构 wheel 的官方 SHA-256 |
| 原控制代码 | 23 个运行模块逐字节一致 | conductor_rt 与 cello_adapter；预检脚本是唯一已有 Python 文件的打包修正 |

本地测试环境：Windows / CPython 3.10.11 / NumPy 1.26.4 / OpenCV 4.11.0.86。没有安装交叉平台 wheel、运行摄像头、初始化模型或连接机器人。已有环境含两套 OpenCV distribution，因此不把它当作新安装模板；新增预检会拒绝该重复安装状态，推荐干净 Mac venv 的单一 contrib distribution。

macOS dry-run 使用 Windows 上的 pip 23.0.1 指定目标平台和 Python/ABI；这是依赖解析与文件存在性检查，不是目标机安装、模型推理、摄像头权限或实时性能验证。安装后在 Mac 再执行 README 的检查。

原 0.2.3.3 校准/平台保护和 cello 关闭、Stop、超时保护保留。原 12 弓 fake-driver 历史验证不等于机械臂测试，软件 ACK 不等于物理停止。自动 Stop 可靠性、时序片段偏差和 Mac GUI/Retina 映射仍未解决或验证。

仓库基线没有 GitHub Actions workflow；本文件记录本地测试，不声称 CI 通过。迁移发布后检查对应提交的远程 checks，状态以实际结果为准。
