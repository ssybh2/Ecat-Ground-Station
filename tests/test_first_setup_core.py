from pathlib import Path
import ast
import json
import struct
import subprocess

import pytest

from ecat_groundstation.setup_core import (
    Cpu, choose_isolation, compact_cpus, cpu_list, verify_isolation, grub_dropin,
    detect_cpus, parse_pro_status, network_snapshot, changed_interfaces, detect_stlinks,
    workspace_path, launch_file, install_cmake_block, validate_imu_ids, patch_imu_source,
    flash_ranges, openocd_commands, tcl_word, flash_verified, parse_slave_sns,
    atomic_json, load_state,
)


def cpus(groups, kinds=None, frequencies=None):
    result = []
    for n, group in enumerate(groups):
        for i in group:
            result.append(Cpu(i, tuple(group), n, 0, (kinds or {}).get(n, 'unknown'), (frequencies or {}).get(n, 0)))
    return result


def make_elf(path, address=0x08000000, machine=40, size=32):
    data = bytearray(84 + size)
    data[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HHI', data, 16, 2, machine, 1)
    struct.pack_into('<I', data, 28, 52)
    struct.pack_into('<HHH', data, 40, 52, 32, 1)
    struct.pack_into('<IIIIIIII', data, 52, 1, 84, address, address, size, size, 5, 4)
    path.write_bytes(data)
    return path


def test_topology_non_contiguous_smt_and_existing_isolation():
    topology = cpus([(0, 4), (1, 5), (2, 6), (3, 7)])
    plan = choose_isolation(topology)
    assert plan.isolated == (3, 7)
    assert plan.housekeeping == (0, 1, 2, 4, 5, 6)
    assert plan.rt_cpu == 3
    existing = choose_isolation(topology, 'isolcpus=domain,managed_irq,2,6', '2,6')
    assert existing.isolated == (2, 6) and existing.existing
    assert not choose_isolation(topology, 'isolcpus=6', '6').existing


@pytest.mark.parametrize('frequency,expected', [(2_100_000, (4,)), (2_000_000, (2, 3)), (1_800_000, (2, 3)), (0, (2, 3))])
def test_hybrid_selection_uses_base_frequency(frequency, expected):
    topology = cpus([(0, 1), (2, 3), (4,)], {0: 'performance', 1: 'performance', 2: 'efficiency'}, {1: 3_000_000, 2: frequency})
    assert choose_isolation(topology).isolated == expected


def test_single_physical_core_refused():
    with pytest.raises(ValueError, match='housekeeping'):
        choose_isolation(cpus([(0, 1)]))


def test_active_isolation_requires_actual_boot_parameters():
    plan = choose_isolation(cpus([(0, 1), (2, 3)]))
    checks = verify_isolation(plan, plan.parameters, '2-3', '2-3')
    assert all(checks.values())
    checks = verify_isolation(plan, plan.parameters.replace('irqaffinity=0-1', 'irqaffinity=0-3'), '2-3', '')
    assert not checks['nohz_full'] and not checks['irqaffinity']
    assert cpu_list('(null)') == []


def test_grub_dropin_preserves_other_flags_and_removes_conflicting_values(tmp_path):
    plan = choose_isolation(cpus([(0, 1), (2, 3)]))
    script = tmp_path / 'dropin.cfg'
    script.write_text(grub_dropin(plan))
    env = {'GRUB_CMDLINE_LINUX': 'rootflags=subvol=@ rcu_nocbs=0',
           'GRUB_CMDLINE_LINUX_DEFAULT': 'quiet splash isolcpus=0 nohz_full=0 irqaffinity=1-3'}
    output = subprocess.check_output(['/bin/sh', '-c', '. "$1"; printf "%s\\n%s\\n" "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"',
                                      'test', str(script)], env=env, text=True)
    assert 'rootflags=subvol=@' in output and 'quiet splash' in output
    assert 'isolcpus=0 ' not in output and 'rcu_nocbs=0' not in output
    assert plan.parameters in output


def test_sysfs_cpu_probe(tmp_path):
    root = tmp_path / 'devices/system/cpu'
    root.mkdir(parents=True)
    (root / 'online').write_text('0-2')
    for i, siblings, kind in [(0, '0-1', '2'), (1, '0-1', '2'), (2, '2', '1')]:
        directory = root / f'cpu{i}/topology'
        directory.mkdir(parents=True)
        (directory / 'thread_siblings_list').write_text(siblings)
        (directory / 'core_type').write_text(kind)
        (directory / 'core_id').write_text(str(i // 2))
    result = detect_cpus(tmp_path)
    assert result[0].siblings == (0, 1)
    assert result[2].kind == 'efficiency'
    assert result[2].base_khz == 0


def test_pro_probe_returns_no_account_information():
    raw = {'attached': True, 'account': {'name': 'private@example.invalid', 'token': 'secret'},
           'services': [{'name': 'realtime-kernel', 'status': 'enabled', 'available': 'yes', 'entitled': 'yes'}]}
    parsed = parse_pro_status(raw)
    assert parsed['attached'] and parsed['rt_enabled']
    assert 'private' not in json.dumps(parsed) and 'secret' not in json.dumps(parsed)


def test_network_carrier_change_even_if_admin_state_is_up():
    before = {'enp3s0': {'carrier': '1', 'operstate': 'up'}, 'enp4s0': {'carrier': '0', 'operstate': 'down'}}
    after = {'enp3s0': {'carrier': '0', 'operstate': 'up'}, 'enp4s0': {'carrier': '0', 'operstate': 'down'}}
    assert changed_interfaces(before, after) == ['enp3s0']
    assert not changed_interfaces(after, after)


def test_stlink_usb_probe_ignores_dfu_device(tmp_path):
    for name, product, label in [('1-1', '3752', 'ST-LINK/V2.1'), ('1-2', 'df11', 'STM32 BOOTLOADER')]:
        d = tmp_path / name
        d.mkdir()
        (d / 'idVendor').write_text('0483')
        (d / 'idProduct').write_text(product)
        (d / 'product').write_text(label)
        (d / 'serial').write_text('ABCDEF')
    result = detect_stlinks(tmp_path)
    assert len(result) == 1 and result[0]['serial'] == 'ABCDEF'


@pytest.mark.parametrize('name', ['../escape', '/tmp/escape', '.ssh', 'with space', '$(touch bad)', 'a/b', ''])
def test_workspace_name_cannot_escape_home(name, tmp_path):
    with pytest.raises(ValueError):
        workspace_path(name, tmp_path)


def test_workspace_path_and_symlink(tmp_path):
    assert workspace_path('EGC_ws', tmp_path) == tmp_path / 'EGC_ws'
    (tmp_path / 'symlink').symlink_to('/tmp')
    with pytest.raises(ValueError):
        workspace_path('symlink', tmp_path)


def test_launch_generation_is_python_and_cmake_is_idempotent():
    code = launch_file('enp3s0', 6, '0-5')
    ast.parse(code)
    assert "'interface': 'enp3s0'" in code
    assert "'rt_cpu': 6" in code
    assert "'non_rt_cpus': '0-5'" in code
    original = 'cmake_minimum_required(VERSION 3.8)\nproject(soem_bringup)\nfind_package(ament_cmake REQUIRED)\nament_package()\n'
    updated = install_cmake_block(original)
    assert updated.index('install(DIRECTORY') < updated.index('ament_package()')
    assert install_cmake_block(updated) == updated
    with pytest.raises(ValueError):
        launch_file('enp3s0', 6, '0-6')


def test_three_imu_profiles_no_collision_and_patch_only_defines():
    validate_imu_ids([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    with pytest.raises(ValueError, match='不能重复'):
        validate_imu_ids([[1, 2, 3], [3, 5, 6]])
    original = '#define PACKET1_CAN_ID 0x01\n#define PACKET2_CAN_ID 0x02 // acceleration\n#define PACKET3_CAN_ID 0x03\nint preserve = 12;\n'
    modified = patch_imu_source(original, [4, 5, 6])
    assert '0x004' in modified and '0x005 // acceleration' in modified and 'int preserve = 12;' in modified
    assert '0x01' in original
    with pytest.raises(ValueError):
        patch_imu_source(original + '#define PACKET1_CAN_ID 0x01\n', [1, 2, 3])


def test_elf_architecture_and_flash_ranges(tmp_path):
    image = make_elf(tmp_path / 'firmware.elf')
    assert flash_ranges(image) == [(0x08000000, 32)]
    with pytest.raises(ValueError, match='ARM ELF32'):
        flash_ranges(make_elf(tmp_path / 'x86.elf', machine=3))
    with pytest.raises(ValueError, match='128 KiB'):
        flash_ranges(make_elf(tmp_path / 'overflow.elf', address=0x0801FFF0))
    data = image.read_bytes()
    image.write_bytes(data[:85])
    with pytest.raises(ValueError, match='越界'):
        flash_ranges(image)


def test_bin_addresses_and_openocd_path_quoting(tmp_path):
    image = tmp_path / 'firmware with spaces [x];$test.bin'
    image.write_bytes(b'\x01' * 32)
    commands = openocd_commands('h750', image, serial='ABC123')
    joined = '\n'.join(commands)
    assert '-f' in commands and 'target/stm32h7x.cfg' in commands
    assert 'reset halt' not in commands
    assert commands.index('halt') < commands.index('flash probe 0')
    assert f'flash write_image erase {{{image}}} 0x08000000' in commands
    assert f'verify_image {{{image}}} 0x08000000' in commands
    assert 'hla_serial {ABC123}' in commands
    assert 'reset run' not in commands
    assert 'MCU device ID mismatch' in joined
    with pytest.raises(ValueError):
        tcl_word('bad}; shutdown; {')
    imu = openocd_commands('g431', image)
    assert 'reset halt' in imu and 'reset run' in imu


def test_flash_success_requires_both_write_and_verify():
    assert flash_verified('wrote 1024 bytes from file\nverified 1024 bytes\nshutdown')
    assert not flash_verified('shutdown command invoked')
    assert not flash_verified('wrote 1024 bytes\nError: verify failed')


def test_state_roundtrip_and_sn_extraction(tmp_path):
    path = tmp_path / 'state.json'
    atomic_json(path, {'version': 1, 'workspace': '/home/test/EGC_ws'})
    assert load_state(path)['workspace'].endswith('EGC_ws')
    assert path.stat().st_mode & 0o777 == 0o600
    log = 'Found slave id=1, sn=2228293, eepid=3\nFound slave idx=2, sn=1234567, type=H750\nFound slave id=1, sn=2228293,'
    assert parse_slave_sns(log) == ['2228293', '1234567']
