"""First-run planning and probes. Importing this module never changes the host.

Hardware and administrator actions live in setup_worker; pure functions here
are also used to preview exactly what a button will do.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile


MASTER_URL = 'https://github.com/AIMEtherCAT/EcatV2_Master.git'
IMU_URL = 'https://github.com/AIMEtherCAT/hipnucimu.git'
STATE_VERSION = 1
STLINK_PRODUCTS = {'3744', '3748', '374a', '374b', '374d', '374e', '374f',
                   '3752', '3753', '3754', '3755', '3757'}
FIRST_RUN_YAML = """# 首次发现 SN 专用；取得真实 SN 后请进入从站任务配置。
# 这是 first-run-test.md 的原始简化示例，不是最终运行配置。
slaves:
  - sn2228293:
      sdo_len: !uint16_t 2
      task_count: !uint8_t 1
      latency_pub_topic: !std::string '/latency'

      tasks:
        - app_1:
            sdowrite_task_type: !uint8_t 3
            pub_topic: !std::string '/imu'
            pdoread_offset: !uint16_t 0
"""


def read_text(path, default=''):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def cpu_list(text):
    if str(text).strip() in ('', '(null)'):
        return []
    result = set()
    for part in str(text).strip().split(','):
        if not part:
            continue
        if re.fullmatch(r'\d+', part):
            result.add(int(part))
        elif re.fullmatch(r'\d+-\d+', part):
            lo, hi = map(int, part.split('-'))
            if hi < lo or hi - lo > 65535:
                raise ValueError('CPU 范围不合法')
            result.update(range(lo, hi + 1))
        else:
            raise ValueError(f'CPU 列表不合法: {part}')
    return sorted(result)


def compact_cpus(cpus):
    cpus = sorted(set(cpus))
    groups = []
    for c in cpus:
        if groups and c == groups[-1][-1] + 1:
            groups[-1].append(c)
        else:
            groups.append([c])
    return ','.join(str(g[0]) if len(g) == 1 else f'{g[0]}-{g[-1]}' for g in groups)


def kernel_args(cmdline):
    args = {}
    for arg in shlex.split(cmdline):
        k, _, v = arg.partition('=')
        args[k] = v
    return args


def numeric_cpu_args(value):
    return cpu_list(','.join(p for p in value.split(',') if re.fullmatch(r'\d+(?:-\d+)?', p)))


@dataclass(frozen=True)
class Cpu:
    id: int
    siblings: tuple
    core: int = 0
    package: int = 0
    kind: str = 'unknown'
    base_khz: int = 0


@dataclass(frozen=True)
class IsolationPlan:
    isolated: tuple
    housekeeping: tuple
    rt_cpu: int
    reason: str
    existing: bool = False

    @property
    def parameters(self):
        x, y = compact_cpus(self.isolated), compact_cpus(self.housekeeping)
        return f'nohz=on nohz_full={x} rcu_nocbs={x} isolcpus=domain,managed_irq,{x} irqaffinity={y}'

    def to_dict(self):
        return {**asdict(self), 'parameters': self.parameters,
                'non_rt_cpus': compact_cpus(self.housekeeping)}


def detect_cpus(sys_root=Path('/sys')):
    root = Path(sys_root) / 'devices/system/cpu'
    online = cpu_list(read_text(root / 'online'))
    atoms = cpu_list(read_text(Path(sys_root) / 'bus/event_source/devices/cpu_atom/cpus'))
    cores = cpu_list(read_text(Path(sys_root) / 'bus/event_source/devices/cpu_core/cpus'))
    result = []
    for i in online:
        p = root / f'cpu{i}'
        siblings = tuple(cpu_list(read_text(p / 'topology/thread_siblings_list', str(i))))
        # X86 topology: 1 = Intel Atom (E), 2 = Intel Core (P).
        typ = read_text(p / 'topology/core_type')
        kind = 'efficiency' if typ == '1' or i in atoms else 'performance' if typ == '2' or i in cores else 'unknown'
        result.append(Cpu(i, siblings, int(read_text(p / 'topology/core_id', str(i))),
                          int(read_text(p / 'topology/physical_package_id', '0')), kind,
                          int(read_text(p / 'cpufreq/base_frequency', '0'))))
    if not result:
        raise ValueError('无法读取在线 CPU 拓扑')
    return result


def choose_isolation(cpus, cmdline='', isolated=''):
    online = {c.id for c in cpus}
    args = kernel_args(cmdline)
    current = set(cpu_list(isolated) or numeric_cpu_args(args.get('isolcpus', '')))
    by_id = {c.id: c for c in cpus}
    if current and current < online:
        complete = set().union(*(set(by_id[i].siblings) for i in current if i in by_id))
        if complete == current and complete <= online:
            return IsolationPlan(tuple(sorted(current)), tuple(sorted(online - current)), min(current),
                                 '检测到已有完整物理核心隔离，沿用当前配置，避免更改已调好的主机。', True)
    candidates = []
    seen = set()
    for c in cpus:
        group = tuple(sorted(c.siblings))
        if group in seen or not set(group) <= online or set(group) == online:
            continue
        seen.add(group)
        candidates.append(c)
    if not candidates:
        raise ValueError('没有可完整隔离且仍保留 housekeeping CPU 的物理核心')
    # The tutorial permits CPU0 on homogeneous CPUs. Retain the boot CPU for
    # housekeeping/nohz here; this also avoids disabling all available CPUs.
    non_boot = [c for c in candidates if 0 not in c.siblings]
    if non_boot:
        candidates = non_boot
    efficient = [c for c in candidates if c.kind == 'efficiency' and c.base_khz > 2_000_000]
    performance = [c for c in candidates if c.kind == 'performance']
    if efficient:
        selected = max(efficient, key=lambda c: (c.base_khz, c.id))
        reason = '按教程选择基础频率超过 2 GHz 的 E 核，并隔离其完整物理核心。'
    elif performance:
        selected = max(performance, key=lambda c: (c.base_khz, c.id))
        reason = 'E 核基础频率未超过 2 GHz 或无法确认，按教程优先选择 P 核。'
    else:
        selected = max(candidates, key=lambda c: (c.base_khz, c.id))
        reason = '未发现可确认的 P/E 差异；保留 CPU0 给系统，选择一个完整物理核心。'
    x = tuple(sorted(selected.siblings))
    return IsolationPlan(x, tuple(sorted(online - set(x))), min(x), reason)


def verify_isolation(plan, cmdline, isolated, nohz_full):
    args = kernel_args(cmdline)
    x, y = set(plan.isolated), set(plan.housekeeping)
    checks = {
        'isolated': set(cpu_list(isolated)) == x,
        'nohz_full': set(cpu_list(nohz_full)) == x,
        'rcu_nocbs': set(numeric_cpu_args(args.get('rcu_nocbs', ''))) == x,
        'irqaffinity': set(numeric_cpu_args(args.get('irqaffinity', ''))) == y,
        'nohz': args.get('nohz') == 'on',
    }
    return checks


def grub_dropin(plan):
    # A managed drop-in avoids rewriting arbitrary shell code in /etc/default/grub.
    return '''# Managed by Ecat GroundStation; remove this file and run update-grub to revert.
_ecat_filter_args() {
    for _ecat_arg in $1; do
        case "$_ecat_arg" in
            nohz=*|nohz_full=*|rcu_nocbs=*|isolcpus=*|irqaffinity=*) ;;
            *) printf '%s ' "$_ecat_arg" ;;
        esac
    done
}
GRUB_CMDLINE_LINUX="$(_ecat_filter_args "$GRUB_CMDLINE_LINUX")"
GRUB_CMDLINE_LINUX_DEFAULT="$(_ecat_filter_args "$GRUB_CMDLINE_LINUX_DEFAULT") ''' + plan.parameters + '''"
unset -f _ecat_filter_args
unset _ecat_arg
'''


def parse_pro_status(data):
    """Return only necessary status; never return account IDs, email or tokens."""
    rt = next((s for s in data.get('services', []) if s.get('name') == 'realtime-kernel'), {})
    return {'attached': data.get('attached') is True, 'rt_enabled': rt.get('status') == 'enabled',
            'rt_available': rt.get('available') == 'yes', 'rt_entitled': rt.get('entitled') == 'yes',
            'rt_status': rt.get('status', 'unknown')}


def inspect_system():
    cpus = detect_cpus()
    cmdline = read_text('/proc/cmdline')
    isolated = read_text('/sys/devices/system/cpu/isolated')
    nohz = read_text('/sys/devices/system/cpu/nohz_full')
    plan = None
    plan_error = ''
    try:
        plan = choose_isolation(cpus, cmdline, isolated)
    except ValueError as exc:
        plan_error = str(exc)
    pro = {'attached': False, 'rt_enabled': False, 'error': ''}
    if shutil.which('pro'):
        try:
            r = subprocess.run(['pro', 'status', '--format', 'json'], capture_output=True, text=True, timeout=25)
            pro = parse_pro_status(json.loads(r.stdout))
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            pro['error'] = f'Ubuntu Pro 状态读取失败: {type(e).__name__}'
    else:
        pro['error'] = '未安装 Ubuntu Pro 客户端（pro）'
    return {'kernel': os.uname().release, 'realtime': read_text('/sys/kernel/realtime') == '1',
            'boot_id': read_text('/proc/sys/kernel/random/boot_id'), 'pro': pro,
            'cpus': [asdict(c) for c in cpus], 'plan': plan.to_dict() if plan else None, 'plan_error': plan_error,
            'isolation_checks': verify_isolation(plan, cmdline, isolated, nohz) if plan else {'available': False},
            'tools': {t: shutil.which(t) or '' for t in ('git', 'pkexec', 'openocd', 'cmake', 'ninja', 'arm-none-eabi-gcc', 'colcon', 'ros2')},
            'stlinks': detect_stlinks()}


def detect_stlinks(root=Path('/sys/bus/usb/devices')):
    result = []
    for path in sorted(Path(root).glob('*')):
        if read_text(path / 'idVendor').lower() != '0483':
            continue
        product = read_text(path / 'idProduct').lower()
        label = read_text(path / 'product')
        if product in STLINK_PRODUCTS or 'st-link' in label.lower() or 'stlink' in label.lower():
            result.append({'path': path.name, 'serial': read_text(path / 'serial'),
                           'product': label or 'ST-Link', 'usb_id': '0483:' + product})
    return result


def network_snapshot(root=Path('/sys/class/net')):
    result = {}
    for path in sorted(Path(root).iterdir()):
        if path.name == 'lo' or (path / 'wireless').exists():
            continue
        if read_text(path / 'type') != '1':
            continue
        result[path.name] = {'carrier': read_text(path / 'carrier', 'unknown'),
                             'operstate': read_text(path / 'operstate', 'unknown'),
                             'physical': (path / 'device').exists()}
    return result


def changed_interfaces(before, after):
    return [name for name in before.keys() & after.keys()
            if (before[name]['carrier'], before[name]['operstate']) != (after[name]['carrier'], after[name]['operstate'])]


def workspace_path(name, home=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('工作空间名称只允许字母、数字、下划线和短横线，不能是路径')
    home = Path(home or Path.home()).resolve()
    path = home / name
    if path.is_symlink() or path.resolve().parent != home:
        raise ValueError('工作空间必须是 Home 目录下的独立真实目录')
    return path


def validate_workspace(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError('请选择有效的工作空间目录')
    return path.resolve()


def verify_workspace_install(path):
    """Reject a launch using an older installed config or CPU/interface setup."""
    workspace = validate_workspace(path)
    if not (workspace / 'install/setup.bash').is_file():
        raise ValueError('工作空间尚未构建，请先执行 colcon build（普通用户）。')
    source = workspace / 'src/soem_bringup'
    installed = workspace / 'install/soem_bringup/share/soem_bringup'
    for relative in ('config/config.yaml', 'launch/bringup.launch.py'):
        original, target = source / relative, installed / relative
        if not original.is_file() or not target.is_file():
            raise ValueError(f'{relative} 缺失，请先创建 bringup 包并完成 colcon build。')
        if sha256_file(original) != sha256_file(target):
            raise ValueError(f'{relative} 已修改，但安装目录仍是旧文件。请先执行 colcon build（普通用户），成功后再打开 root 终端。')
    return workspace


def validate_interface(name, root=Path('/sys/class/net')):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name) or not (Path(root) / name).is_dir() or name == 'lo':
        raise ValueError('网口不存在或名称不合法')
    return name


def launch_file(interface, rt_cpu, non_rt_cpus):
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('网口名称不合法')
    others = cpu_list(non_rt_cpus)
    if type(rt_cpu) is not int or rt_cpu < 0 or rt_cpu in others or not others:
        raise ValueError('实时 CPU 与其余 CPU 必须分离')
    return f'''# Generated by Ecat GroundStation · first-run-test
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():
    config_file = os.path.join(get_package_share_directory('soem_bringup'), 'config', 'config.yaml')
    return LaunchDescription([
        Node(
            package='soem_wrapper',
            executable='soem_backend',
            name='soem_backend',
            parameters=[{{
                'interface': {interface!r},
                'rt_cpu': {rt_cpu},
                'non_rt_cpus': {compact_cpus(others)!r},
                'config_file': config_file,
            }}],
            output='screen',
        )
    ])
'''


def install_cmake_block(text):
    if re.search(r'install\s*\(\s*DIRECTORY\s+launch\s+config\s+DESTINATION\s+share/\$\{PROJECT_NAME\}/?\s*\)', text, re.S):
        return text
    block = '\ninstall(DIRECTORY\n    launch\n    config\n    DESTINATION share/${PROJECT_NAME}/\n)\n\n'
    match = re.search(r'^\s*ament_package\s*\(', text, re.M)
    if not match:
        raise ValueError('CMakeLists.txt 中未找到 ament_package()')
    return text[:match.start()] + block + text[match.start():]


def validate_imu_ids(profiles):
    seen = set()
    for i, ids in enumerate(profiles, 1):
        if len(ids) != 3 or any(type(x) is not int or not 1 <= x <= 0x7FF for x in ids):
            raise ValueError(f'IMU {i}: 三个 CAN ID 必须在 0x001–0x7FF 内')
        if len(set(ids)) != 3 or seen.intersection(ids):
            raise ValueError('同一 CAN 总线上的 IMU 帧 ID 不能重复')
        seen.update(ids)


def patch_imu_source(text, ids):
    validate_imu_ids([ids])
    for n, value in enumerate(ids, 1):
        pattern = rf'^(\s*#\s*define\s+PACKET{n}_CAN_ID\s+)(?:0[xX][0-9a-fA-F]+|\d+)([uUlL]*)(.*)$'
        text, count = re.subn(pattern, lambda m: f'{m[1]}0x{value:03X}{m[2]}{m[3]}', text, flags=re.M)
        if count != 1:
            raise ValueError(f'PACKET{n}_CAN_ID 必须恰好定义一次，源码版本不匹配')
    return text


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n')
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
        if data.get('version') != STATE_VERSION:
            return {}
        return data
    except (ValueError, OSError):
        return {}


def flash_ranges(path, base=0x08000000):
    """Extract actual load ranges; reject wrong arch, empty/truncated images."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0 or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('固件文件不存在、为空或超过 64 MiB')
    data = path.read_bytes()
    suffix = path.suffix.lower()
    ranges = []
    if suffix == '.elf':
        if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01' or struct.unpack_from('<H', data, 18)[0] != 40:
            raise ValueError('需要 little-endian ARM ELF32 固件')
        phoff = struct.unpack_from('<I', data, 28)[0]
        entsize, number = struct.unpack_from('<HH', data, 42)
        if entsize < 32 or number == 0 or phoff + entsize * number > len(data):
            raise ValueError('ELF program header 损坏')
        for i in range(number):
            typ, offset, _, physical, size = struct.unpack_from('<IIIII', data, phoff + entsize * i)
            if typ == 1 and size:
                if offset + size > len(data):
                    raise ValueError('ELF load segment 越界')
                ranges.append((physical, size))
    elif suffix == '.bin':
        ranges = [(base, len(data))]
    elif suffix == '.hex':
        upper, eof = 0, False
        for line in data.decode('ascii').splitlines():
            if not line.strip():
                continue
            if eof or not line.startswith(':'):
                raise ValueError('Intel HEX 格式不合法')
            record = bytes.fromhex(line[1:])
            if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xFF:
                raise ValueError('Intel HEX 长度或 checksum 错误')
            size, addr, typ = record[0], int.from_bytes(record[1:3], 'big'), record[3]
            if typ == 0 and size:
                ranges.append((upper + addr, size))
            elif typ == 1:
                eof = True
            elif typ == 4 and size == 2:
                upper = int.from_bytes(record[4:6], 'big') << 16
            elif typ == 2 and size == 2:
                upper = int.from_bytes(record[4:6], 'big') << 4
            elif typ not in (3, 5):
                raise ValueError('不支持的 Intel HEX record')
        if not eof:
            raise ValueError('Intel HEX 缺少 EOF')
    else:
        raise ValueError('支持的固件格式为 .elf / .bin / .hex')
    flash = [(addr, size) for addr, size in ranges if 0x08000000 <= addr < 0x09000000]
    if not flash or any(addr + size > 0x08020000 for addr, size in flash):
        raise ValueError('固件超出 H750/G431 的 128 KiB 内部 Flash 范围 0x08000000–0x0801FFFF')
    if any(not (0x08000000 <= addr < 0x08020000 or 0x20000000 <= addr < 0x40000000) for addr, _ in ranges):
        raise ValueError('镜像包含不支持的加载地址')
    return flash


