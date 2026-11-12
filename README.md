# Ecat GroundStation

面向 [AIMEtherCAT/EcatV2_Master](https://github.com/AIMEtherCAT/EcatV2_Master) 的本地桌面上位机，采用 Python 3.10 + PyQt5 + ROS 2。界面参考 QGroundControl 的设备工程、参数配置、状态监控与独立调试窗口组织方式。

![设备工作台](artifacts/overview.png)

## 在这台电脑上启动

双击桌面 **Ecat GroundStation**，或执行：

```bash
/home/hby/ecat_groundstation/run.sh
```

默认打开示例工程，处于离线编辑状态，不启动 EtherCAT 主站。右上角选 **离线演示 → 连接**，再到 **执行器调试** 双击任务，可体验独立窗口、命令编辑与模拟反馈。

本机已有 ROS 2 Humble、PyQt5、PyYAML 和构建完成的 `custom_msgs`；`run.sh` 自动加载 `/opt/ros/humble/setup.bash` 和 `/home/hby/Rolling_ZLT/install/setup.bash`。

导入现有机器人配置：

```bash
/home/hby/ecat_groundstation/run.sh \
  --config /home/hby/Rolling_ZLT/src/soem_bringup/config/config.yaml
```

仅查看配置不会发送硬件命令。该本机配置的 `sn2555957` 需要 82 B 反馈 PDO，超出标准版 80 B；导入时提示使用 Large PDO，必须核对实物确为产品代码 4。YAML 本身不含模块型号，JSON 工程会保存所选型号。

## 已实现

| 页面 | 功能 |
|---|---|
| 工作台总览 | 配置拓扑、从站与任务统计、校验状态、PDO/SDO 使用量 |
| 从站与任务 | SN 管理、标准/Large PDO 模型、任务添加/删除/排序、动态参数表单、独立读写话题 |
| 端口与资源 | CAN 总线与帧 ID、PWM/DSHOT 端口、UART、DBUS、I²C 分配和冲突检查 |
| 执行器调试 | 每个任务独立窗口、消息字段与范围校验、按住运行、实时反馈、曲线、窗口采样 CSV |
| ROS 2 与主站 | ROS Domain、网卡、实时 CPU、话题发现、显式启动/停止自有主站进程 |
| 配置与日志 | 带 C++ 类型标签的 YAML 导入/导出、JSON 工程、原子保存、校验报告、进程日志 |

支持的任务：DJI RC、LK 电机（8 种模式）、HIPNUC IMU、DSHOT600、DJI 电机组、板载 PWM、外置 PWM、MS5837-30BA、PMU、SBUS、DM 电机（MIT/位置限速/速度）、超级电容、VT13、DD 电机组（4 种模式）。ADC 任务 9 未被当前主站实现，导入时明确拒绝；SBT908E 等第三方从站暂未做专用编辑器。

配置细节以仓库代码和官方 TaskEditor 为准：控制周期单位为 **ms**；板载 PWM/DSHOT 使用 **Port 1 / Port 2** 编码；断连动作 **1 = 保持最后值，2 = 恢复初值**。PWM 信号周期和脉宽单位为 μs。DM 的 P/V/T Max 必须与电机固件一致。

## 连接真实电机或执行器

1. 在 **从站与任务** 导入/配置实际 SN、型号、端口、CAN ID、控制模式及 ROS 话题。先完成配置校验，再保存工程。
2. 到 **ROS 2 与主站** 核对工作空间、EtherCAT 专用网卡、实时 CPU、非实时 CPU 与 ROS_DOMAIN_ID。不同机器要使用匹配的 Domain 和可达的 DDS 网络。
3. **已有主站运行**：使用与当前主站一致的配置工程，直接在右上角选 **ROS 2 实机 → 连接**，不再启动第二个主站。
4. **没有主站运行**：点击 **导出当前配置并启动主站**。程序将当前配置保存到 `~/.local/share/ecat-groundstation/runs/<时间>/config.yaml`，以该绝对路径启动所选工作空间内的 `soem_backend`。根据主站日志确认各从站完成配置并进入 OP。随后在右上角选择 **ROS 2 实机 → 连接**。
5. 到 **执行器调试** 打开对应任务。确认反馈及时、设备在线和命令话题有匹配订阅者，再设置目标值，勾选 **允许当前执行器输出**，持续按住 **运行**。松开按键或窗口失去焦点会释放输出。

也可手工导出 YAML，使用原有 bringup 加载该文件。配置变更需要重启主站：当前后端没有通用任务热更新服务，GUI 不会把“文件已导出”标成“硬件已应用”。连接中的配置编辑会锁定；先断开连接并停止本软件启动的主站再修改。

主站启动仍依赖原仓库要求的实时内核、CPU 设置和网卡访问权限；本软件不自动使用 sudo、更改系统权限或重建主站。`run.sh` 支持 `ECAT_WORKSPACE` 和 `ECAT_ROS_SETUP` 环境变量以切换工作空间与 ROS 安装。启动整个 GUI 时应加载与主站一致的 `custom_msgs`。

### 输出与停止行为

- ROS 命令发送频率为 20 Hz；MCU 控制周期由 YAML 的 `sdowrite_control_period` 单独决定。GUI 不承担 1 kHz 实时控制环。
- 通信工作线程独立于界面，命令租约为 300 ms；带反馈任务要求反馈年龄不超过 600 ms，且在线/故障标志正常。过期或故障会撤销授权，恢复通信后仍需重新授权，禁止自动重放命令。
- 同一命令话题发现其他发布者时，禁止本上位机控制，避免控制源竞争。
- DM、DJI、DD 和 LK 单电机模式释放时发送 `enable=0`；LK 广播模式发送零电流；超级电容发送禁用；PWM/DSHOT 恢复配置初值。释放消息在短时间内重复发送，ROS 2 的 best effort 通道仍不提供必达保证。
- **PWM 的 1500 μs 只是示例值，可能对应中位、运动或其他动作。请为具体执行器设置正确的释放初值。** DSHOT 1–47 属于特殊命令，调试界面拒绝发送。
- 软件停止不能替代硬件急停。进程被强制杀死、整机失电或网络断开时，禁用消息可能无法送达；原主站会保留最后一次 ROS 命令，必须有独立的主站/固件命令超时机制和硬件急停。当前上位机未修改原主站的看门狗实现。

### 与当前主站实现的兼容限制

源码中的 DJI/DD `init_value()` 固定写四个槽位共 12 B，而正常命令按启用槽位压缩。为避免初始化越界/覆盖，本软件要求：**部分启用的电机组位于全部输出任务末尾，且其起始偏移后仍有至少 12 B 可用空间**。可将 PWM 等输出任务移到该电机组前面。原主站源码保持不变。

配置拓扑来自工程，不是 EtherCAT 总线扫描。ROS 发现和有效反馈不构成固件配置回读验证。没有实现 EEPROM 烧写、自动枚举物理端口或主站任务热更新。

## 架构

```mermaid
flowchart LR
  UI[Qt 桌面界面] --> CFG[工程模型 / 校验 / 类型标签 YAML]
  CFG --> MASTER[原 soem_backend]
  UI --> LEASE[独立通信线程 / 命令租约]
  LEASE <-->|ROS 2 custom_msgs| MASTER
  MASTER <-->|SOEM / EtherCAT| H750[H750 从站]
  H750 <-->|CAN / PWM / UART| MOTOR[电机 / 执行器 / 传感器]
```

`schema.py` 定义任务字段和协议布局；`config.py` 负责配置模型与 YAML；`commands.py` 处理命令字段和范围；`bridge.py` 负责 ROS 与演示模式；`app.py`、`widgets.py` 实现桌面交互。

## 在其他 Ubuntu / ROS 2 Humble 电脑上使用

```bash
sudo apt install python3-pyqt5 python3-yaml python3-pytest
source /opt/ros/humble/setup.bash
source /path/to/ecat_ws/install/setup.bash
cd /path/to/ecat_groundstation
python3 -m ecat_groundstation
```

ROS 的 `rclpy`、`custom_msgs` 通过 ROS 工作空间提供，不通过 pip 安装。也可作为 `ament_python` 包构建：

```bash
colcon build --base-paths /path/to/ecat_groundstation --packages-select ecat_groundstation
source install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

## 验证

```bash
source /opt/ros/humble/setup.bash
source /home/hby/Rolling_ZLT/install/setup.bash
cd /home/hby/ecat_groundstation
QT_QPA_PLATFORM=offscreen python3 -m pytest -q
```

测试覆盖所有支持任务与控制模式的 YAML 往返、容量/资源/话题错误、实际 yaml-cpp 读取、已安装 ROS 消息类型、Qt 任务编辑与窗口操作，以及隔离 DDS Domain 中的 DM 命令/反馈/租约超时/反馈超时。隔离测试不启动 EtherCAT 主站、不打开原始网卡、不接触真实执行器。未做真实电机台架验收。

本机验证结果：**48 项测试通过**；`colcon build` 成功；通过安装后的 `ros2 run` 入口生成界面截图；Python wheel 构建成功。原 EcatV2_Master 工作树无修改。

本机配置导入用于兼容检查，未覆盖原文件。演示文件位于 `examples/`，界面截图位于 `artifacts/`。

## 协议来源

- EcatV2_Master 本机检出的提交：`7c1bd8a3c7f47cc8505470640f8dd7baf5698d76`。
- [主站任务实现](https://github.com/AIMEtherCAT/EcatV2_Master/tree/main/src/soem_wrapper/src/tasks)、[消息定义](https://github.com/AIMEtherCAT/EcatV2_Master/tree/main/src/custom_msgs/msg)。
- [官方 TaskEditor 配置生成器](https://github.com/AIMEtherCAT/TaskEditor/blob/master/src/utils/generate-module-def.js)、[控制周期单位](https://github.com/AIMEtherCAT/TaskEditor/blob/master/src/components/ControlPeriodInput.vue)、[端口编码](https://github.com/AIMEtherCAT/TaskEditor/blob/master/src/components/PortSelector.vue)、[断连动作](https://github.com/AIMEtherCAT/TaskEditor/blob/master/src/components/ConnectionLostActionSelector.vue)。

新代码按 LGPL-2.1-or-later 提供；所带消息字段表来自上述 EcatV2_Master，参见 `THIRD_PARTY_NOTICES.md` 和 `LICENSE`。
