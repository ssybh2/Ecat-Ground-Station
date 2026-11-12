"""Typed command construction shared by the UI and the ROS worker."""
from pathlib import Path
import json
import math
from .schema import message_types

MESSAGE_FIELDS = json.loads((Path(__file__).parent / 'message_fields.json').read_text())


def verify_message_class(task, message_class):
    """Reject a stale custom_msgs overlay before any commands can be sent."""
    aliases = {'float': 'float32', 'double': 'float64', 'sequence<uint16>': 'uint16[]'}
    actual = {key: aliases.get(typ, typ) for key, typ in message_class.get_fields_and_field_types().items()}
    if actual != fields_for(task):
        raise ValueError('当前 custom_msgs 与上位机命令定义不一致，请加载匹配的工作空间')


def fields_for(task):
    _, type_name = message_types(task)
    return MESSAGE_FIELDS.get(type_name.rsplit('/', 1)[-1], {})


def limits_for(task, key, ros_type):
    v = task.values
    if key == 'direction':
        return 0, 1
    if task.type_id == 2:
        if key == 'angle' and v['sdowrite_control_type'] in (6, 7):
            return 0, 35999
        if key == 'torque' and v['sdowrite_control_type'] == 1:
            return -850, 850
        if key == 'torque' or key == 'torque_limit' or key.endswith('_cmd'):
            return -2048, 2048
    if task.type_id == 12:
        bound = {'p_des': v['conf_pmax'], 'position': v['conf_pmax'],
                 'v_des': v['conf_vmax'], 'speed': v['conf_vmax'], 'torque': v['conf_tmax']}.get(key)
        if bound is not None:
            return (0 if key == 'speed' and v['sdowrite_control_type'] == 2 else -bound), bound
        if key == 'kp':
            return 0, 500
        if key == 'kd':
            return 0, 5
    if task.type_id in (4, 6, 7):
        return 0, 2047 if task.type_id == 4 else v['sdowrite_pwm_period']
    if ros_type.startswith('uint'):
        return 0, 2**int(ros_type[4:]) - 1
    if ros_type.startswith('int'):
        bits = int(ros_type[3:])
        return -(2**(bits - 1)), 2**(bits - 1) - 1
    return -1e6, 1e6


def is_enable(key):
    return key == 'enable' or key.endswith('_enable')


def neutral_values(task):
    result = {}
    for key, typ in fields_for(task).items():
        if typ.endswith('[]'):
            result[key] = [task.values.get('sdowrite_init_value', 0)] * task.values.get('sdowrite_channel_num', 4)
        elif task.type_id in (4, 6) and key.startswith('channel'):
            result[key] = task.values['sdowrite_init_value']
        else:
            result[key] = 0.0 if typ.startswith('float') else 0
    return result


def validate_command(task, values):
    fields = fields_for(task)
    if set(values) != set(fields):
        raise ValueError('命令字段与 ROS 消息不一致')
    for key, typ in fields.items():
        items = values[key] if typ.endswith('[]') else [values[key]]
        base_type = typ.removesuffix('[]')
        if typ.endswith('[]') and (not isinstance(items, list) or len(items) != task.values['sdowrite_channel_num']):
            raise ValueError('外置 PWM 通道数量不一致')
        lo, hi = limits_for(task, key, base_type)
        for value in items:
            if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f'{key} 超过范围 [{lo}, {hi}]')
            if not base_type.startswith('float') and type(value) is not int:
                raise ValueError(f'{key} 必须是整数')
            if is_enable(key) or key == 'do_charge':
                if value not in (0, 1):
                    raise ValueError(f'{key} 必须为 0 或 1')
            if task.type_id == 4 and 1 <= value <= 47:
                raise ValueError('禁止通过运行界面发送 DSHOT 1–47 特殊命令')
    if task.type_id in (5, 15):
        for i in range(1, 5):
            if not task.values[f'sdowrite_motor{i}_can_id'] and values[f'motor{i}_enable']:
                raise ValueError(f'电机 {i} 未在从站配置中启用')
            if task.type_id == 5 and task.values.get(f'sdowrite_motor{i}_control_type') == 3:
                if not 0 <= values[f'motor{i}_cmd'] <= 8191:
                    raise ValueError('DJI 位置模式的编码器目标范围为 0–8191')


def feedback_problem(task, feedback):
    if not feedback:
        return '尚无反馈'
    if 'online' in feedback and not feedback['online']:
        return '从站报告设备离线'
    for key in ('overvoltage', 'undervoltage', 'overcurrent', 'mos_overtemperature',
                'rotor_overtemperature', 'communication_lost', 'overload', 'fail_safe'):
        if feedback.get(key):
            return f'设备故障: {key}'
    if task.type_id in (5, 15) or (task.type_id == 2 and task.values['sdowrite_control_type'] == 8):
        for i in range(1, 5):
            if task.type_id == 2 or task.values[f'sdowrite_motor{i}_can_id']:
                if not feedback.get(f'motor{i}_online'):
                    return f'电机 {i} 离线'
                if feedback.get(f'motor{i}_error_code'):
                    return f'电机 {i} 故障码 {feedback[f"motor{i}_error_code"]}'
    return ''
