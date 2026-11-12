"""Wire schema verified against EcatV2_Master and AIM TaskEditor.

Field tags are part of the ABI: yaml-cpp dispatches on the tag, not the value.
SDO serialization order is specified by the backend's build_buf calls.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Field:
    key: str
    tag: str
    default: Any
    label: str
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None


def u8(key, default, label, choices=(), minimum=0, maximum=255):
    return Field(key, "uint8_t", default, label, tuple(choices), minimum, maximum)


def u16(key, default, label, minimum=0, maximum=65535):
    return Field(key, "uint16_t", default, label, (), minimum, maximum)


def u32(key, default, label, minimum=0, maximum=0xFFFFFFFF):
    return Field(key, "uint32_t", default, label, (), minimum, maximum)


def flt(key, default, label, minimum=None, maximum=None):
    return Field(key, "float", default, label, (), minimum, maximum)


CAN = u8("sdowrite_can_inst", 1, "CAN 端口", ((1, "CAN 1"), (2, "CAN 2")))
PERIOD = u16("sdowrite_control_period", 1, "控制周期 / ms", 1, 65535)
LOST = u8("sdowrite_connection_lost_write_action", 2, "主站断连动作",
          ((1, "1 · 保持最后值"), (2, "2 · 恢复初值"), (0, "0 · 未指定 / 不重置")))
READ_LOST = u8("conf_connection_lost_read_action", 2, "断连反馈动作",
               ((1, "1 · 保持最后值"), (2, "2 · 发布空消息"), (0, "0 · 未指定 / 不重置")))


@dataclass(frozen=True)
class TaskSpec:
    id: int
    name: str
    category: str
    read_type: str = ""
    write_type: str = ""
    read_bytes: int = 0
    write_bytes: int = 0
    fields: tuple = ()
    note: str = ""


LK_MODES = (
    (1, "开环电流"), (2, "转矩"), (3, "速度 / 转矩限制"),
    (4, "多圈位置"), (5, "多圈位置 / 速度限制"), (6, "单圈位置"),
    (7, "单圈位置 / 速度限制"), (8, "广播电流 · 四电机"),
)
LK_MESSAGES = {
    1: "WriteLkMotorOpenloopControl", 2: "WriteLkMotorTorqueControl",
    3: "WriteLkMotorSpeedControlWithTorqueLimit", 4: "WriteLkMotorMultiRoundPositionControl",
    5: "WriteLkMotorMultiRoundPositionControlWithSpeedLimit",
    6: "WriteLkMotorSingleRoundPositionControl",
    7: "WriteLkMotorSingleRoundPositionControlWithSpeedLimit", 8: "WriteLkMotorBroadcastCurrentControl",
}
DM_MESSAGES = {1: "WriteDmMotorMITControl", 2: "WriteDmMotorPositionControlWithSpeedLimit", 3: "WriteDmMotorSpeedControl"}


def motor_fields(dd=False):
    fs = ([u8("sdowrite_can_baudrate", 1, "CAN 波特率", ((1, "1 Mbit/s"), (2, "500 kbit/s")))] if dd else [])
    fs += [LOST, PERIOD, u32("sdowrite_can_packet_id", 0x200 if not dd else 0x32,
                           "控制帧 CAN ID", maximum=0x7FF)]
    for i in range(1, 5):
        fs.append(u32(f"sdowrite_motor{i}_can_id", (0x201 if not dd else 0x97) if i == 1 else 0,
                      f"电机 {i} 反馈 CAN ID（0 = 禁用）", maximum=0x7FF))
    fs.append(CAN)
    for i in range(1, 5):
        fs.append(u8(f"sdowrite_motor{i}_control_type", 1, f"电机 {i} 控制模式",
                     ((1, "电流 / 开环"), (2, "速度"), (3, "单圈位置")) if not dd else
                     ((1, "开环电压"), (2, "闭环电流"), (3, "速度"), (4, "单圈位置"))))
        if not dd:
            for loop, name in (("speed", "速度"), ("angle", "位置")):
                for suffix, label in (("kp", "Kp"), ("ki", "Ki"), ("kd", "Kd"),
                                      ("max_out", "输出限制"), ("max_iout", "积分限制")):
                    fs.append(flt(f"sdowrite_motor{i}_{loop}_pid_{suffix}", 0.0,
                                  f"电机 {i} · {name} PID {label}", 0.0))
    return tuple(fs)


SPECS = {s.id: s for s in (
    TaskSpec(1, "DJI 遥控器 · DBUS", "遥控器", "ReadDJIRC", read_bytes=19, fields=(READ_LOST,), note="占用固定 DBUS 接口，与 SBUS 互斥。"),
    TaskSpec(2, "LK Tech 电机", "执行器", "ReadLkMotor", read_bytes=8, write_bytes=3,
             fields=(LOST, PERIOD, u8("sdowrite_control_type", 1, "控制模式", LK_MODES),
                     u32("sdowrite_can_packet_id", 0x141, "控制 CAN ID", maximum=0x7FF), CAN, READ_LOST),
             note="位置角度单位为 0.01°；速度命令单位为 0.01°/s，以消息定义为准。"),
    TaskSpec(3, "HIPNUC IMU · CAN", "传感器", "sensor_msgs/msg/Imu", read_bytes=21,
             fields=(CAN, u32("sdowrite_packet1_id", 1, "姿态帧 CAN ID", maximum=0x7FF),
                     u32("sdowrite_packet2_id", 2, "角速度帧 CAN ID", maximum=0x7FF),
                     u32("sdowrite_packet3_id", 3, "加速度帧 CAN ID", maximum=0x7FF),
                     Field("conf_frame_name", "std::string", "imu_link", "坐标系 frame_id"), READ_LOST)),
    TaskSpec(4, "DSHOT600 · 四通道", "执行器", write_type="WriteDSHOT", write_bytes=8,
             fields=(LOST, u8("sdowrite_dshot_id", 2, "定时器端口", ((1, "Port 1"), (2, "Port 2"))),
                     u16("sdowrite_init_value", 0, "初始值 / 停止值", 0, 2047)),
             note="0 为停止；1–47 是 ESC 特殊命令，调试界面禁止输出。48–2047 为油门。"),
    TaskSpec(5, "DJI 电机组 · 四电机", "执行器", "ReadDJIMotor", "WriteDJIMotor", fields=motor_fields(),
             note="反馈 CAN ID = 0 的槽位不占用 PDO。PID 参数在从站启动时应用。"),
    TaskSpec(6, "板载 PWM · 四通道", "执行器", write_type="WriteOnBoardPWM", write_bytes=8,
             fields=(LOST, u8("sdowrite_port_id", 2, "定时器端口", ((1, "Port 1"), (2, "Port 2"))),
                     u16("sdowrite_pwm_period", 20000, "PWM 周期 / μs", 1),
                     u16("sdowrite_init_value", 1500, "初始 / 释放脉宽 / μs")),
             note="释放按键时恢复配置初值。1500 μs 仅为示例，不代表所有执行器的停止值。"),
    TaskSpec(7, "外置 PWM · UART", "执行器", write_type="WriteExternalPWM",
             fields=(LOST, u8("sdowrite_uart_id", 4, "UART 端口", ((1, "USART 1"), (4, "UART 4"))),
                     u16("sdowrite_pwm_period", 20000, "PWM 周期 / μs", 1),
                     u8("sdowrite_channel_num", 4, "通道数", minimum=1, maximum=16),
                     u16("sdowrite_init_value", 1500, "初始 / 释放脉宽 / μs"))),
    TaskSpec(8, "MS5837-30BA · I²C", "传感器", "ReadMS5837BA30", read_bytes=9,
             fields=(u8("sdowrite_i2c_id", 3, "I²C 端口", ((3, "I²C 3"),)),
                     u8("sdowrite_osr_id", 5, "过采样 OSR", tuple((i, str(128 * 2**i)) for i in range(1, 7))), READ_LOST)),
    TaskSpec(10, "PMU · UAVCAN", "传感器", "ReadCANPMU", read_bytes=7, fields=(READ_LOST,), note="固定使用 CAN 2，同一总线只支持一个 PMU。"),
    TaskSpec(11, "SBUS 遥控器 · DBUS", "遥控器", "ReadSBUSRC", read_bytes=24, fields=(READ_LOST,), note="占用固定 DBUS 接口，与 DJI 遥控器互斥。"),
    TaskSpec(12, "DM 达妙电机", "执行器", "ReadDmMotor", read_bytes=9, write_bytes=9,
             fields=(LOST, PERIOD, u16("sdowrite_can_id", 1, "电机控制 CAN ID", 1, 0x7FF),
                     u16("sdowrite_master_id", 17, "电机反馈 Master ID", 1, 0x7FF), CAN,
                     u8("sdowrite_control_type", 1, "控制模式", ((1, "MIT"), (2, "位置 / 速度限制"), (3, "速度"))),
                     flt("conf_pmax", 3.1415927, "P Max / rad", 0.0001),
                     flt("conf_vmax", 30.0, "V Max / rad/s", 0.0001),
                     flt("conf_tmax", 10.0, "T Max / N·m", 0.0001), READ_LOST),
             note="P/V/T Max 必须与电机固件映射一致；切换模式后需重启主站应用配置。"),
    TaskSpec(13, "超级电容 · CAN", "执行器", "ReadSuperCap", "WriteSuperCap", read_bytes=7, write_bytes=4,
             fields=(LOST, CAN, u32("sdowrite_chassis_to_cap_id", 0x210, "控制 CAN ID", maximum=0x7FF),
                     u32("sdowrite_cap_to_chassis_id", 0x211, "反馈 CAN ID", maximum=0x7FF), READ_LOST)),
    TaskSpec(14, "VT13 遥控器", "遥控器", "ReadVT13RC", read_bytes=18, fields=(READ_LOST,), note="串口接线请按 VT13 从站固件核对。"),
    TaskSpec(15, "DD 电机组 · 四电机", "执行器", "ReadDDMotor", "WriteDDMotor", fields=motor_fields(True),
             note="波特率必须与电机一致。电压 / 电流 / 速度 / 位置目标单位以对应 DD 型号手册为准。"),
)}


def active_fields(task):
    fs = list(SPECS[task.type_id].fields)
    if task.type_id == 2 and task.values.get("sdowrite_control_type") == 8:
        fs = [f for f in fs if f.key != "sdowrite_can_packet_id"]
    if task.type_id in (5, 15):
        result = []
        import re
        for f in fs:
            match = re.match(r"sdowrite_motor([1-4])_(.+)", f.key)
            if match and match[2] != "can_id":
                motor, suffix = match.groups()
                if not task.values.get(f"sdowrite_motor{motor}_can_id", 0):
                    continue
                mode = task.values.get(f"sdowrite_motor{motor}_control_type", 1)
                if suffix.startswith("speed_pid") and mode < 2:
                    continue
                if suffix.startswith("angle_pid") and mode < 3:
                    continue
            result.append(f)
        fs = result
    return fs


def pdo_size(task):
    spec, v = SPECS[task.type_id], task.values
    if task.type_id in (5, 15):
        n = sum(bool(v.get(f"sdowrite_motor{i}_can_id")) for i in range(1, 5))
        return n * 9, n * 3
    if task.type_id == 2:
        mode = v["sdowrite_control_type"]
        return (32 if mode == 8 else 8), {1: 3, 2: 3, 3: 7, 4: 5, 5: 7, 6: 6, 7: 8, 8: 8}.get(mode, 0)
    if task.type_id == 12:
        return 9, 5 if v["sdowrite_control_type"] == 3 else 9
    if task.type_id == 7:
        return 0, v["sdowrite_channel_num"] * 2
    return spec.read_bytes, spec.write_bytes


def message_types(task):
    spec = SPECS[task.type_id]
    read, write = spec.read_type, spec.write_type
    if task.type_id == 12:
        write = DM_MESSAGES.get(task.values["sdowrite_control_type"], "")
    if task.type_id == 2:
        mode = task.values["sdowrite_control_type"]
        write = LK_MESSAGES.get(mode, "")
        read = "ReadLkMotorMulti" if mode == 8 else "ReadLkMotor"
    qualify = lambda s: s if not s or "/" in s else "custom_msgs/msg/" + s
    return qualify(read), qualify(write)


def resources(task):
    """Return (resource, identifier, exclusive) allocations."""
    v, t = task.values, task.type_id
    if t in (1, 11):
        return [("DBUS", "receiver", True)]
    if t == 14:
        return [("VT13", "receiver", True)]
    if t in (4, 6):
        return [(f"PWM/DSHOT 端口 {v.get('sdowrite_dshot_id', v.get('sdowrite_port_id'))}", "4 通道", True)]
    if t == 7:
        return [(f"UART {v['sdowrite_uart_id']}", "PWM", True)]
    if t == 8:
        return [(f"I²C {v['sdowrite_i2c_id']}", "0x76", True)]
    if t == 10:
        return [("CAN 2", "UAVCAN PMU", False)]
    can = f"CAN {v.get('sdowrite_can_inst', 1)}"
    if t == 2:
        ids = [0x280, 0x141, 0x142, 0x143, 0x144] if v['sdowrite_control_type'] == 8 else [v['sdowrite_can_packet_id']]
    elif t == 12:
        ids = [v['sdowrite_can_id'], v['sdowrite_master_id']]
    elif t in (5, 15):
        ids = [v['sdowrite_can_packet_id']] + [v[f'sdowrite_motor{i}_can_id'] for i in range(1, 5) if v[f'sdowrite_motor{i}_can_id']]
    elif t == 3:
        ids = [v[f'sdowrite_packet{i}_id'] for i in range(1, 4)]
    elif t == 13:
        ids = [v['sdowrite_chassis_to_cap_id'], v['sdowrite_cap_to_chassis_id']]
    else:
        return []
    return [(can, f"0x{i:03X}", False) for i in ids]
