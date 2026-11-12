"""Project model, strict tagged YAML import/export, allocation validation."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile
from dataclasses import dataclass, field, asdict

import yaml
from .schema import SPECS, active_fields, pdo_size, resources

TAG_BYTES = {"uint8_t": 1, "int8_t": 1, "uint16_t": 2, "int16_t": 2,
             "uint32_t": 4, "int32_t": 4, "float": 4}
TYPE_BOUNDS = {"uint8_t": (0, 255), "int8_t": (-128, 127), "uint16_t": (0, 65535),
               "int16_t": (-32768, 32767), "uint32_t": (0, 4294967295),
               "int32_t": (-2147483648, 2147483647)}


@dataclass(frozen=True)
class Tagged:
    tag: str
    value: object


class ConfigLoader(yaml.SafeLoader):
    pass


class ConfigDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _construct(loader, suffix, node):
    if suffix not in (*TAG_BYTES, "std::string") or not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"不支持的 YAML 类型标签 !{suffix}")
    raw = loader.construct_scalar(node)
    if suffix == "std::string":
        value = raw
    elif suffix == "float":
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("浮点数必须为有限值")
    else:
        value = int(raw, 16 if raw.lower().lstrip('-').startswith('0x') else 10)
        lo, hi = TYPE_BOUNDS[suffix]
        if not lo <= value <= hi:
            raise ValueError(f"!{suffix} 数值越界: {value}")
    return Tagged(suffix, value)


def _represent(dumper, item):
    return dumper.represent_scalar("!" + item.tag, str(item.value),
                                   style="'" if item.tag == "std::string" else None)


def _unique_map(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"重复或非法 YAML 键: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


ConfigLoader.add_multi_constructor("!", _construct)
ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_map)
ConfigDumper.add_representer(Tagged, _represent)


@dataclass
class Task:
    type_id: int
    values: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)

    @classmethod
    def create(cls, type_id, sn, index):
        spec = SPECS[type_id]
        values = {f.key: f.default for f in spec.fields}
        if spec.read_type:
            values['pub_topic'] = f"/ecat/sn{sn}/app{index}/read"
        if spec.write_type or type_id in (2, 12):
            values['sub_topic'] = f"/ecat/sn{sn}/app{index}/write"
        return cls(type_id, values)


@dataclass
class Slave:
    sn: str
    model: str = "standard"
    tasks: list[Task] = field(default_factory=list)
    latency_topic: str = ""

    def __post_init__(self):
        if not self.latency_topic:
            self.latency_topic = f"/ecat/sn{self.sn}/latency"

    @property
    def capacity(self):
        return (112 if self.model == "large" else 80), 80

    def allocations(self):
        read, write = 0, 0
        result = []
        for task in self.tasks:
            r, w = pdo_size(task)
            result.append((read, write, r, w))
            read += r
            write += w
        return result, read, write

    def sdo_length(self):
        return 1 + sum(1 + sum(TAG_BYTES[f.tag] for f in active_fields(t)
                               if f.key.startswith('sdowrite_')) for t in self.tasks)


@dataclass
class Project:
    slaves: list[Slave] = field(default_factory=list)
    interface: str = "enp2s0"
    rt_cpu: int = 6
    non_rt_cpus: str = "0-5,7-15"
    ros_domain: int = 24
    workspace: str = ""

    @classmethod
    def demo(cls):
        slave = Slave("4587574")
        slave.tasks = [Task.create(t, slave.sn, i) for i, t in enumerate((12, 6, 3), 1)]
        slave.tasks[2].values['sdowrite_can_inst'] = 2
        return cls([slave], ros_domain=int(os.environ.get('ROS_DOMAIN_ID', '0')))

    def to_dict(self):
        result = asdict(self)
        result['format'] = 'ecat-groundstation/v1'
        return result

    @classmethod
    def from_dict(cls, data):
        data = copy.deepcopy(data)
        if data.pop('format', None) != 'ecat-groundstation/v1':
            raise ValueError("不支持的项目版本")
        slaves = data.pop('slaves')
        result = cls(**data)
        for s in slaves:
            ts = s.pop('tasks')
            slave = Slave(**s)
            for td in ts:
                if td['type_id'] not in SPECS:
                    raise ValueError(f"不支持的任务 {td['type_id']}")
                defaults = Task.create(td['type_id'], slave.sn, len(slave.tasks) + 1)
                defaults.values.update(td['values'])
                defaults.extras = td.get('extras', {})
                slave.tasks.append(defaults)
            result.slaves.append(slave)
        return result

    def validate(self):
        errors = []
        sns, topics = set(), {}
        if not self.slaves:
            errors.append("请至少添加一个从站")
        for slave in self.slaves:
            prefix = f"SN {slave.sn}"
            if not re.fullmatch(r'[1-9][0-9]{0,9}', slave.sn) or int(slave.sn) > 0xFFFFFFFF:
                errors.append(f"{prefix}: SN 必须是非零 uint32 十进制数")
            if slave.sn in sns:
                errors.append(f"{prefix}: SN 重复")
            sns.add(slave.sn)
            if slave.model not in ('standard', 'large'):
                errors.append(f"{prefix}: 未知模块型号")
            if not 1 <= len(slave.tasks) <= 255:
                errors.append(f"{prefix}: 任务数量须为 1–255")
            claimed = {}
            self._check_topic(slave.latency_topic, prefix + '/latency', topics, errors)
            try:
                for idx, task in enumerate(slave.tasks, 1):
                    at = f"{prefix} / app_{idx}"
                    if task.type_id not in SPECS:
                        errors.append(f"{at}: 不支持的任务类型 {task.type_id}")
                        continue
                    for f in active_fields(task):
                        val = task.values.get(f.key)
                        if f.tag == 'std::string':
                            ok = isinstance(val, str) and bool(val)
                        elif f.tag == 'float':
                            ok = type(val) in (int, float) and math.isfinite(val) and abs(val) <= 3.4028234e38
                        else:
                            ok = type(val) is int and TYPE_BOUNDS[f.tag][0] <= val <= TYPE_BOUNDS[f.tag][1]
                        if ok and f.tag != 'std::string':
                            ok = (f.minimum is None or val >= f.minimum) and (f.maximum is None or val <= f.maximum)
                        if ok and f.choices:
                            ok = val in dict(f.choices)
                        if not ok:
                            errors.append(f"{at}: {f.label} 数值或类型不合法: {val}")
                    for topic_key in ('pub_topic', 'sub_topic'):
                        if topic_key in task.values:
                            self._check_topic(task.values[topic_key], at + '/' + topic_key, topics, errors)
                    for key in task.extras:
                        if key.startswith('sdowrite_'):
                            errors.append(f"{at}: 未识别的 SDO 字段 {key}，请核对固件版本")
                    if task.type_id in (4, 6, 7):
                        init = task.values['sdowrite_init_value']
                        if task.type_id == 4 and 1 <= init <= 47:
                            errors.append(f"{at}: DSHOT 初值不能是 1–47 特殊命令")
                        if task.type_id != 4 and init > task.values['sdowrite_pwm_period']:
                            errors.append(f"{at}: PWM 初值超过周期")
                    if task.type_id in (5, 15) and not any(task.values[f'sdowrite_motor{i}_can_id'] for i in range(1, 5)):
                        errors.append(f"{at}: 至少启用一个电机槽位")
                    if task.type_id in (5, 15):
                        # Current backend init_value writes all four slots even
                        # when its normal PDO serializer packs enabled slots.
                        layouts, _, _ = slave.allocations()
                        write_start = layouts[idx - 1][1]
                        if write_start + 12 > slave.capacity[1]:
                            errors.append(f"{at}: 原主站电机组初始化写入 12 B，末尾空间不足")
                        if pdo_size(task)[1] < 12 and any(pdo_size(t)[1] for t in slave.tasks[idx:]):
                            errors.append(f"{at}: 原主站部分启用电机组的初始化写入 12 B；请将该组放在全部输出任务末尾，或启用四个槽位")
                    for resource, identity, exclusive in resources(task):
                        for old_id, old_exclusive, old_task in claimed.get(resource, []):
                            if exclusive or old_exclusive or identity == old_id:
                                errors.append(f"{at}: {resource} / {identity} 与 {old_task} 资源冲突")
                        claimed.setdefault(resource, []).append((identity, exclusive, f'app_{idx}'))
                _, read, write = slave.allocations()
                rc, wc = slave.capacity
                if read > rc or write > wc:
                    errors.append(f"{prefix}: PDO 超容量，反馈 {read}/{rc} B，命令 {write}/{wc} B")
                if slave.sdo_length() > 65535:
                    errors.append(f"{prefix}: SDO 参数超过 uint16 容量")
            except (KeyError, TypeError, ValueError) as e:
                errors.append(f"{prefix}: 配置不完整或数值错误: {e}")
        if type(self.ros_domain) is not int or not 0 <= self.ros_domain <= 232:
            errors.append("ROS_DOMAIN_ID 必须在 0–232")
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', self.interface):
            errors.append("网卡名称不合法")
        if type(self.rt_cpu) is not int or self.rt_cpu < 0:
            errors.append("实时 CPU 必须是非负整数")
        if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', self.non_rt_cpus):
            errors.append("非实时 CPU 格式应如 0-5,7-15")
        return errors

    @staticmethod
    def _check_topic(value, owner, claimed, errors):
        if not isinstance(value, str) or not re.fullmatch(r'/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*', value):
            errors.append(f"{owner}: ROS 话题须为合法绝对名称")
        elif value in claimed:
            errors.append(f"{owner}: 话题 {value} 与 {claimed[value]} 重复")
        else:
            claimed[value] = owner

    def export_yaml(self):
        errors = self.validate()
        if errors:
            raise ValueError('\n'.join(errors))
        root = {'slaves': []}
        for slave in self.slaves:
            rows, _, _ = slave.allocations()
            body = {'sdo_len': Tagged('uint16_t', slave.sdo_length()),
                    'task_count': Tagged('uint8_t', len(slave.tasks)),
                    'latency_pub_topic': Tagged('std::string', slave.latency_topic), 'tasks': []}
            for idx, (task, (ro, wo, r, w)) in enumerate(zip(slave.tasks, rows), 1):
                data = {'sdowrite_task_type': Tagged('uint8_t', task.type_id)}
                for f in active_fields(task):
                    data[f.key] = Tagged(f.tag, float(task.values[f.key]) if f.tag == 'float' else task.values[f.key])
                for topic_key, offset_key, offset, size in (('pub_topic', 'pdoread_offset', ro, r),
                                                            ('sub_topic', 'pdowrite_offset', wo, w)):
                    if size or topic_key in task.values:
                        data[topic_key] = Tagged('std::string', task.values[topic_key])
                        data[offset_key] = Tagged('uint16_t', offset)
                for key, extra in task.extras.items():
                    if key not in data:
                        data[key] = Tagged(extra['tag'], extra['value'])
                body['tasks'].append({f'app_{idx}': data})
            root['slaves'].append({f'sn{slave.sn}': body})
        return '# Generated by Ecat GroundStation · EcatV2_Master tagged ABI\n' + yaml.dump(
            root, Dumper=ConfigDumper, allow_unicode=True, sort_keys=False)

    @classmethod
    def import_yaml(cls, text):
        root = yaml.load(text, Loader=ConfigLoader)
        if not isinstance(root, dict) or set(root) != {'slaves'} or not isinstance(root['slaves'], list):
            raise ValueError("需要 EcatV2_Master 的 slaves 列表；第三方设备配置暂不支持")
        result, warnings = cls(ros_domain=int(os.environ.get('ROS_DOMAIN_ID', '0'))), []
        for entry in root['slaves']:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise ValueError("每个从站必须为单一 sn<序列号> 映射")
            name, body = next(iter(entry.items()))
            if not re.fullmatch(r'sn[0-9]+', name) or not isinstance(body, dict):
                raise ValueError(f"不支持的设备 {name}；仅支持 AIM H750")
            required = {'sdo_len', 'task_count', 'latency_pub_topic', 'tasks'}
            if set(body) != required:
                raise ValueError(f"{name} 从站字段不匹配: {set(body) ^ required}")
            get = lambda d, k, tag: _require_tag(d, k, tag)
            slave = Slave(name[2:], latency_topic=get(body, 'latency_pub_topic', 'std::string'))
            if not isinstance(body['tasks'], list):
                raise ValueError(f"{name}: tasks 必须是列表")
            declared_offsets = []
            for idx, row in enumerate(body['tasks'], 1):
                if not isinstance(row, dict) or set(row) != {f'app_{idx}'}:
                    raise ValueError(f"{name}: 任务键必须连续为 app_1, app_2…")
                data = row[f'app_{idx}']
                tid = get(data, 'sdowrite_task_type', 'uint8_t')
                if tid not in SPECS:
                    raise ValueError(f"{name}/app_{idx}: 不支持任务 {tid}（ADC 9 未实现于主站）")
                task = Task.create(tid, slave.sn, idx)
                known = {f.key: f for f in SPECS[tid].fields}
                for key, item in data.items():
                    if not isinstance(item, Tagged):
                        raise ValueError(f"{name}/app_{idx}/{key}: 缺少显式 YAML 类型标签")
                    expected = known[key].tag if key in known else {'pub_topic': 'std::string', 'sub_topic': 'std::string',
                        'pdoread_offset': 'uint16_t', 'pdowrite_offset': 'uint16_t', 'sdowrite_task_type': 'uint8_t'}.get(key)
                    if expected and item.tag != expected:
                        raise ValueError(f"{name}/app_{idx}/{key}: 需要 !{expected}，得到 !{item.tag}")
                    if key in known or key in ('pub_topic', 'sub_topic'):
                        task.values[key] = item.value
                    elif key not in ('sdowrite_task_type', 'pdoread_offset', 'pdowrite_offset'):
                        task.extras[key] = {'tag': item.tag, 'value': item.value}
                for f in active_fields(task):
                    if f.key not in data:
                        warnings.append(f"{name}/app_{idx}: 缺少 {f.key}，补充默认值 {f.default}")
                # Required topic names must never silently acquire new destinations.
                for key in ('pub_topic', 'sub_topic'):
                    if key in task.values and key not in data:
                        raise ValueError(f"{name}/app_{idx}: 缺少 {key}")
                declared_offsets.append((data.get('pdoread_offset'), data.get('pdowrite_offset')))
                slave.tasks.append(task)
            layouts, read, _ = slave.allocations()
            if read > 80:
                slave.model = 'large'
                warnings.append(f"{name}: 反馈 PDO > 80 B，按 Large PDO 模型载入，请核对实物")
            if get(body, 'task_count', 'uint8_t') != len(slave.tasks):
                warnings.append(f"{name}: task_count 已按实际任务数重算")
            if get(body, 'sdo_len', 'uint16_t') != slave.sdo_length():
                warnings.append(f"{name}: sdo_len 已重算为 {slave.sdo_length()}")
            for idx, (row, offsets) in enumerate(zip(layouts, declared_offsets), 1):
                for current, expected in zip(offsets, row[:2]):
                    if current is not None and current.value != expected:
                        warnings.append(f"{name}/app_{idx}: PDO 偏移已重新连续分配")
            result.slaves.append(slave)
        return result, warnings


def _require_tag(data, key, tag):
    item = data.get(key) if isinstance(data, dict) else None
    if not isinstance(item, Tagged) or item.tag != tag:
        raise ValueError(f"{key} 必须有 !{tag} 类型标签")
    return item.value


def atomic_write(path, content):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_project(project, path):
    atomic_write(path, json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + '\n')


def load_project(path):
    return Project.from_dict(json.loads(Path(path).read_text(encoding='utf-8')))
