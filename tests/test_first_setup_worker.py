"""Destructive operations use simulated tools and temporary paths exclusively."""
from argparse import Namespace
from pathlib import Path
import json
import os
import shutil
import subprocess
import signal
import time
import selectors

import pytest

from ecat_groundstation import setup_worker as worker
from ecat_groundstation.setup_core import FIRST_RUN_YAML, sha256_file
from ecat_groundstation.setup_core import Cpu


def git(directory, *args):
    return subprocess.check_output(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=directory, text=True)


@pytest.mark.parametrize('tool_set,missing', [('imu', ['gcc-arm-none-eabi', 'libnewlib-arm-none-eabi']), ('flash', ['openocd']), ('imu', [])])
def test_install_only_missing_minimal_packages_without_recommends(monkeypatch, tool_set, missing):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    packages = worker.TOOL_PACKAGES[tool_set]

    def status(command, **kwargs):
        assert command[0] == 'dpkg-query'
        output = ''.join(package + '\tinstall ok installed\n' for package in packages if package not in missing)
        return subprocess.CompletedProcess(command, 1 if missing else 0, stdout=output, stderr='')

    monkeypatch.setattr(worker.subprocess, 'run', status)
    commands = []
    monkeypatch.setattr(worker, 'run', lambda command, **kwargs: commands.append(command))
    result = worker.install_tools(Namespace(tool_set=tool_set))
    assert result['packages'] == missing
    if missing:
        assert len(commands) == 2 and commands[0] == ['apt-get', 'update']
        assert commands[1] == ['apt-get', 'install', '-y', '--no-install-recommends', '--no-upgrade', *missing]
    else:
        assert not commands and '无需下载' in result['message']
    forbidden = {'ninja-build', 'usbutils', 'build-essential', 'libstdc++-arm-none-eabi-newlib'}
    assert not set(packages) & forbidden
    if tool_set == 'flash':
        assert packages == ('openocd',)


