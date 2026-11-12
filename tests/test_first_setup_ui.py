import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtTest import QTest

from ecat_groundstation.app import MainWindow
from ecat_groundstation.first_setup import FirstSetupPage, SetupJobs
from ecat_groundstation.setup_core import Cpu, choose_isolation


@pytest.fixture(scope='module')
def app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_privileged_jobs_use_system_prompt_and_no_password_argument():
    command = SetupJobs.command('flash-mcu', ['--target', 'h750', '--image', '/tmp/file name.elf'])
    assert command[0] == 'pkexec'
    assert '-I' in command and '/tmp/file name.elf' in command
    assert not any('password' in arg for arg in command)
    command = SetupJobs.command('build-workspace', ['--workspace', '/home/user/EGC_ws'])
    assert command[0] == '/usr/bin/python3'


@pytest.mark.parametrize('started', [True, (True, 12345), False, (False, 0)])
def test_root_terminal_handles_qt_return_and_reports_spawn_failure(app, tmp_path, monkeypatch, started):
    from PyQt5.QtCore import QProcess
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    monkeypatch.setattr('ecat_groundstation.first_setup.shutil.which', lambda name: '/usr/bin/' + name)
    calls, warnings = [], []
    monkeypatch.setattr(QProcess, 'startDetached', lambda *args: calls.append(args) or started)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args))
    page.open_terminal('root-shell', ['--workspace', str(tmp_path)])
    assert len(calls) == 1 and calls[0][1][1] == 'pkexec'
    expected = started[0] if isinstance(started, tuple) else started
    assert bool(warnings) == (not expected)
    page.timer.stop()
    page.close()


@pytest.mark.parametrize('relative', ['config/config.yaml', 'launch/bringup.launch.py'])
def test_root_terminal_blocks_stale_installed_config_or_launch(app, tmp_path, monkeypatch, relative):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    page.workspace_edit.setText(str(tmp_path))
    (tmp_path / 'install/setup.bash').parent.mkdir()
    (tmp_path / 'install/setup.bash').write_text('# setup\n')
    for name in ('config/config.yaml', 'launch/bringup.launch.py'):
        for base in ('src/soem_bringup', 'install/soem_bringup/share/soem_bringup'):
            target = tmp_path / base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# installed\n')
    source = tmp_path / 'src/soem_bringup' / relative
    source.write_text('# new SN or CPU settings\n')
    calls, messages = [], []
    page.open_terminal = lambda *args: calls.append(args)
    monkeypatch.setattr(QMessageBox, 'information', lambda parent, title, text: messages.append(text))
    page.open_root_terminal()
    assert not calls and 'colcon build' in messages[0] and relative in messages[0]
    installed = tmp_path / 'install/soem_bringup/share/soem_bringup' / relative
    installed.write_text(source.read_text())
    page.open_root_terminal()
    assert len(calls) == 1 and calls[0][0] == 'root-shell'
    page.timer.stop()
    page.close()


def test_one_click_kernel_inspection_followup_and_existing_cpu_no_write(app, tmp_path, monkeypatch):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    calls = []
    monkeypatch.setattr(page, 'start_job', lambda action, args=(): calls.append((action, args)))
    monkeypatch.setattr(QMessageBox, 'information', lambda *args, **kwargs: QMessageBox.Ok)
    page.enable_rt()
    assert calls[-1][0] == 'inspect' and page.after_inspection == 'enable-rt'
    plan = choose_isolation([Cpu(0, (0, 1)), Cpu(1, (0, 1)), Cpu(2, (2, 3)), Cpu(3, (2, 3))]).to_dict()
    page.inspection = {'realtime': True, 'isolation_checks': {'isolated': True, 'nohz_full': True, 'rcu': True}}
    page.current_plan = plan
    count = len(calls)
    page.isolate_cpu_checked()
    assert len(calls) == count  # Idempotent: no pkexec/update-grub on this case.
    page.timer.stop()
    page.close()


def test_compile_invalidates_old_record_and_id_conflicts_block_job(app, tmp_path, monkeypatch):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    page.state['imu_artifacts'] = {'1': {'ids': [1, 2, 3], 'image': '/tmp/old.elf', 'sha256': 'old'}}
    calls = []
    monkeypatch.setattr(page, 'start_job', lambda action, args=(): calls.append((action, args)) or True)
    page.compile_imu(1)
    assert calls[-1][0] == 'compile-imu'
    assert '1' not in page.state['imu_artifacts']
    page.id_editors[1][0].setText('0x001')
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args, **kwargs: QMessageBox.Ok)
    count = len(calls)
    page.compile_imu(2)
    assert len(calls) == count
    page.timer.stop()
    page.close()


