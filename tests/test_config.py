import copy
from pathlib import Path

import pytest
import yaml

from ecat_groundstation.config import Project, Slave, Task, ConfigLoader, save_project, load_project
from ecat_groundstation.schema import SPECS, pdo_size, active_fields
from ecat_groundstation.commands import fields_for, neutral_values, validate_command, feedback_problem


def single(tid):
    s = Slave('1234567')
    s.tasks = [Task.create(tid, s.sn, 1)]
    return Project([s])


@pytest.mark.parametrize('tid', sorted(SPECS))
def test_all_task_types_roundtrip(tid):
    p = single(tid)
    assert p.validate() == []
    text = p.export_yaml()
    imported, warnings = Project.import_yaml(text)
    assert not warnings
    assert imported.export_yaml() == text
    assert imported.slaves[0].allocations() == p.slaves[0].allocations()
    t = p.slaves[0].tasks[0]
    if fields_for(t):
        validate_command(t, neutral_values(t))


@pytest.mark.parametrize('mode,write', [(1, 9), (2, 9), (3, 5)])
def test_dm_layout(mode, write):
    p = single(12)
    p.slaves[0].tasks[0].values['sdowrite_control_type'] = mode
    assert p.slaves[0].sdo_length() == 11
    assert pdo_size(p.slaves[0].tasks[0]) == (9, write)


@pytest.mark.parametrize('mode,size', [(1, 3), (2, 3), (3, 7), (4, 5), (5, 7), (6, 6), (7, 8), (8, 8)])
def test_lk_modes(mode, size):
    p = single(2)
    t = p.slaves[0].tasks[0]
    t.values['sdowrite_control_type'] = mode
    assert pdo_size(t) == (32 if mode == 8 else 8, size)
    assert p.slaves[0].sdo_length() == (7 if mode == 8 else 11)
    result, warnings = Project.import_yaml(p.export_yaml())
    assert not warnings
    assert result.slaves[0].sdo_length() == p.slaves[0].sdo_length()
    validate_command(t, neutral_values(t))


def test_dji_dynamic_pid_sdo_and_pdo():
    p = single(5)
    t = p.slaves[0].tasks[0]
    assert p.slaves[0].sdo_length() == 27
    t.values.update(sdowrite_motor1_control_type=3, sdowrite_motor2_can_id=0x202,
                    sdowrite_motor2_control_type=2)
    assert p.slaves[0].sdo_length() == 88
    assert pdo_size(t) == (18, 6)
    out, w = Project.import_yaml(p.export_yaml())
    assert not w
    assert out.slaves[0].sdo_length() == 88


def test_resources_and_topics_block_export():
    p = single(6)
    p.slaves[0].tasks.append(Task.create(4, '1234567', 2))
    assert any('端口 2' in e for e in p.validate())
    with pytest.raises(ValueError, match='资源冲突'):
        p.export_yaml()
    p.slaves[0].tasks[1].values['sdowrite_dshot_id'] = 1
    assert p.validate() == []
    p.slaves[0].tasks[1].values['sub_topic'] = p.slaves[0].tasks[0].values['sub_topic']
    assert any('话题' in e for e in p.validate())


def test_can_id_collision_in_same_task():
    p = single(12)
    p.slaves[0].tasks[0].values['sdowrite_master_id'] = 1
    assert any('资源冲突' in e for e in p.validate())


def test_capacity_standard_vs_large():
    p = single(3)
    s = p.slaves[0]
    for i in range(2, 5):
        t = Task.create(3, s.sn, i)
        for k in range(1, 4):
            t.values[f'sdowrite_packet{k}_id'] += 3 * (i - 1)
        s.tasks.append(t)
    assert s.allocations()[1] == 84
    assert any('PDO 超容量' in e for e in p.validate())
    s.model = 'large'
    assert p.validate() == []
    imported, warnings = Project.import_yaml(p.export_yaml())
    assert imported.slaves[0].model == 'large'
    assert warnings


def test_tag_mismatch_rejected():
    text = single(12).export_yaml()
    text = text.replace("sdowrite_can_id: !uint16_t", "sdowrite_can_id: !uint32_t")
    with pytest.raises(ValueError, match='需要 !uint16_t'):
        Project.import_yaml(text)


@pytest.mark.parametrize('text', [
    'slaves: []\nslaves: []',
    'slaves: !!python/object/apply:os.system ["echo should-not-run"]',
    'slaves: [{sn1: {sdo_len: 0}}]',
    'slaves: [{SBT908E: {}}]',
])
def test_bad_documents_rejected(text):
    with pytest.raises((ValueError, yaml.YAMLError)):
        Project.import_yaml(text)


def test_repair_legacy_lengths_and_offsets():
    text = Project.demo().export_yaml()
    text = text.replace("sdo_len: !uint16_t '32'", "sdo_len: !uint16_t '1'")
    text = text.replace("pdowrite_offset: !uint16_t '9'", "pdowrite_offset: !uint16_t '77'")
    imported, warnings = Project.import_yaml(text)
    assert len(warnings) == 2
    assert imported.export_yaml() == Project.demo().export_yaml()


def test_project_preserves_model_settings_and_unused_pid(tmp_path):
    p = single(5)
    p.slaves[0].model = 'large'
    p.slaves[0].tasks[0].values['sdowrite_motor1_angle_pid_kp'] = 3.2
    p.interface = 'eth0'
    save_project(p, tmp_path / 'project.json')
    result = load_project(tmp_path / 'project.json')
    assert result.to_dict() == p.to_dict()


def test_nan_overflow_and_unknown_sdo_rejected():
    p = single(12)
    t = p.slaves[0].tasks[0]
    t.values['conf_pmax'] = float('nan')
    assert p.validate()
    t.values['conf_pmax'] = 3.14
    t.extras['sdowrite_not_supported'] = {'tag': 'uint8_t', 'value': 0}
    assert any('未识别' in e for e in p.validate())


def test_command_limits_and_neutral():
    p = single(12)
    t = p.slaves[0].tasks[0]
    values = neutral_values(t)
    assert values['enable'] == 0
    values['p_des'] = 999.0
    with pytest.raises(ValueError):
        validate_command(t, values)
    pwm = single(6).slaves[0].tasks[0]
    assert set(neutral_values(pwm).values()) == {1500}
    shot = single(4).slaves[0].tasks[0]
    with pytest.raises(ValueError, match='特殊命令'):
        validate_command(shot, dict(channel1=10, channel2=0, channel3=0, channel4=0))
    assert feedback_problem(t, {'online': 0})
    assert feedback_problem(t, {'online': 1, 'overcurrent': 1})


def test_existing_workspace_config_if_present():
    path = Path('/home/hby/Rolling_ZLT/src/soem_bringup/config/config.yaml')
    if not path.is_file():
        pytest.skip('Local hardware project not installed')
    p, warnings = Project.import_yaml(path.read_text())
    assert p.validate() == []
    assert [s.sdo_length() for s in p.slaves] == [51, 50]
    assert [s.allocations()[1] for s in p.slaves] == [82, 63]