def test_worker_termination_exits_selector_wait_and_reaps_child(tmp_path):
    # A real process and pipe wait reproduce the old swallowed InterruptedError.
    script = tmp_path / 'cancel-worker.py'
    script.write_text('''import sys\nfrom ecat_groundstation import setup_worker as worker\ndef job(args):\n return worker.run([sys.executable, '-u', '-c', 'import os,time; print("CHILD_PID="+str(os.getpid()),flush=True); time.sleep(60)'])\nworker.ACTIONS['inspect']=job\nraise SystemExit(worker.main(['inspect']))\n''')
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    process = subprocess.Popen(['/usr/bin/python3', '-u', str(script)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    child_pid = None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = b''
    try:
        deadline = time.monotonic() + 5
        while child_pid is None and time.monotonic() < deadline:
            for key, _ in selector.select(.1):
                output += os.read(key.fd, 65536)
                import re
                match = re.search(rb'\nCHILD_PID=(\d+)\n', output)
                if match:
                    child_pid = int(match[1])
        assert child_pid is not None, output.decode()
        process.terminate()
        remainder, _ = process.communicate(timeout=7)
        assert process.returncode == 1 and '任务被取消' in remainder.decode()
        assert not Path(f'/proc/{child_pid}').exists()
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        if child_pid is not None and Path(f'/proc/{child_pid}').exists():
            os.killpg(child_pid, signal.SIGKILL)


def test_workspace_git_submodule_recursive_and_retry(tmp_path, monkeypatch):
    # Real Git operations against local fixture repositories, with no network.
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    inner = tmp_path / 'soem-origin'
    inner.mkdir()
    git(inner, 'init')
    (inner / 'soem.c').write_text('/* fixture only */\n')
    git(inner, 'add', '.')
    git(inner, 'commit', '-m', 'fixture')
    outer = tmp_path / 'master-origin'
    outer.mkdir()
    git(outer, 'init')
    (outer / 'tools').mkdir()
    (outer / 'tools/eepromtool').write_text('fixture, never execute')
    git(outer, 'submodule', 'add', str(inner), 'src/soem/soem')
    git(outer, 'add', '.')
    git(outer, 'commit', '-m', 'fixture with nested soem')
    target = tmp_path / 'EGC_ws'
    monkeypatch.setattr(worker, 'workspace_path', lambda name: target)
    monkeypatch.setattr(worker, 'MASTER_URL', str(outer))
    result = worker.create_workspace(Namespace(name='EGC_ws'))
    assert result['workspace'] == str(target)
    assert (target / 'src/EcatV2_Master/src/soem/soem/soem.c').is_file()
    assert os.access(target / 'src/EcatV2_Master/tools/eepromtool', os.X_OK)
    assert (target / '.ecat-groundstation').stat().st_uid == target.stat().st_uid
    assert worker.create_workspace(Namespace(name='EGC_ws'))['workspace'] == str(target)


@pytest.fixture
def bringup_workspace(tmp_path):
    workspace = tmp_path / 'EGC_ws'
    (workspace / 'src/EcatV2_Master/src/soem_wrapper').mkdir(parents=True)
    return workspace


def fake_ros_create(setup, command, workspace, timeout=3600):
    package = workspace / 'soem_bringup'
    package.mkdir()
    (package / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.8)\nproject(soem_bringup)\nfind_package(ament_cmake REQUIRED)\nament_package()\n')
    (package / 'package.xml').write_text('<package format="3"><name>soem_bringup</name><export><build_type>ament_cmake</build_type></export></package>')


def bringup_args(workspace):
    return Namespace(workspace=str(workspace), interface='enp3s0', rt_cpu=6, non_rt_cpus='0-5', ros_setup='/opt/ros/humble/setup.bash')


def test_bringup_creation_updates_and_protects_user_changes(bringup_workspace, monkeypatch):
    monkeypatch.setattr(worker, 'ros_command', fake_ros_create)
    args = bringup_args(bringup_workspace)
    first = worker.create_bringup(args)
    config = Path(first['config'])
    assert config.read_text() == FIRST_RUN_YAML
    assert 'install(DIRECTORY' in (config.parent.parent / 'CMakeLists.txt').read_text()
    assert "'rt_cpu': 6" in Path(first['launch']).read_text()
    worker.create_bringup(args)  # Managed and unchanged: safe retry.
    config.write_text('user changed SN and tasks\n')
    with pytest.raises(ValueError, match='用户修改'):
        worker.create_bringup(args)
    assert config.read_text() == 'user changed SN and tasks\n'


def test_unmanaged_existing_bringup_never_overwritten(bringup_workspace, monkeypatch):
    (bringup_workspace / 'src/soem_bringup').mkdir()
    with pytest.raises(ValueError, match='非本软件生成'):
        worker.create_bringup(bringup_args(bringup_workspace))


def test_root_shell_keeps_logs_readable_in_workspace_and_cleans_rc(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace with spaces'
    (workspace / 'install').mkdir(parents=True)
    (workspace / 'install/setup.bash').write_text('# fixture\n')
    for relative in ('config/config.yaml', 'launch/bringup.launch.py'):
        for package in (workspace / 'src/soem_bringup', workspace / 'install/soem_bringup/share/soem_bringup'):
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# fixture\n')
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: str(path) == '/opt/ros/humble/setup.bash' or original_is_file(path))
    calls = []

    def shell(command, cwd):
        # Inspect the startup file without running a root shell or any launch.
        rc = Path(command[3])
        content = rc.read_text()
        assert 'umask 022\n' in content
        assert 'export ROS_LOG_DIR=' in content and '.ecat-groundstation/ros-logs' in content
        assert 'export ROS_DOMAIN_ID=24\n' in content
        assert str(workspace / 'install/setup.bash') in content
        assert '--noprofile' in command and cwd == workspace
        assert (workspace / '.ecat-groundstation').stat().st_uid == workspace.stat().st_uid
        calls.append(rc)
        return 0

    monkeypatch.setattr(worker.subprocess, 'call', shell)
    worker.root_shell(Namespace(workspace=str(workspace), ros_setup='/opt/ros/humble/setup.bash', domain=24))
    assert len(calls) == 1 and not calls[0].exists()


def test_repair_only_changes_root_owned_metadata_not_source_or_logs(bringup_workspace, monkeypatch):
    workspace = bringup_workspace
    package = workspace / 'src/soem_bringup'
    package.mkdir()
    (package / 'package.xml').write_text('<package/>')
    metadata = workspace / '.ecat-groundstation'
    (metadata / 'ros-logs').mkdir(parents=True)
    log = metadata / 'ros-logs/launch.log'
    log.write_text('preserve existing root launch log')
    owner = workspace.stat()
    original_stat = Path.stat

    def root_owned(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == metadata:
            fields = list(value)
            fields[4] = 0
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'stat', root_owned)
    monkeypatch.setattr(worker.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('PKEXEC_UID', str(owner.st_uid))
    changed = []
    monkeypatch.setattr(worker.os, 'chown', lambda *args, **kwargs: changed.append((args, kwargs)))
    worker.repair_workspace_access(Namespace(workspace=str(workspace)))
    assert changed == [((metadata, owner.st_uid, owner.st_gid), {'follow_symlinks': False})]
    assert log.read_text() == 'preserve existing root launch log'
    assert (package / 'package.xml').read_text() == '<package/>'


def test_metadata_repair_rejects_symlink_and_other_workspace_owner(bringup_workspace, tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (bringup_workspace / '.ecat-groundstation').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        worker.ensure_workspace_metadata(bringup_workspace)
    monkeypatch.setattr(worker.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('PKEXEC_UID', str(bringup_workspace.stat().st_uid + 1))
    with pytest.raises(ValueError, match='当前登录用户'):
        worker.ensure_workspace_metadata(bringup_workspace)


@pytest.mark.parametrize('readback_matches', [True, False])
def test_eeprom_backup_and_verify_even_if_tool_returns_zero(tmp_path, monkeypatch, readback_matches):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    monkeypatch.setattr(worker, 'validate_interface', lambda name: name)
    monkeypatch.setattr(worker, 'BACKUP_ROOT', tmp_path / 'backups')
    ws = tmp_path / 'ws'
    tool = ws / 'src/EcatV2_Master/tools/eepromtool'
    tool.parent.mkdir(parents=True)
    tool.write_text('fixture only, never executed')
    tool.chmod(0o755)
    monkeypatch.setattr(worker, 'verified_eeprom_tool', lambda workspace: tool)
    image = tmp_path / 'known.bin'
    image.write_bytes(b'\x12\x34' * 1024)
    calls = []

    def simulated_tool(args, **kwargs):
        calls.append(list(map(str, args)))
        if args[3] == '-r':
            data = image.read_bytes() if len(calls) > 1 and readback_matches else b'\x00' * 2048
            Path(args[4]).write_bytes(data)
        return 'EEPROM_WRITE_VERIFIED bytes=2048' if args[3] == '-w' else 'EEPROM_READ_VERIFIED bytes=2048'

    monkeypatch.setattr(worker, 'run', simulated_tool)
    args = Namespace(workspace=str(ws), interface='enp3s0', slave=1, image=str(image))
    if readback_matches:
        result = worker.flash_eeprom(args)
        assert '比对成功' in result['message']
    else:
        with pytest.raises(RuntimeError, match='不一致'):
            worker.flash_eeprom(args)
    assert [c[3] for c in calls] == ['-r', '-w', '-r']
    assert any((tmp_path / 'backups').glob('eeprom/*/before.bin'))


@pytest.mark.parametrize('output,ok', [('wrote 32 bytes\nverified 32 bytes\nshutdown', True), ('shutdown command invoked', False)])
def test_flash_mcu_result_requires_write_verify_and_hash(tmp_path, monkeypatch, output, ok):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    monkeypatch.setattr(worker, 'selected_stlink', lambda serial: 'TEST_SERIAL')
    image = tmp_path / 'firmware.bin'
    image.write_bytes(b'\x11' * 32)
    calls = []
    def simulated_openocd(args, **kwargs):
        calls.append(args)
        return output if any('flash write_image' in str(x) for x in args) else 'target halted\nDevice ID matched\nsize 128kb'
    monkeypatch.setattr(worker, 'run', simulated_openocd)
    args = Namespace(serial='TEST_SERIAL', image=str(image), base='0x08000000', target='h750', speed=400, expected_hash=sha256_file(image))
    if ok:
        assert '校验成功' in worker.flash_mcu(args)['message']
    else:
        with pytest.raises(RuntimeError, match='不能确认烧录成功'):
            worker.flash_mcu(args)
    assert len(calls) == 2
    args.expected_hash = '0' * 64
    with pytest.raises(ValueError, match='SHA256'):
        worker.flash_mcu(args)
    assert len(calls) == 2  # Hash mismatch prevents even the halt operation.


def test_stlink_absent_and_multiple_are_rejected(monkeypatch):
    monkeypatch.setattr(worker, 'detect_stlinks', lambda: [])
    with pytest.raises(ValueError, match='请连接'):
        worker.selected_stlink('')
    monkeypatch.setattr(worker, 'detect_stlinks', lambda: [{'serial': 'A'}, {'serial': 'B'}])
    with pytest.raises(ValueError, match='多个'):
        worker.selected_stlink('')
    assert worker.selected_stlink('B') == 'B'


def test_real_ros_bringup_creation_and_colcon_in_temporary_workspace(bringup_workspace):
    if not Path('/opt/ros/humble/setup.bash').is_file() or not shutil.which('colcon'):
        pytest.skip('ROS build environment not installed')
    args = bringup_args(bringup_workspace)
    result = worker.create_bringup(args)
    built = worker.build_workspace(args)
    assert '成功' in built['message']
    installed = bringup_workspace / 'install/soem_bringup/share/soem_bringup'
    assert (installed / 'config/config.yaml').read_text() == FIRST_RUN_YAML
    assert (installed / 'launch/bringup.launch.py').read_text() == Path(result['launch']).read_text()
    # Reproduce the actual user workflow: replace the SN-discovery template,
    # rebuild as the user, and verify that the installed YAML is now the new one.
    from ecat_groundstation.config import Project, Slave, Task
    from ecat_groundstation.workspace_config import write_workspace_config
    slave = Slave('9876543')
    slave.tasks = [Task.create(3, slave.sn, 1)]
    p = Project([slave], workspace=str(bringup_workspace))
    applied = write_workspace_config(p)
    args.expected_config_hash = applied['config_hash']
    rebuilt = worker.build_workspace(args)
    assert rebuilt['config_hash'] == applied['config_hash']
    assert (installed / 'config/config.yaml').read_text() == p.export_yaml()
    assert 'sn9876543' in (installed / 'config/config.yaml').read_text()


def test_subprocess_timeout_does_not_report_success():
    with pytest.raises(TimeoutError):
        worker.run(['/usr/bin/python3', '-c', 'import time;time.sleep(10)'], timeout=.1)


@pytest.mark.parametrize('fail_update', [False, True])
def test_grub_backup_write_and_failed_update_rollback_in_sandbox(tmp_path, monkeypatch, fail_update):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    monkeypatch.setattr(worker, 'detect_cpus', lambda: [Cpu(0, (0, 1)), Cpu(1, (0, 1)), Cpu(2, (2, 3)), Cpu(3, (2, 3))])
    monkeypatch.setattr(worker, 'read_text', lambda path: '1' if path == '/sys/kernel/realtime' else '')
    monkeypatch.setattr(worker, 'BACKUP_ROOT', tmp_path / 'backups')
    monkeypatch.setattr(worker, 'GRUB_DEFAULT', tmp_path / 'grub')
    monkeypatch.setattr(worker, 'GRUB_DROPIN', tmp_path / 'grub.d/99-test.cfg')
    monkeypatch.setattr(worker, 'GRUB_CONFIG', tmp_path / 'boot/grub.cfg')
    monkeypatch.setattr(worker.shutil, 'which', lambda name: '/test/' + name)
    worker.GRUB_DEFAULT.write_text('GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n')
    worker.GRUB_DROPIN.parent.mkdir()
    worker.GRUB_DROPIN.write_text('# original managed file\n')
    calls = []
    def simulated_update(command, **kwargs):
        calls.append(command)
        if fail_update and len(calls) == 1:
            raise RuntimeError('simulated grub update failure')
        return ''
    monkeypatch.setattr(worker, 'run', simulated_update)
    if fail_update:
        with pytest.raises(RuntimeError, match='simulated'):
            worker.isolate_cpu(Namespace())
        assert worker.GRUB_DROPIN.read_text() == '# original managed file\n'
        assert calls == [['update-grub'], ['update-grub']]
    else:
        result = worker.isolate_cpu(Namespace())
        assert result['reboot_required']
        assert 'nohz_full=2-3' in worker.GRUB_DROPIN.read_text()
        assert calls[1][0] == 'grub-script-check'
    assert any((tmp_path / 'backups').glob('*/grub'))


@pytest.mark.parametrize('attached,enabled,expected_run', [(True, False, True), (True, True, False), (False, False, False)])
def test_realtime_kernel_pro_gates_without_system_changes(monkeypatch, attached, enabled, expected_run):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'read_text', lambda path: '0')
    calls = []
    states = [enabled]
    def status(command, **kwargs):
        return Namespace(stdout=json.dumps({'attached': attached, 'services': [{'name': 'realtime-kernel', 'status': 'enabled' if states[0] else 'disabled'}]}))
    def enable(command, **kwargs):
        calls.append(command)
        states[0] = True
    monkeypatch.setattr(worker.subprocess, 'run', status)
    monkeypatch.setattr(worker, 'run', enable)
    if not attached:
        with pytest.raises(RuntimeError, match='pro attach'):
            worker.enable_rt(Namespace())
    else:
        assert worker.enable_rt(Namespace())['reboot_required']
    assert bool(calls) == expected_run
    if calls:
        assert calls == [['pro', 'enable', 'realtime-kernel', '--assume-yes']]


def test_readonly_eeprom_diagnostic_never_calls_write(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    monkeypatch.setattr(worker, 'validate_interface', lambda name: name)
    monkeypatch.setattr(worker, 'verified_eeprom_tool', lambda ws: Path('/test/verified-tool'))
    monkeypatch.setattr(worker, 'BACKUP_ROOT', tmp_path / 'backups')
    expected = bytearray(b'\xff' * 2048)
    image = tmp_path / 'large.bin'
    image.write_bytes(expected)
    observed = bytearray(expected)
    for offset in (160, 161, 204, 218, 219, 233, 247):
        observed[offset] ^= 1
    calls = []
    def read(args, **kwargs):
        calls.append(args)
        assert args[3] == '-r'
        Path(args[4]).write_bytes(observed)
        return 'EEPROM_READ_VERIFIED'
    monkeypatch.setattr(worker, 'run', read)
    result = worker.diagnose_eeprom(Namespace(workspace=str(tmp_path), interface='enp3s0', slave=1, image=str(image)))
    assert result['different_bytes'] == 7 and result['different_words'] == 5
    assert not result['matches_image']
    assert len(calls) == 1 and Path(result['report']).exists()


def test_eeprom_write_failure_records_remaining_bytes_without_blind_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    monkeypatch.setattr(worker, 'validate_interface', lambda name: name)
    monkeypatch.setattr(worker, 'BACKUP_ROOT', tmp_path / 'backups')
    tool = tmp_path / 'tool'
    tool.write_text('simulated, never executed')
    tool.chmod(0o755)
    monkeypatch.setattr(worker, 'verified_eeprom_tool', lambda ws: tool)
    image = tmp_path / 'image.bin'
    image.write_bytes(b'\xff' * 2048)
    observed = bytearray(image.read_bytes())
    observed[160] = 0
    calls = []
    def simulated(args, **kwargs):
        calls.append(args[3])
        if args[3] == '-r':
            Path(args[4]).write_bytes(observed)
            return 'EEPROM_READ_VERIFIED'
        raise RuntimeError('native exhausted per-word retries')
    monkeypatch.setattr(worker, 'run', simulated)
    with pytest.raises(RuntimeError, match='剩余 1 字节'):
        worker.flash_eeprom(Namespace(workspace=str(tmp_path), interface='enp3s0', slave=1, image=str(image)))
    assert calls == ['-r', '-w', '-r']  # The failure is diagnosed, not blindly retried.
    report = next((tmp_path / 'backups').glob('eeprom/*/failure.json'))
    assert json.loads(report.read_text())['first_differences'][0]['offset'] == 160
