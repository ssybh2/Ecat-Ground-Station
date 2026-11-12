"""One-shot first-run jobs, launched by the GUI as user or through pkexec.

No passwords are accepted, persisted or printed. Privilege elevation is owned
by the system's authentication dialog. Build jobs explicitly reject root.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecat_groundstation.setup_core import (
    MASTER_URL, IMU_URL, FIRST_RUN_YAML, read_text, detect_cpus, choose_isolation,
    grub_dropin, inspect_system, parse_pro_status, detect_stlinks, validate_workspace,
    validate_interface, workspace_path, launch_file, install_cmake_block, validate_imu_ids,
    verify_workspace_install,
    patch_imu_source, sha256_file, atomic_json, openocd_commands, flash_verified, flash_ranges,
)
from ecat_groundstation.eeprom_support import build_eeprom_tool, verified_eeprom_tool
from ecat_groundstation.h750_runtime import prepare_h750_runtime


RESULT_PREFIX = '@@ECAT_RESULT@@'
BACKUP_ROOT = Path('/var/backups/ecat-groundstation')
GRUB_DEFAULT = Path('/etc/default/grub')
GRUB_DROPIN = Path('/etc/default/grub.d/99-ecat-groundstation.cfg')
GRUB_CONFIG = Path('/boot/grub/grub.cfg')


class JobCancelled(RuntimeError):
    """Must not be InterruptedError: selectors treats it as a retriable syscall."""


def result(data):
    print(RESULT_PREFIX + json.dumps(data, ensure_ascii=False), flush=True)


def require_user():
    if os.geteuid() == 0:
        raise RuntimeError('源码准备和编译必须由普通用户执行，请勿以 root 启动上位机')


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError('此操作需要管理员授权，请从上位机按钮启动系统认证窗口')


def run(args, cwd=None, env=None, timeout=1800):
    print('$ ' + shlex.join(map(str, args)), flush=True)
    proc = subprocess.Popen(list(map(str, args)), cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    start = time.monotonic()
    output = []
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            if timeout and time.monotonic() - start > timeout:
                os.killpg(proc.pid, signal.SIGTERM)
                raise TimeoutError(f'命令超时（{timeout} 秒），未标记成功')
            for key, _ in selector.select(.1):
                block = os.read(key.fileobj.fileno(), 65536)
                if block:
                    text = block.decode('utf-8', errors='replace')
                    output.append(text)
                    print(text, end='', flush=True)
                else:
                    selector.unregister(proc.stdout)
            if proc.poll() is not None and not selector.get_map():
                break
        if proc.returncode:
            raise RuntimeError(f'命令失败，退出码 {proc.returncode}: {args[0]}')
        return ''.join(output)
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        proc.stdout.close()


def ros_command(setup, command, workspace, timeout=3600):
    setup = Path(setup).resolve()
    if not re.fullmatch(r'/opt/ros/[A-Za-z0-9_-]+/setup\.bash', str(setup)) or not setup.is_file():
        raise ValueError('请安装并选择 /opt/ros/<发行版>/setup.bash')
    env = os.environ.copy()
    for key in ('AMENT_PREFIX_PATH', 'COLCON_PREFIX_PATH', 'CMAKE_PREFIX_PATH',
                'LD_LIBRARY_PATH', 'PYTHONPATH', 'ROS_DISTRO', 'ROS_VERSION', 'ROS_PYTHON_VERSION'):
        env.pop(key, None)
    return run(['/bin/bash', '-c', 'source "$1" && shift && exec "$@"',
                'ecat-ros-setup', str(setup), *command], cwd=workspace, env=env, timeout=timeout)


def ensure_no_master():
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = p.read_bytes().split(b'\0')
            if args and Path(os.fsdecode(args[0])).name == 'soem_backend':
                raise RuntimeError('检测到 soem_backend 正在运行，请先停止 EtherCAT 主站')
        except (OSError, ProcessLookupError):
            continue


def inspect(args):
    return inspect_system()


def enable_rt(args):
    require_root()
    if read_text('/sys/kernel/realtime') == '1':
        return {'message': '当前已运行 PREEMPT_RT 实时内核，无需重复安装', 'reboot_required': False}
    data = subprocess.run(['pro', 'status', '--format', 'json'], capture_output=True, text=True, timeout=30)
    status = parse_pro_status(json.loads(data.stdout))
    if not status['attached']:
        raise RuntimeError('请先通过 Ubuntu Pro 引导完成 pro attach，然后重新检查')
    if status['rt_enabled']:
        return {'message': '实时内核服务已启用，当前尚未运行实时内核。请重启并在 GRUB 选择 realtime 内核。', 'reboot_required': True}
    run(['pro', 'enable', 'realtime-kernel', '--assume-yes'], timeout=7200)
    data = subprocess.run(['pro', 'status', '--format', 'json'], capture_output=True, text=True, timeout=30)
    if not parse_pro_status(json.loads(data.stdout))['rt_enabled']:
        raise RuntimeError('pro enable 已退出，但 realtime-kernel 状态未启用，请查看完整日志')
    return {'message': '实时内核已启用。请保存工作并重启电脑；重启后回到首次配置检查运行内核。', 'reboot_required': True}


def attach_pro(args):
    require_root()
    # Interactive Ubuntu One/device-code flow remains in a visible terminal.
    code = subprocess.call(['pro', 'attach'])
    if code:
        raise RuntimeError(f'Ubuntu Pro 绑定未完成，退出码 {code}')
    return {'message': 'Ubuntu Pro 绑定命令已完成，请返回上位机刷新环境检查'}


def isolate_cpu(args):
    require_root()
    ensure_no_master()
    if read_text('/sys/kernel/realtime') != '1':
        raise RuntimeError('请先重启进入实时内核，再配置 CPU 核心隔离')
    plan = choose_isolation(detect_cpus(), read_text('/proc/cmdline'), read_text('/sys/devices/system/cpu/isolated'))
    from ecat_groundstation.setup_core import verify_isolation
    checks = verify_isolation(plan, read_text('/proc/cmdline'), read_text('/sys/devices/system/cpu/isolated'),
                              read_text('/sys/devices/system/cpu/nohz_full'))
    if all(checks.values()):
        return {'message': '当前隔离已完整生效，无需修改 GRUB', 'plan': plan.to_dict(), 'reboot_required': False}
    if not GRUB_DEFAULT.is_file() or not shutil.which('update-grub'):
        raise RuntimeError('此主机没有 Ubuntu GRUB 配置，停止自动修改')
    target = GRUB_DROPIN
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000)
    backup = BACKUP_ROOT / stamp
    backup.mkdir(parents=True, mode=0o700)
    previous = target.read_bytes() if target.exists() else None
    shutil.copy2(GRUB_DEFAULT, backup / 'grub')
    if previous is not None:
        shutil.copy2(target, backup / target.name)
    atomic_json(backup / 'manifest.json', {'dropin_existed': previous is not None, 'plan': plan.to_dict()})
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(grub_dropin(plan))
    temp.chmod(0o644)
    os.replace(temp, target)
    try:
        run(['update-grub'], timeout=300)
        if shutil.which('grub-script-check'):
            run(['grub-script-check', str(GRUB_CONFIG)], timeout=30)
    except Exception:
        print('GRUB 生成失败，正在恢复此前的配置。', flush=True)
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
        run(['update-grub'], timeout=300)
        raise
    return {'message': '隔离参数已保存并生成 GRUB。请重启后再次检查，未重启前不算生效。',
            'plan': plan.to_dict(), 'backup': str(backup), 'reboot_required': True}


def ensure_workspace_metadata(workspace):
    """Keep app data writable by the workspace owner, including before root launch."""
    owner = workspace.stat()
    if owner.st_uid == 0:
        raise ValueError('工作空间属于 root，请使用普通用户拥有的工作空间')
    invoking_uid = os.environ.get('PKEXEC_UID')
    if os.geteuid() == 0 and invoking_uid is not None and int(invoking_uid) != owner.st_uid:
        raise ValueError('只能修复当前登录用户拥有的工作空间')
    metadata = workspace / '.ecat-groundstation'
    if metadata.is_symlink() or not metadata.resolve().is_relative_to(workspace):
        raise ValueError('工程数据目录不能是符号链接，未修改权限')
    metadata.mkdir(mode=0o755, exist_ok=True)
    current = metadata.stat()
    if current.st_uid == 0 and os.geteuid() == 0:
        # Only the app's top-level directory: never recursively chown the
        # workspace, source configuration, build products, or ROS logs.
        os.chown(metadata, owner.st_uid, owner.st_gid, follow_symlinks=False)
        metadata.chmod((current.st_mode & 0o777) | 0o700)
    elif current.st_uid != owner.st_uid:
        raise PermissionError(f'工程数据目录不属于工作空间用户：{metadata}')
    return metadata


def repair_workspace_access(args):
    require_root()
    workspace = validate_workspace(args.workspace)
    if not (workspace / 'src/soem_bringup/package.xml').is_file():
        raise ValueError('工作空间缺少 soem_bringup，未修改权限')
    metadata = ensure_workspace_metadata(workspace)
    return {'workspace': str(workspace), 'metadata': str(metadata),
            'message': '已修复工程数据目录归属，可以由普通用户备份并写入任务配置。'}


def create_workspace(args):
    require_user()
    path = workspace_path(args.name)
    if path.exists() and not (path / '.ecat-groundstation-workspace.json').is_file():
        raise ValueError('同名目录已存在且不是本软件创建的工作空间，请换一个名称，避免覆盖已有工程')
    path.mkdir(parents=True, exist_ok=True)
    (path / 'src').mkdir(exist_ok=True)
    ensure_workspace_metadata(path)
    marker = path / '.ecat-groundstation-workspace.json'
    # Retain a marker on failure so network-interrupted jobs can be retried.
    atomic_json(marker, {'version': 1, 'master_url': MASTER_URL})
    if not (path / '.git').exists():
        run(['git', 'init'], cwd=path)
    target = path / 'src/EcatV2_Master'
    submodule_entry = subprocess.run(['git', 'config', '-f', '.gitmodules', '--get-regexp', r'^submodule\..*\.path$'],
                                    cwd=path, capture_output=True, text=True)
    registered = [line.split(None, 1)[1] for line in submodule_entry.stdout.splitlines() if len(line.split(None, 1)) == 2]
    if 'src/EcatV2_Master' not in registered:
        add_flags = []
        if target.exists() and any(target.iterdir()):
            remote = subprocess.run(['git', '-C', str(target), 'remote', 'get-url', 'origin'], capture_output=True, text=True)
            if remote.returncode or remote.stdout.strip() != MASTER_URL:
                raise ValueError('src/EcatV2_Master 已存在且来源不匹配，未自动覆盖')
        cached = path / '.git/modules/src/EcatV2_Master'
        if cached.is_dir():
            remote = subprocess.run(['git', '--git-dir', str(cached), 'config', '--get', 'remote.origin.url'], capture_output=True, text=True)
            if remote.returncode or remote.stdout.strip() != MASTER_URL:
                raise ValueError('已有子模块缓存的来源不匹配，请检查工作空间')
            add_flags = ['--force']
        run(['git', 'submodule', 'add', *add_flags, MASTER_URL, 'src/EcatV2_Master'], cwd=path, timeout=1800)
    run(['git', 'submodule', 'update', '--init', '--recursive'], cwd=path, timeout=3600)
    tools = target / 'tools'
    for tool in tools.iterdir():
        if tool.is_file():
            tool.chmod(tool.stat().st_mode | 0o111)
    # These files are owned by the user; sudo chmod is unnecessary.
    return {'workspace': str(path), 'message': '工作空间、Git 子模块及 SOEM 已准备好，工具执行权限已设置。请连接 Type-C 逻辑供电和 EtherCAT IN 网线。'}


def create_bringup(args):
    require_user()
    workspace = validate_workspace(args.workspace)
    if not (workspace / 'src/EcatV2_Master/src/soem_wrapper').is_dir():
        raise ValueError('工作空间缺少 EcatV2_Master 子模块，请先完成创建工作空间')
    launch = launch_file(args.interface, args.rt_cpu, args.non_rt_cpus)
    package = workspace / 'src/soem_bringup'
    marker = package / '.ecat-groundstation-generated.json'
    targets = {'config/config.yaml': FIRST_RUN_YAML, 'launch/bringup.launch.py': launch}
    configuration_ready = False
    if package.exists():
        if not marker.is_file():
            raise ValueError('soem_bringup 已存在且非本软件生成，未覆盖现有文件')
        previous = json.loads(marker.read_text())
        configuration_ready = previous.get('configuration_ready', False)
        for relative, expected in previous.get('hashes', {}).items():
            p = package / relative
            if p.exists() and sha256_file(p) != expected:
                raise ValueError(f'{relative} 已由用户修改，为保护现有配置拒绝覆盖，请使用新工作空间')
        if configuration_ready and (package / 'config/config.yaml').is_file():
            targets['config/config.yaml'] = (package / 'config/config.yaml').read_text()
    else:
        ros_command(args.ros_setup, ['ros2', 'pkg', 'create', '--build-type', 'ament_cmake', 'soem_bringup'], workspace / 'src')
    cmake = package / 'CMakeLists.txt'
    targets['CMakeLists.txt'] = install_cmake_block(cmake.read_text())
    # launch/launch_ros are Python runtime dependencies, not CMake packages.
    # Passing them to ros2 pkg create --dependencies generates invalid
    # find_package(launch REQUIRED) lines on Humble.
    package_xml = ET.fromstring((package / 'package.xml').read_text())
    for dependency in ('launch', 'launch_ros', 'ament_index_python', 'soem_wrapper'):
        if not any(element.text == dependency for element in package_xml):
            item = ET.Element('exec_depend')
            item.text = dependency
            package_xml.insert(list(package_xml).index(package_xml.find('export')) if package_xml.find('export') is not None else len(package_xml), item)
    ET.indent(package_xml, space='  ')
    targets['package.xml'] = '<?xml version="1.0"?>\n' + ET.tostring(package_xml, encoding='unicode') + '\n'
    stamp = str(time.time_ns())
    for relative, content in targets.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text() != content:
            shutil.copy2(target, target.with_name(target.name + '.bak-' + stamp))
        target.write_text(content)
    atomic_json(marker, {'version': 1, 'configuration_ready': configuration_ready,
                         'hashes': {rel: sha256_file(package / rel) for rel in targets}})
    return {'workspace': str(workspace), 'config': str(package / 'config/config.yaml'),
            'launch': str(package / 'launch/bringup.launch.py'),
            'message': 'soem_bringup 已生成：教程 SN 发现示例、当前网卡和完整核心设置已写入。'}


def build_workspace(args):
    require_user()
    workspace = validate_workspace(args.workspace)
    if not (workspace / 'src/soem_bringup/package.xml').is_file():
        raise ValueError('请先创建 soem_bringup')
    ensure_no_master()
    source_config = workspace / 'src/soem_bringup/config/config.yaml'
    source_hash = sha256_file(source_config)
    expected = getattr(args, 'expected_config_hash', '')
    if expected and source_hash != expected:
        raise ValueError('源 config.yaml 已变化，请重新应用任务修改再编译')
    ros_command(args.ros_setup, ['colcon', 'build', '--event-handlers', 'console_direct+'], workspace, timeout=7200)
    installed = workspace / 'install/soem_bringup/share/soem_bringup/launch/bringup.launch.py'
    if not installed.is_file() or not (workspace / 'install/setup.bash').is_file():
        raise RuntimeError('colcon 已退出，但未找到安装后的 bringup 文件')
    installed_config = workspace / 'install/soem_bringup/share/soem_bringup/config/config.yaml'
    if sha256_file(source_config) != source_hash or not installed_config.is_file() or sha256_file(installed_config) != source_hash:
        raise RuntimeError('源配置或安装配置与本次编译不一致，不能确认 config.yaml 已更新，请重新编译')
    return {'workspace': str(workspace), 'config': str(source_config), 'installed_config': str(installed_config),
            'config_hash': source_hash,
            'message': 'colcon build 成功，安装后的 config.yaml 已与源配置核对一致。请重新运行主站加载新配置。'}


def root_shell(args):
    require_root()
    workspace = verify_workspace_install(args.workspace)
    setup = Path(args.ros_setup).resolve()
    if not re.fullmatch(r'/opt/ros/[A-Za-z0-9_-]+/setup\.bash', str(setup)) or not setup.is_file():
        raise ValueError('ROS setup 路径不合法')
    if not (workspace / 'install/setup.bash').is_file():
        raise ValueError('工作空间尚未成功构建')
    metadata = ensure_workspace_metadata(workspace)
    fd, rc = tempfile.mkstemp(prefix='ecat-first-run-root-', suffix='.bash')
    try:
        with os.fdopen(fd, 'w') as out:
            # Keep launch/backend logs beside this workspace so diagnostics do
            # not require access to /root/.ros after the terminal is closed.
            out.write('umask 022\n')
            log_dir = metadata / 'ros-logs'
            out.write('export ROS_LOG_DIR=' + shlex.quote(str(log_dir)) + '\n')
            out.write('source ' + shlex.quote(str(setup)) + '\n')
            out.write('source ' + shlex.quote(str(workspace / 'install/setup.bash')) + '\n')
            out.write('cd -- ' + shlex.quote(str(workspace)) + '\n')
            out.write(f'export ROS_DOMAIN_ID={int(args.domain)}\n')
            out.write("export PS1='(EtherCAT root) \\w # '\n")
            out.write("printf '\\n工作空间已加载，请手动执行：\\nros2 launch soem_bringup bringup.launch.py\\n\\n'\n")
            out.write("printf 'ROS 日志目录：%s\\n' \"$ROS_LOG_DIR\"\n")
        subprocess.call(['/bin/bash', '--noprofile', '--rcfile', rc, '-i'], cwd=workspace)
    finally:
        Path(rc).unlink(missing_ok=True)
    return {'message': '首次运行 root 终端已退出'}


def flash_eeprom(args):
    require_root()
    ensure_no_master()
    workspace = validate_workspace(args.workspace)
    interface = validate_interface(args.interface)
    if not 1 <= args.slave <= 255:
        raise ValueError('EtherCAT 从站位置必须为 1–255')
    tool = verified_eeprom_tool(workspace)
    image = Path(args.image).resolve()
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise ValueError('逐字校验工具不存在或没有执行权限，请先准备工具')
    if image.suffix.lower() != '.bin' or not image.is_file():
        raise ValueError('请选择有效的 EEPROM .bin 文件')
    contents = image.read_bytes()
    if len(contents) < 128 or len(contents) > 131072 or len(contents) % 2:
        raise ValueError('EEPROM 镜像大小不合法')
    backup = BACKUP_ROOT / 'eeprom' / (time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000))
    backup.mkdir(parents=True, mode=0o700)
    old = backup / 'before.bin'
    run([tool, interface, args.slave, '-r', old], timeout=180)
    if not old.is_file() or old.stat().st_size < 128:
        raise RuntimeError('EEPROM 备份失败，未执行写入。请检查网卡、从站位置和供电')
    # eepromtool returns zero even on some errors: verify actual readback bytes.
    actual = backup / 'after.bin'
    try:
        written = run([tool, interface, args.slave, '-w', image], timeout=300)
    except (RuntimeError, TimeoutError) as exc:
        diagnostic = {'stage': 'write', 'error': str(exc)}
        try:
            # Diagnose the partial state without performing another write.
            run([tool, interface, args.slave, '-r', actual], timeout=180)
            diagnostic.update(eeprom_difference(contents, actual.read_bytes()))
        except (RuntimeError, TimeoutError, OSError) as read_error:
            diagnostic['readback_error'] = str(read_error)
        atomic_json(backup / 'failure.json', diagnostic)
        remaining = f'，剩余 {diagnostic["different_bytes"]} 字节不同' if 'different_bytes' in diagnostic else ''
        raise RuntimeError(f'EEPROM 逐字补写未通过校验{remaining}。备份与诊断保留在 {backup}；请查看任务日志中的偏移、状态码和重试次数') from exc
    if 'EEPROM_WRITE_VERIFIED' not in written:
        raise RuntimeError(f'写入工具未报告完整校验成功。备份保留于 {old}')
    run([tool, interface, args.slave, '-r', actual], timeout=180)
    if not actual.is_file() or actual.read_bytes()[:len(contents)] != contents:
        diff = eeprom_difference(contents, actual.read_bytes() if actual.is_file() else b'')
        atomic_json(backup / 'failure.json', diff)
        raise RuntimeError(f'EEPROM 回读仍有 {diff["different_bytes"]} 字节不一致，不能确认成功。原始备份在 {old}，详情在 failure.json')
    atomic_json(backup / 'manifest.json', {'interface': interface, 'slave': args.slave,
                                         'image': str(image), 'sha256': sha256_file(image)})
    return {'message': 'EEPROM 写入及回读比对成功，请将 EtherCAT 板逻辑电源断电再上电。', 'backup': str(backup)}


def eeprom_difference(expected, actual):
    offsets = [i for i in range(len(expected)) if i >= len(actual) or expected[i] != actual[i]]
    return {'expected_bytes': len(expected), 'actual_bytes': len(actual), 'different_bytes': len(offsets),
            'different_words': len({i // 2 for i in offsets}),
            'first_differences': [{'offset': i, 'offset_hex': hex(i), 'expected': expected[i],
                                   'actual': actual[i] if i < len(actual) else None} for i in offsets[:32]]}


def prepare_eeprom_tool(args):
    require_user()
    workspace = validate_workspace(args.workspace)
    tool = build_eeprom_tool(workspace, run)
    return {'tool': str(tool), 'message': '逐字校验 EEPROM 工具已就绪，将检查每次传输、补写不一致位置并进行有限重试。'}


def diagnose_eeprom(args):
    require_root()
    ensure_no_master()
    workspace = validate_workspace(args.workspace)
    interface = validate_interface(args.interface)
    if not 1 <= args.slave <= 255:
        raise ValueError('从站位置应为 1–255')
    tool = verified_eeprom_tool(workspace)
    image = Path(args.image).resolve()
    expected = image.read_bytes()
    if not 128 <= len(expected) <= 131072:
        raise ValueError('镜像长度不合法')
    folder = BACKUP_ROOT / 'eeprom-diagnostics' / str(time.time_ns())
    folder.mkdir(parents=True, mode=0o700)
    output = folder / 'readback.bin'
    run([tool, interface, args.slave, '-r', output], timeout=180)
    diff = eeprom_difference(expected, output.read_bytes())
    atomic_json(folder / 'report.json', diff)
    print(json.dumps(diff, ensure_ascii=False, indent=2), flush=True)
    return {**diff, 'matches_image': diff['different_bytes'] == 0, 'report': str(folder / 'report.json'),
            'message': f'只读诊断完成：与所选镜像有 {diff["different_bytes"]} 字节 / {diff["different_words"]} 个16位字不同。本次未写入 EEPROM。'}


def selected_stlink(serial):
    links = detect_stlinks()
    if not links:
        raise ValueError('未检测到 ST-Link，请连接 ST-Link USB、SWD 与目标板逻辑供电')
    if serial:
        if not any(s['serial'] == serial for s in links):
            raise ValueError('所选 ST-Link 已断开，请重新检测')
        return serial
    if len(links) != 1:
        raise ValueError('检测到多个 ST-Link，请在界面选择具体序列号')
    return links[0]['serial']


def probe_mcu(args):
    require_root()
    ensure_no_master()
    serial = selected_stlink(args.serial)
    output = run(openocd_commands(args.target, serial=serial, speed=args.speed), timeout=60)
    if not re.search(r'\bhalted\b', output, re.I):
        raise RuntimeError('OpenOCD 未确认目标 halted，请检查 SWD 和供电')
    return {'message': 'MCU 已暂停并完成 device ID / Flash 检查。尚未擦除或烧录；目标保持暂停。', 'serial': serial}


def flash_mcu(args):
    require_root()
    ensure_no_master()
    serial = selected_stlink(args.serial)
    image = Path(args.image).resolve()
    base = int(args.base, 0)
    flash_ranges(image, base)
    if args.expected_hash and sha256_file(image) != args.expected_hash:
        raise ValueError('固件与编译记录的 SHA256 不一致，请重新编译或选择原始产物')
    # Probe first; then reconnect to this exact probe and assert device ID again
    # in the same OpenOCD sequence that performs erase/write/verify.
    probe = run(openocd_commands(args.target, serial=serial, speed=args.speed), timeout=60)
    if not re.search(r'\bhalted\b', probe, re.I):
        raise RuntimeError('未能 halt 目标，未执行擦写')
    output = run(openocd_commands(args.target, image, serial, args.speed, base), timeout=300)
    if not flash_verified(output):
        raise RuntimeError('未同时得到写入和回读校验成功标记，不能确认烧录成功')
    message = '写入与回读校验成功。'
    message += 'H750 未自动复位，请按板载 RESET 或重新上电。' if args.target == 'h750' else 'STM32G431 已复位运行，请核对 IMU 实际输出。'
    return {'message': message, 'image': str(image), 'sha256': sha256_file(image), 'serial': serial}


TOOL_PACKAGES = {
    # binutils and newlib headers are hard dependencies of these packages.
    # H750 builds its single-architecture C++ runtime in the user's cache.
    'imu': ('cmake', 'make', 'gcc-arm-none-eabi', 'libnewlib-arm-none-eabi', 'python3-venv'),
    'flash': ('openocd',),
    'h750': ('cmake', 'make', 'gcc-arm-none-eabi', 'libnewlib-arm-none-eabi', 'python3-venv'),
}


def install_tools(args):
    require_root()
    tool_set = getattr(args, 'tool_set', 'imu')
    packages = TOOL_PACKAGES[tool_set]
    query = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Status}\n', *packages],
                           capture_output=True, text=True, timeout=30)
    installed = {fields[0].split(':')[0] for line in query.stdout.splitlines()
                 if len(fields := line.split('\t', 1)) == 2 and fields[1] == 'install ok installed'}
    missing = [package for package in packages if package not in installed]
    if missing:
        print('仅安装缺失的依赖：' + ', '.join(missing) + '；不安装推荐软件包。', flush=True)
        run(['apt-get', 'update'], timeout=1800)
        run(['apt-get', 'install', '-y', '--no-install-recommends', '--no-upgrade', *missing], timeout=7200)
    label = {'imu': 'IMU 编译依赖', 'flash': 'OpenOCD 烧录工具', 'h750': 'H750 轻量基础依赖'}[tool_set]
    return {'tool_set': tool_set, 'packages': missing,
            'message': label + ('已安装。' if missing else '已齐全，无需下载。') +
            ('不安装 ARM C++ 标准库。CMake 不足时，编译按钮会准备当前用户独立的 CMake 环境。' if tool_set == 'imu' else '')}


def prepare_imu(args):
    require_user()
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'clone', IMU_URL, repo], timeout=1800)
    if not (repo / 'Core/Src/main.c').is_file() or not (repo / 'CMakeLists.txt').is_file():
        raise ValueError('所选目录不是可识别的 hipnucimu 工程，请选择正确目录')
    patch_imu_source((repo / 'Core/Src/main.c').read_text(), [1, 2, 3])
    run(['git', 'submodule', 'update', '--init', '--recursive'], cwd=repo, timeout=1800)
    return {'repo': str(repo), 'message': 'hipnucimu 源码已准备好；原目录中的 CAN ID 不会被修改。'}


def suitable_cmake(repo, tool_root):
    text = (repo / 'CMakeLists.txt').read_text()
    match = re.search(r'cmake_minimum_required\s*\(\s*VERSION\s+(\d+)\.(\d+)', text, re.I)
    required = tuple(map(int, match.groups())) if match else (3, 29)
    local = tool_root / 'bin/cmake'
    candidates = [str(local), shutil.which('cmake') or '']
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        check = subprocess.run([candidate, '--version'], capture_output=True, text=True, timeout=15)
        m = re.search(r'cmake version (\d+)\.(\d+)', check.stdout)
        if check.returncode == 0 and m and tuple(map(int, m.groups())) >= required:
            return candidate
    print(f'当前 CMake 不满足 {required[0]}.{required[1]}，准备当前用户独立工具环境。', flush=True)
    if not (tool_root / 'bin/python').is_file():
        run([sys.executable, '-m', 'venv', tool_root], timeout=180)
    requirement = f'cmake>={required[0]}.{required[1]},<4'
    run([tool_root / 'bin/python', '-m', 'pip', 'install', requirement], timeout=1800)
    return str(local)


def compile_imu(args):
    require_user()
    profiles = json.loads(args.profiles)
    validate_imu_ids(profiles)
    if not 1 <= args.profile <= len(profiles):
        raise ValueError('IMU 序号超出范围')
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / 'Core/Src/main.c').is_file():
        raise ValueError('请先准备 hipnucimu 源码')
    if not shutil.which('arm-none-eabi-gcc'):
        raise ValueError('缺少 ARM 交叉编译工具链，请点击安装编译/烧录工具')
    ids = profiles[args.profile - 1]
    output_root = Path(args.output_root).expanduser().resolve()
    cmake = suitable_cmake(repo, output_root / 'toolchain')
    stamp = str(time.time_ns())
    name = 'imu' + str(args.profile) + '-' + '-'.join(f'{x:03x}' for x in ids) + '-' + stamp
    variant = output_root / name
    source = variant / 'source'
    shutil.copytree(repo, source, ignore=shutil.ignore_patterns('.git', '.idea', '__pycache__', 'build', 'cmake-build-*', '*.elf', '*.bin', '*.hex', '*.zip'))
    main = source / 'Core/Src/main.c'
    main.write_text(patch_imu_source(main.read_text(), ids))
    build = variant / 'build/Release'
    run([cmake, '-S', source, '-B', build, '-G', 'Unix Makefiles', '-DCMAKE_BUILD_TYPE=Release'], timeout=180)
    run([cmake, '--build', build, '--target', 'hipnucimu.elf', '--parallel', min(4, os.cpu_count() or 1)], timeout=900)
    image = build / 'hipnucimu.elf'
    flash_ranges(image)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True)
    data = {'profile': args.profile, 'ids': ids, 'image': str(image), 'sha256': sha256_file(image),
            'source_commit': head.stdout.strip(), 'source': str(source), 'built_at': stamp,
            'message': f'IMU {args.profile} 已编译成功，CAN ID: ' + ', '.join(hex(x) for x in ids)}
    atomic_json(variant / 'manifest.json', data)
    return data


H750_URL = 'https://github.com/AIMEtherCAT/EcatV2_AX58100_H750_Universal.git'
H750_TARGET = 'EcatV2_AX58100_H750_Universal'


def h750_source(path):
    if not str(path).strip():
        raise ValueError('请选择 H750 源码目录')
    source = Path(path).expanduser().resolve()
    if not (source / 'CMakeLists.txt').is_file():
        raise ValueError('源码目录缺少 CMakeLists.txt，请选择 CMake 工程根目录')
    markers = list(source.glob('*H750*.ld')) + list(source.glob('*h750*.ld'))
    if not markers and not any('STM32H750' in p.read_text(errors='replace') for p in source.glob('*.ioc')):
        raise ValueError('未识别到 STM32H750 的链接脚本或 IOC 配置，请核对源码目录')
    return source


def prepare_h750(args):
    require_user()
    if not str(args.repo).strip():
        raise ValueError('请填写 H750 源码路径')
    repo = Path(args.repo).expanduser().resolve()
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        run(['git', 'clone', '--recurse-submodules', H750_URL, repo], timeout=1800)
    h750_source(repo)
    if (repo / '.git').exists():
        run(['git', 'submodule', 'update', '--init', '--recursive'], cwd=repo, timeout=1800)
    return {'repo': str(repo), 'message': 'H750 源码已检查，现有本地修改已保留；可以编译固件。'}


def compile_h750(args):
    require_user()
    repo = h750_source(args.repo)
    output_root = Path(args.output_root).expanduser().resolve()
    if not str(args.output_root).strip() or output_root.is_relative_to(repo):
        raise ValueError('编译输出目录必须位于源码目录之外，避免快照递归复制')
    build_type = getattr(args, 'build_type', 'Release')
    target = getattr(args, 'build_target', H750_TARGET).strip()
    if build_type not in ('Release', 'Debug', 'MinSizeRel', 'RelWithDebInfo'):
        raise ValueError('构建类型不合法')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}', target):
        raise ValueError('请填写有效的 CMake 目标名称')
    toolchain_bin = getattr(args, 'toolchain_bin', '').strip()
    env = os.environ.copy()
    if toolchain_bin:
        compiler_dir = Path(toolchain_bin).expanduser().resolve()
        if not compiler_dir.is_dir():
            raise ValueError('ARM 工具链 bin 目录不存在')
        env['PATH'] = str(compiler_dir) + os.pathsep + env.get('PATH', '')
        toolchain_bin = str(compiler_dir)
    gcc = shutil.which('arm-none-eabi-gcc', path=env.get('PATH'))
    if not gcc or not shutil.which('arm-none-eabi-objcopy', path=env.get('PATH')):
        raise ValueError('缺少 ARM 编译工具，请选择工具链 bin 目录或安装 H750 编译依赖')
    toolchain_value = getattr(args, 'toolchain_file', '').strip()
    toolchain = Path(toolchain_value).expanduser() if toolchain_value else repo / 'cmake/gcc-arm-none-eabi.cmake'
    if not toolchain.is_absolute():
        toolchain = repo / toolchain
    toolchain = toolchain.resolve()
    if not toolchain.is_file():
        raise ValueError('找不到 CMake 工具链文件，请选择 gcc-arm-none-eabi.cmake')
    extra_cmake, runtime_info = [], None
    cpp_sources = any(p.suffix in ('.cpp', '.cc', '.cxx') and
                      not any(part.startswith(('build', 'cmake-build', '.')) for part in p.relative_to(repo).parts)
                      for p in repo.rglob('*') if p.is_file())
    if cpp_sources:
        gpp = shutil.which('arm-none-eabi-g++', path=env.get('PATH'))
        if not gpp:
            raise ValueError('H750 源码包含 C++，所选工具链缺少 arm-none-eabi-g++')
        flags = ['-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard']
        standard_match = re.search(r'set\(\s*CMAKE_CXX_STANDARD\s+(\d+)', (repo / 'CMakeLists.txt').read_text(), re.I)
        standard = int(standard_match[1]) if standard_match else 20
        check_source = '#include <memory>\n' + ('#include <ranges>\n' if standard >= 20 else '') + 'int main() { return 0; }\n'
        check = subprocess.run([gpp, *flags, '-std=c++' + str(standard), '-fsyntax-only', '-x', 'c++', '-'],
                               input=check_source,
                               capture_output=True, text=True, timeout=30, env=env)
        library = subprocess.run([gpp, *flags, '-print-file-name=libstdc++.a'],
                                 capture_output=True, text=True, timeout=30, env=env)
        if check.returncode or not Path(library.stdout.strip()).is_file():
            configuration = toolchain.read_text() + (repo / 'CMakeLists.txt').read_text()
            required_flags = ('-fno-exceptions', '-fno-rtti', '-fno-threadsafe-statics',
                              '-mcpu=cortex-m7', '-mfpu=fpv5-d16', '-mfloat-abi=hard')
            if not all(flag in configuration for flag in required_flags):
                raise ValueError('轻量 H750 运行库用于 Cortex-M7 hard-float、关闭异常和 RTTI 及线程安全静态初始化的工程；此自定义工程请使用完整工具链，不会下载大型库包')
            env, linker_flags, runtime_info = prepare_h750_runtime(gpp, output_root / 'toolchain/h750-runtime', env, run,
                                                                 needs_headers=bool(check.returncode))
            verified = subprocess.run([gpp, *flags, '-std=c++' + str(standard), '-fsyntax-only', '-x', 'c++', '-'],
                                      input=check_source, capture_output=True, text=True, timeout=30, env=env)
            if verified.returncode:
                raise ValueError('ARM C++ 头文件检查未通过：' + verified.stderr[-1500:])
            extra_cmake = ['-DCMAKE_CXX_STANDARD_LIBRARIES=' + linker_flags]
    modules = repo / '.gitmodules'
    if modules.is_file() and 'ecat/soes' in modules.read_text() and not (repo / 'ecat/soes/soes/esc.c').is_file():
        raise ValueError('SOES 子模块尚未就绪，请先点击获取 / 检查 H750 源码')
    cmake = suitable_cmake(repo, output_root / 'toolchain')
    stamp = str(time.time_ns())
    variant = output_root / ('h750-' + stamp)
    source = variant / 'source'
    shutil.copytree(repo, source, ignore=shutil.ignore_patterns('.git', '.idea', '__pycache__', 'build', 'build-*',
                                                             'cmake-build-*', '*.elf', '*.bin', '*.hex', '*.zip'))
    selected_toolchain = source / toolchain.relative_to(repo) if toolchain.is_relative_to(repo) else toolchain
    build = variant / 'build' / build_type
    run([cmake, '-S', source, '-B', build, '-G', 'Unix Makefiles', '-DCMAKE_BUILD_TYPE=' + build_type,
         '-DCMAKE_TOOLCHAIN_FILE=' + str(selected_toolchain), *extra_cmake], timeout=180, env=env)
    run([cmake, '--build', build, '--target', target, '--parallel', min(4, os.cpu_count() or 1)], timeout=1200, env=env)
    matches = [p for p in build.rglob('*.elf') if p.name in (target, target + '.elf')]
    if len(matches) != 1:
        raise ValueError('没有找到唯一的目标 ELF，请核对 CMake 目标名称；不会使用旧固件')
    image = matches[0]
    flash_ranges(image)  # ARM ELF32 and H750 internal Flash bounds before offering it for flashing.
    objcopy = shutil.which('arm-none-eabi-objcopy', path=env.get('PATH'))
    binary, ihex = image.with_suffix('.bin'), image.with_suffix('.hex')
    run([objcopy, '-O', 'binary', image, binary], env=env, timeout=60)
    run([objcopy, '-O', 'ihex', image, ihex], env=env, timeout=60)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True)
    data = {'repo': str(repo), 'image': str(image), 'bin': str(binary), 'hex': str(ihex),
            'sha256': sha256_file(image), 'source_commit': head.stdout.strip(), 'source': str(source),
            'build_type': build_type, 'build_target': target, 'toolchain_file': toolchain_value,
            'toolchain_bin': toolchain_bin, 'output_root': str(output_root), 'built_at': stamp,
            'runtime': runtime_info,
            'message': 'H750 固件编译成功，ELF 已校验并生成 BIN / HEX；可以在现有烧录窗口中选择烧录。'}
    atomic_json(variant / 'manifest.json', data)
    return data


ACTIONS = {'inspect': inspect, 'enable-rt': enable_rt, 'attach-pro': attach_pro,
           'isolate-cpu': isolate_cpu, 'create-workspace': create_workspace,
           'create-bringup': create_bringup, 'build-workspace': build_workspace,
           'root-shell': root_shell, 'repair-workspace-access': repair_workspace_access, 'flash-eeprom': flash_eeprom,
           'prepare-eeprom-tool': prepare_eeprom_tool, 'diagnose-eeprom': diagnose_eeprom,
           'probe-mcu': probe_mcu, 'flash-mcu': flash_mcu, 'install-tools': install_tools,
           'prepare-imu': prepare_imu, 'compile-imu': compile_imu,
           'prepare-h750': prepare_h750, 'compile-h750': compile_h750}


def main(argv=None):
    def interrupted(signum, frame):
        raise JobCancelled('任务被取消')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description='Ecat GroundStation first-run job')
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--name', default='EGC_ws')
    parser.add_argument('--workspace', default='')
    parser.add_argument('--interface', default='')
    parser.add_argument('--slave', type=int, default=1)
    parser.add_argument('--image', default='')
    parser.add_argument('--ros-setup', default='/opt/ros/humble/setup.bash')
    parser.add_argument('--rt-cpu', type=int, default=0)
    parser.add_argument('--non-rt-cpus', default='1')
    parser.add_argument('--domain', type=int, default=0)
    parser.add_argument('--target', choices=('h750', 'g431'), default='h750')
    parser.add_argument('--tool-set', choices=('imu', 'flash', 'h750'), default='imu')
    parser.add_argument('--toolchain-file', default='')
    parser.add_argument('--toolchain-bin', default='')
    parser.add_argument('--build-type', default='Release')
    parser.add_argument('--build-target', default=H750_TARGET)
    parser.add_argument('--serial', default='')
    parser.add_argument('--speed', type=int, default=400)
    parser.add_argument('--base', default='0x08000000')
    parser.add_argument('--expected-hash', default='')
    parser.add_argument('--expected-config-hash', default='')
    parser.add_argument('--repo', default='')
    parser.add_argument('--profiles', default='[[1,2,3],[4,5,6],[7,8,9]]')
    parser.add_argument('--profile', type=int, default=1)
    parser.add_argument('--output-root', default='')
    args = parser.parse_args(argv)
    try:
        result(ACTIONS[args.action](args))
        return 0
    except Exception as exc:
        message = f'操作失败: {type(exc).__name__}: {exc}'
        print(message, flush=True)
        print('@@ECAT_ERROR@@' + json.dumps({'message': message, 'error_type': type(exc).__name__}, ensure_ascii=False), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
