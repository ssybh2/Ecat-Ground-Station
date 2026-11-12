"""H750 builds use temporary source trees; no probe or flash operation is run."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from argparse import Namespace
from pathlib import Path
import json
import shutil
import subprocess

import pytest
from PyQt5.QtWidgets import QApplication, QPushButton, QFileDialog, QMessageBox
from PyQt5.QtTest import QTest

from ecat_groundstation import setup_worker as worker
from ecat_groundstation.first_setup import FirstSetupPage, FirmwarePane, SetupJobs
from ecat_groundstation.setup_core import flash_ranges, sha256_file


@pytest.fixture
def firmware_source(tmp_path):
    repo = tmp_path / 'H750 local source'
    (repo / 'cmake').mkdir(parents=True)
    (repo / 'main.c').write_text('void Reset_Handler(void) { for (;;) {} }\n')
    (repo / 'STM32H750XX_FLASH.ld').write_text('''ENTRY(Reset_Handler)
MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 128K }
SECTIONS { .text : { *(.text*) *(.rodata*) } > FLASH }
''')
    (repo / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.22)
project(custom_h750 C)
add_executable(custom_h750 main.c)
set_target_properties(custom_h750 PROPERTIES SUFFIX ".elf")
target_link_options(custom_h750 PRIVATE -nostdlib "-T${CMAKE_SOURCE_DIR}/STM32H750XX_FLASH.ld")
''')
    (repo / 'cmake/gcc-arm-none-eabi.cmake').write_text('''set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_C_COMPILER arm-none-eabi-gcc)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
set(CMAKE_C_FLAGS "-mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard")
''')
    return repo


def args_for(repo, tmp_path):
    return Namespace(repo=str(repo), output_root=str(tmp_path / 'separate builds'), build_type='Release',
                     build_target='custom_h750', toolchain_bin='', toolchain_file='')


def test_real_arm_cmake_build_produces_elf_bin_hex_and_keeps_source(firmware_source, tmp_path):
    if not all(shutil.which(tool) for tool in ('arm-none-eabi-gcc', 'arm-none-eabi-objcopy', 'cmake', 'make')):
        pytest.skip('ARM GCC / CMake not installed')
    before = {str(p.relative_to(firmware_source)): p.read_bytes() for p in firmware_source.rglob('*') if p.is_file()}
    result = worker.compile_h750(args_for(firmware_source, tmp_path))
    assert flash_ranges(result['image'])[0][0] == 0x08000000
    assert Path(result['bin']).stat().st_size > 0 and ':00000001FF' in Path(result['hex']).read_text()
    assert sha256_file(result['image']) == result['sha256']
    assert result['repo'] == str(firmware_source) and result['build_target'] == 'custom_h750'
    assert before == {str(p.relative_to(firmware_source)): p.read_bytes() for p in firmware_source.rglob('*') if p.is_file()}
    manifest = Path(result['source']).parent / 'manifest.json'
    assert json.loads(manifest.read_text())['sha256'] == result['sha256']


def test_output_in_source_and_non_h750_source_are_rejected(firmware_source, tmp_path):
    args = args_for(firmware_source, tmp_path)
    args.output_root = str(firmware_source / 'generated')
    with pytest.raises(ValueError, match='源码目录之外'):
        worker.compile_h750(args)
    (firmware_source / 'STM32H750XX_FLASH.ld').unlink()
    with pytest.raises(ValueError, match='STM32H750'):
        worker.h750_source(firmware_source)


def test_failed_rebuild_cannot_select_older_elf(firmware_source, tmp_path, monkeypatch):
    args = args_for(firmware_source, tmp_path)
    old = Path(args.output_root) / 'older/build/Release/custom_h750.elf'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old firmware')
    monkeypatch.setattr(worker, 'suitable_cmake', lambda *args: '/usr/bin/cmake')
    monkeypatch.setattr(worker.shutil, 'which', lambda name, **kwargs: '/tools/' + name)
    def failure(*args, **kwargs):
        raise RuntimeError('compiler failed')
    monkeypatch.setattr(worker, 'run', failure)
    with pytest.raises(RuntimeError, match='compiler failed'):
        worker.compile_h750(args)
    assert not list(Path(args.output_root).glob('h750-*/manifest.json'))
    assert old.read_bytes() == b'old firmware'


def test_missing_cpp_libraries_stop_before_build_or_download(firmware_source, tmp_path, monkeypatch):
    (firmware_source / 'task.cpp').write_text('#include <memory>\n')
    monkeypatch.setattr(worker.shutil, 'which', lambda name, **kwargs: '/tools/' + name)
    monkeypatch.setattr(worker.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout='', stderr='missing C++ header'))
    calls = []
    monkeypatch.setattr(worker, 'run', lambda *args, **kwargs: calls.append(args))
    args = args_for(firmware_source, tmp_path)
    with pytest.raises(ValueError, match='关闭异常和 RTTI'):
        worker.compile_h750(args)
    assert not calls and not Path(args.output_root).exists()


def test_existing_local_source_check_does_not_clone_or_modify(firmware_source, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, 'run', lambda *args, **kwargs: calls.append(args))
    assert worker.prepare_h750(Namespace(repo=str(firmware_source)))['repo'] == str(firmware_source)
    assert not calls


def test_h750_jobs_are_ordinary_user_and_cpp_dependencies_are_separate():
    assert SetupJobs.command('compile-h750', [])[0] == '/usr/bin/python3'
    assert SetupJobs.command('prepare-h750', [])[0] == '/usr/bin/python3'
    assert worker.TOOL_PACKAGES['h750'] == worker.TOOL_PACKAGES['imu']
    assert all('libstdc++-arm-none-eabi-newlib' not in packages for packages in worker.TOOL_PACKAGES.values())


def test_ui_build_offers_choice_without_replacing_selected_firmware(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    assert page.h750_modes.currentIndex() == 0
    manual = str(tmp_path / 'my existing firmware.bin')
    page.h750.image.setText(manual)
    page.h750_modes.setCurrentIndex(1)
    page.h750_repo.setText(str(tmp_path / 'local firmware'))
    calls = []
    page.start_job = lambda action, args=(): calls.append((action, args)) or True
    page.compile_h750()
    assert calls[-1][0] == 'compile-h750'
    assert '--repo' in calls[-1][1] and '--build-target' in calls[-1][1]
    image = tmp_path / 'build/firmware.elf'
    image.parent.mkdir()
    image.write_bytes(b'recorded firmware')
    record = {**page.h750_settings(), 'image': str(image), 'sha256': sha256_file(image), 'message': 'success'}
    page.job_finished('compile-h750', True, record)
    assert page.h750.image.text() == manual and page.h750_modes.currentIndex() == 1
    assert page.h750_use_artifact.isEnabled()
    # Cancelling the artifact chooser must also preserve the selected file.
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: ('', ''))
    page.choose_h750_artifact()
    assert page.h750.image.text() == manual
    # A saved build record never turns the default manual page into a build prerequisite.
    restored = FirstSetupPage(state_path=tmp_path / 'state.json')
    assert restored.h750_modes.currentIndex() == 0 and not restored.h750.image.text()
    restored.timer.stop()
    restored.close()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(image), ''))
    page.choose_h750_artifact()
    assert page.h750.image.text() == str(image) and page.h750.expected_hash == record['sha256']
    assert page.h750_modes.currentIndex() == 0
    dialog = page.open_firmware('h750')
    pane = dialog.findChild(FirmwarePane)
    assert pane.image.text() == str(image) and pane.expected_hash == record['sha256']
    assert any(b.text() == '选择固件…' for b in page.h750.findChildren(QPushButton))
    dialog.close()
    app.processEvents()
    page.h750_build_type.setCurrentText('Debug')
    assert not page.current_h750_artifact() and page.h750.image.text() == str(image)
    page.h750.image.setText(manual)
    page.compile_h750()
    page.job_finished('compile-h750', False, {'message': 'build failed'})
    assert page.h750.image.text() == manual
    assert '编译未成功' in page.h750_artifact_label.text()
    assert not page.state.get('h750_artifact')
    page.timer.stop()
    page.close()


def test_direct_local_firmware_flash_requires_no_source_or_compiler(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    page.h750_repo.setText('/no/source/workspace')
    page.h750_target.setText('invalid compiler settings!')
    page.resize(1200, 850)
    page.show()
    page.tabs.setCurrentIndex(2)
    QTest.qWait(20)
    assert page.h750_modes.currentIndex() == 0
    assert page.h750.image.isVisible() and not page.h750_repo.isVisible()
    image = tmp_path / 'existing H750.bin'
    image.write_bytes(b'\x00' * 32)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *args: (str(image), ''))
    page.h750.browse()
    monkeypatch.setattr('ecat_groundstation.first_setup.shutil.which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr('ecat_groundstation.first_setup.detect_stlinks', lambda: [{'serial': 'TEST123', 'product': 'ST-LINK'}])
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    calls = []
    page.start_job = lambda action, args=(): calls.append((action, args)) or True
    page.h750.execute(True)
    assert len(calls) == 1 and calls[0][0] == 'flash-mcu'
    assert str(image) in calls[0][1] and 'h750' in calls[0][1]
    page.timer.stop()
    page.close()
