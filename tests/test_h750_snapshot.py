from pathlib import Path
import shutil
import subprocess

import pytest

from ecat_groundstation import h750_snapshot as snapshot
from ecat_groundstation.setup_core import sha256_file


HEADER = '''using uint8_t = unsigned char;
struct FDCAN_RxHeaderTypeDef {};
class Base { public: virtual ~Base() = default; };
class CanRunnable : public Base { public:
virtual void can_recv(FDCAN_RxHeaderTypeDef *rx_header, uint8_t *rx_data);
};
class I2CRunnable : public Base { public:
virtual void i2c_recv();
};
'''


def source_tree(tmp_path, monkeypatch):
    (tmp_path / 'Application').mkdir()
    (tmp_path / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.22)\nproject(test CXX)\n')
    header = tmp_path / 'Application/task_defs.hpp'
    header.write_text(HEADER)
    monkeypatch.setattr(snapshot, 'KNOWN_TASK_HEADER', sha256_file(header))
    return header


def test_callback_fix_links_at_o0_and_is_idempotent(tmp_path, monkeypatch):
    if not shutil.which('g++'):
        pytest.skip('C++ compiler unavailable')
    header = source_tree(tmp_path, monkeypatch)
    main = tmp_path / 'main.cpp'
    main.write_text('''#include "Application/task_defs.hpp"
class Can final : public CanRunnable { public: void can_recv(FDCAN_RxHeaderTypeDef*,uint8_t*) override {} };
class I2C final : public I2CRunnable { public: void i2c_recv() override {} };
int main(){ Base* a=new Can; Base* b=new I2C; delete a; delete b; }
''')
    command = ['g++', '-O0', str(main), '-o', str(tmp_path / 'check')]
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0 and 'vtable' in failed.stderr
    changes = snapshot.prepare_h750_snapshot(tmp_path, 'Debug')
    assert len(changes) == 2 and header.read_text().count(' = 0;') == 2
    subprocess.run(command, capture_output=True, text=True, check=True)
    subprocess.run([str(tmp_path / 'check')], check=True)
    assert snapshot.prepare_h750_snapshot(tmp_path, 'Debug') == []


@pytest.mark.parametrize('custom', ['modified-header', 'provided-base-definition'])
def test_custom_firmware_is_not_rewritten(tmp_path, monkeypatch, custom):
    header = source_tree(tmp_path, monkeypatch)
    if custom == 'modified-header':
        header.write_text(HEADER + '// user customization\n')
    else:
        (tmp_path / 'custom.cpp').write_text('void CanRunnable::can_recv(FDCAN_RxHeaderTypeDef*,uint8_t*) {}\n')
    before = header.read_bytes()
    assert snapshot.prepare_h750_snapshot(tmp_path, 'Release') == []
    assert header.read_bytes() == before
