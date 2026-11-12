# v0.3.1 Launch 排查与修复

本次检查的工作空间为 `/home/hby/EGC_test`，真实从站 SN 为 `2818120`，网卡 `enp5s0f0`，实时 CPU 6，其余 CPU 0–5。

## 日志确认的情况

- 2026-11-12 15:09:11 启动时网卡已成功打开，并发现 Large PDO 从站和固件 swrev 8；随后报 `get Key not found: sn2818120_sdo_len`。当时加载的安装配置仍是首次获取 SN 的模板。
- 15:09:48 编译已把用户配置写入安装目录。当前源 YAML 和安装 YAML 完全一致，包含 `sn2818120`、3 个任务、`sdo_len=21`。
- 15:10:06 的进程未打印主站初始化信息，随后用户多次 Ctrl+C，launch 升级到 SIGTERM，进程以 -6 退出。仅凭这份日志无法确定初始化停顿的原因。普通用户和管理员的独立 ROS 测试节点均能正常创建和退出，没有据此改动全局 DDS 配置或删除共享内存。
- GUI 日志还记录了 `TypeError: cannot unpack non-iterable bool object`：Ubuntu 上 PyQt5 的两参数 `QProcess.startDetached` 返回布尔值，原代码却把它解包为两个值。终端进程可能已打开，GUI 回调仍会异常。

原始错误和独立 ROS 检查结果保存在 [launch-diagnostics.json](../artifacts/launch-diagnostics.json)。

## 软件修复

1. 打开终端兼容布尔值及 `(started, pid)` 两种 Qt 返回形式；启动失败时显示明确提示。
2. 打开 root 终端前核对源目录和安装目录的 `config/config.yaml` 与 `launch/bringup.launch.py`。修改 SN、任务、CPU 或网卡后若尚未编译，不会继续打开加载旧文件的终端。后台 root-shell 入口同样执行该检查。
3. root 终端设置 `ROS_LOG_DIR=<工作空间>/.ecat-groundstation/ros-logs/`，采用普通用户可读取的日志权限并打印目录位置。没有修改 `/root/.bashrc` 或系统全局 ROS 设置。

## 操作顺序

配置任务 → 应用任务修改 → colcon build（普通用户）→ 等待安装配置核对成功 → 打开新的 root 终端 → 手动执行 `ros2 launch soem_bringup bringup.launch.py`。

首次模板仅用于读取真实 SN，出现该 SN 对应配置缺失时，应回到任务配置、应用并编译完整配置。不要把启动进程退出码 0 当作 EtherCAT 初始化成功，应查看 `Initialization succeeded` 和 `All ... slave(s) ready`。

## 自动验证

123 项测试通过，新增测试覆盖 Qt 终端返回值、终端启动失败提示、未安装的新 YAML/launch 阻止启动、编译后允许启动，以及 root 日志目录与临时启动文件清理。

## 实机短时启动验证

2026-11-12 15:21 使用用户当前的三任务配置（DJI RC、CAN IMU、DSHOT 初值 0）实际启动约 9 秒，没有发送 GUI 控制指令。日志确认：

- 真实 SN `2818120`，产品代码 4，固件 swrev 8，SDO 长度 21 B；
- 所有从站进入 OP；`Initialization succeeded`；
- `Thread priority set to 49 with SCHED_FIFO`；
- SDO 已收到并确认，`All 1 slave(s) ready, system started`；
- SIGINT 后发送退出复位、停止数据线程与 SDO worker，正常退出，launch 返回码 0。

完整输出见 [launch-smoke-check.log](../artifacts/launch-smoke-check.log)。15:10 的初始化停顿在本次验证中没有复现，不能据此断言其历史原因。部分内核线程迁移警告仍存在，未阻止本次启动；没有为消除这些警告改动内核或隔离配置。