def test_first_setup_to_existing_config_editor(app, tmp_path):
    window = MainWindow()
    page = window.first_setup
    page.state_path = tmp_path / 'state.json'
    assert window.nav.item(window.PAGE_FIRST_SETUP).text().endswith('首次配置')
    assert window.pages.widget(window.PAGE_FIRST_SETUP) is page
    page.current_plan = choose_isolation([Cpu(0, (0, 1)), Cpu(1, (0, 1)), Cpu(2, (2, 3)), Cpu(3, (2, 3))]).to_dict()
    page.real_sn.setText('9876543')
    page.interface.setCurrentText('enp3s0')
    page.workspace_edit.setText(str(tmp_path / 'EGC_ws'))
    page.adopt_setup()
    assert window.project.slaves[0].sn == '9876543'
    assert window.project.rt_cpu == 2
    assert window.project.non_rt_cpus == '0-1'
    assert window.project.slaves[0].sdo_length() == 15  # Real task ABI, not the 2-byte SN-discovery example.
    assert window.nav.currentRow() == window.PAGE_CONFIG
    assert not window.project.validate()
    window.dirty = False
    page.timer.stop()
    window.close()
    app.processEvents()


def test_eeprom_tool_preparation_does_not_write_and_cancelled_preparation_cannot_flash(app, tmp_path):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    image = tmp_path / 'image.bin'
    image.write_bytes(b'\xff' * 2048)
    page.eeprom_image.setText(str(image))
    page.interface.setCurrentText('enp3s0')
    calls = []
    page.start_job = lambda action, args=(): calls.append((action, args)) or True
    page.prepare_eeprom('diagnose-eeprom')
    assert calls[-1][0] == 'prepare-eeprom-tool'
    page.job_finished('prepare-eeprom-tool', True, {'message': 'prepared'})
    assert calls[-1][0] == 'diagnose-eeprom'
    page.prepare_eeprom('flash-eeprom')
    count = len(calls)
    page.job_finished('prepare-eeprom-tool', False, {'message': 'cancelled'})
    assert page.pending_eeprom is None and len(calls) == count
    page.timer.stop()
    page.close()


def test_setup_error_message_and_log_survive_window_closure(app, tmp_path):
    jobs = SetupJobs(log_dir=tmp_path / 'logs')
    jobs.action = 'flash-eeprom'
    jobs.busy = True
    jobs.log_dir.mkdir()
    jobs.log_path = jobs.log_dir / 'attempt.log'
    jobs.log_stream = jobs.log_path.open('w')
    jobs.decoder = __import__('codecs').getincrementaldecoder('utf-8')('replace')
    jobs.handle_line('EEPROM word retry: byte=0x00a0 attempt=8/8')
    jobs.handle_line('@@ECAT_ERROR@@' + json.dumps({'message': '剩余7字节，详见备份'}))
    completed = []
    jobs.done.connect(lambda action, ok, data: completed.append((action, ok, data)))
    from PyQt5.QtCore import QProcess
    jobs.finished(1, QProcess.NormalExit)
    assert completed and not completed[0][1]
    assert completed[0][2]['message'] == '剩余7字节，详见备份'
    assert 'byte=0x00a0' in jobs.log_path.read_text()
    assert completed[0][2]['log_path'] == str(jobs.log_path)


def test_eeprom_retry_preserves_large_pdo_and_selected_board(app, tmp_path):
    from ecat_groundstation.setup_core import atomic_json
    image = tmp_path / 'LargePDOV.bin'
    image.write_bytes(b'\xff' * 2048)
    state = tmp_path / 'state.json'
    atomic_json(state, {'version': 1, 'workspace': str(tmp_path), 'model': 'large',
                        'interface': 'enp5s0f0', 'eeprom_slave': 2, 'eeprom_image': str(image)})
    page = FirstSetupPage(state_path=state)
    assert page.eeprom_kind.currentIndex() == 1
    assert page.slave_index.value() == 2
    assert page.interface.currentText() == 'enp5s0f0'
    assert page.eeprom_image.text() == str(image)
    page.timer.stop()
    page.close()
