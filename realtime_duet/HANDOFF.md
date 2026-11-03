# 0.3.8 实验版交接

当前范围只到 conductor → beat，见 [README.md](README.md) 和 [VALIDATION.md](VALIDATION.md)。原始视频和个人日志不随代码分发；重放仅使用已授权本地文件。

修复涉及 clock.py 的 arming 幂等、count_in.py 的观测准备拍与一致性、body_reference.py 的有界同手腕运动保持、vision.py 的短手指间断和 Stop 证据生命周期。音频实现未改，仍由实际发布事件触发 click，独立 callback 和有界队列，无麦克风。

下一步：

- 参考三拍仍漏 Stop：手形间断，重捕获后 approach 已为空，确认/上抬证据不能同时满足。当前日志未记录 approach 清空的直接原因；pose 运动噪声只是待检验解释。不要为此片段无依据放宽手形或稳定性要求。
- 一段旧片在高位捏合后触发 Stop，但随后继续指挥；意图未确认，保留为疑似假 Stop。
- 多个转折/跨线候选仍可能在去抖边界改变接受数和 tempo；时间抖动测试不能只看能否 Start/Stop。
- 当前独立视觉标签只有少量运动学转折点，需要音乐家确认末拍语义及更多视角/人物样本。

```sh
cd realtime_duet/conductor
../.venv/bin/python -B -m unittest discover -s tests -v
```

成对脚本支持 --video、--meter、--hand、--baseline、--output；--recorded-run 可使用本项目原始相机帧时间索引，否则使用视频 PTS。仅输出数值/事件，不存关键点数组。共享推理、固定 33 ms 模拟可用时间和 ±4 ms 抖动用于状态机诊断，不是声学或相机实时延迟。

后续使用独立候选及新输出目录保留失败证据，不在真人试验中静默改版重启。当前版本继续标为 experimental；无本次机械臂、完整旋律或接触音质结论。