def tcl_word(value):
    value = str(value)
    if any(ch in value for ch in ('{', '}', '\\', '\n', '\r', '\x00')):
        raise ValueError('OpenOCD 路径或序列号不能含大括号、反斜杠或换行，请重命名文件')
    return '{' + value + '}'


def openocd_commands(target, image=None, serial='', speed=None, base=0x08000000):
    if target not in ('h750', 'g431'):
        raise ValueError('不支持的 MCU')
    speed = speed or (400 if target == 'h750' else 1000)
    if not 50 <= speed <= 4000:
        raise ValueError('SWD 速度应在 50–4000 kHz')
    family = 'stm32h7x' if target == 'h750' else 'stm32g4x'
    args = ['openocd', '-f', 'interface/stlink.cfg', '-f', f'target/{family}.cfg']
    commands = [f'adapter speed {speed}']
    if serial:
        commands.append('hla_serial ' + tcl_word(serial))
    commands += ['init', 'halt' if target == 'h750' else 'reset halt']
    register, expected = ('0x5c001000', '0x450') if target == 'h750' else ('0xe0042000', '0x468')
    commands.append(f'if {{([mrw {register}] & 0xfff) != {expected}}} {{error "MCU device ID mismatch"}}')
    commands += ['flash probe 0', 'flash info 0', 'targets']
    if image:
        image = Path(image).resolve()
        flash_ranges(image, base)
        word = tcl_word(image)
        address = f' 0x{base:08x}' if image.suffix.lower() == '.bin' else ''
        commands += [f'flash write_image erase {word}{address}', f'verify_image {word}{address}']
        if target == 'g431':
            commands.append('reset run')
    commands.append('shutdown')
    for command in commands:
        args += ['-c', command]
    return args


def flash_verified(output):
    return bool(re.search(r'(?:wrote\s+\d+\s+bytes|Programming Finished)', output, re.I)
                and re.search(r'(?:verified\s+\d+\s+bytes|Verified OK)', output, re.I))


def parse_slave_sns(log):
    return list(dict.fromkeys(re.findall(r'Found slave\s+(?:id|idx)\s*=\s*\d+\s*,\s*sn\s*=\s*(\d+)', log, re.I)))
