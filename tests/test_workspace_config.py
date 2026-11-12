from argparse import Namespace
from pathlib import Path
import json
import shutil

import pytest

from ecat_groundstation.config import Project, Slave, Task, load_project
from ecat_groundstation.setup_core import FIRST_RUN_YAML, atomic_json, launch_file, sha256_file
from ecat_groundstation.workspace_config import write_workspace_config, config_path, infer_workspace, last_workspace_project
from ecat_groundstation import setup_worker as worker


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'EGC_ws'
    package = root / 'src/soem_bringup'
    (package / 'config').mkdir(parents=True)
    (package / 'launch').mkdir()
    (root / 'src/EcatV2_Master/src/soem_wrapper').mkdir(parents=True)
    (package / 'package.xml').write_text('<package format="3"><name>soem_bringup</name><export><build_type>ament_cmake</build_type></export></package>')
    (package / 'CMakeLists.txt').write_text('project(soem_bringup)\nfind_package(ament_cmake REQUIRED)\nament_package()\n')
    (package / 'config/config.yaml').write_text(FIRST_RUN_YAML)
    (package / 'launch/bringup.launch.py').write_text(launch_file('enp3s0', 2, '0-1'))
    atomic_json(package / '.ecat-groundstation-generated.json', {'version': 1, 'hashes': {
        rel: sha256_file(package / rel) for rel in ('config/config.yaml', 'launch/bringup.launch.py', 'CMakeLists.txt', 'package.xml')}})
    return root


def project_for(workspace):
    slave = Slave('9876543')
    slave.tasks = [Task.create(12, slave.sn, 1)]
    return Project([slave], workspace=str(workspace), interface='enp3s0', rt_cpu=2, non_rt_cpus='0-1')


def test_apply_writes_actual_yaml_backup_and_project_snapshot(workspace):
    project = project_for(workspace)
    project.slaves[0].tasks[0].values['sdowrite_can_id'] = 21
    result = write_workspace_config(project)
    assert Path(result['config']).read_text() == project.export_yaml()
    assert Path(result['backup']).read_text() == FIRST_RUN_YAML
    assert load_project(result['project_path']).to_dict() == project.to_dict()
    assert infer_workspace(result['config']) == str(workspace)
    assert last_workspace_project({'workspace': str(workspace)}) == Path(result['project_path'])
    marker = json.loads((workspace / 'src/soem_bringup/.ecat-groundstation-generated.json').read_text())
    assert marker['configuration_ready']
    assert marker['hashes']['config/config.yaml'] == result['config_hash']
    repeated = write_workspace_config(project)
    assert not repeated['changed'] and not repeated['backup']


def test_invalid_task_configuration_never_replaces_current_yaml(workspace):
    project = project_for(workspace)
    project.slaves[0].tasks.append(Task.create(12, '9876543', 2))
    with pytest.raises(ValueError, match='资源冲突'):
        write_workspace_config(project)
    assert config_path(workspace).read_text() == FIRST_RUN_YAML
    assert not (workspace / '.ecat-groundstation').exists()


def test_unbound_demo_and_wrong_workspace_are_rejected(tmp_path):
    project = Project.demo()
    assert not project.workspace
    with pytest.raises(ValueError, match='尚未关联'):
        write_workspace_config(project)
    project.workspace = str(tmp_path)
    with pytest.raises(ValueError, match='没有 soem_bringup'):
        write_workspace_config(project)


def test_config_symlink_cannot_overwrite_outside_workspace(workspace, tmp_path):
    outside = tmp_path / 'unrelated.yaml'
    outside.write_text('preserve this file')
    target = config_path(workspace)
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match='工作空间之外'):
        write_workspace_config(project_for(workspace))
    assert outside.read_text() == 'preserve this file'


def test_recreating_bringup_preserves_final_task_configuration(workspace):
    project = project_for(workspace)
    write_workspace_config(project)
    args = Namespace(workspace=str(workspace), interface='enp3s0', rt_cpu=2, non_rt_cpus='0-1', ros_setup='/opt/ros/humble/setup.bash')
    worker.create_bringup(args)
    assert config_path(workspace).read_text() == project.export_yaml()
    assert 'sn2228293' not in config_path(workspace).read_text()


@pytest.mark.parametrize('mode', ['success', 'stale-install', 'source-changed'])
def test_build_verifies_installed_configuration_hash(workspace, monkeypatch, mode):
    project = project_for(workspace)
    applied = write_workspace_config(project)
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    def build(setup, command, root, timeout=0):
        assert command[:2] == ['colcon', 'build']
        install = root / 'install/soem_bringup/share/soem_bringup'
        (install / 'config').mkdir(parents=True)
        (install / 'launch').mkdir()
        (root / 'install/setup.bash').write_text('')
        (install / 'launch/bringup.launch.py').write_text('')
        (install / 'config/config.yaml').write_text(FIRST_RUN_YAML if mode == 'stale-install' else project.export_yaml())
        if mode == 'source-changed':
            config_path(root).write_text('# changed while building\n')
    monkeypatch.setattr(worker, 'ros_command', build)
    args = Namespace(workspace=str(workspace), ros_setup='/opt/ros/humble/setup.bash', expected_config_hash=applied['config_hash'])
    if mode == 'success':
        result = worker.build_workspace(args)
        assert result['config_hash'] == applied['config_hash']
        assert Path(result['installed_config']).read_text() == project.export_yaml()
    else:
        with pytest.raises(RuntimeError, match='不一致'):
            worker.build_workspace(args)


def test_build_does_not_run_if_saved_config_has_changed(workspace, monkeypatch):
    project = project_for(workspace)
    applied = write_workspace_config(project)
    config_path(workspace).write_text('external edit\n')
    monkeypatch.setattr(worker, 'ensure_no_master', lambda: None)
    def must_not_build(*args, **kwargs):
        pytest.fail('Build ran against a different config')
    monkeypatch.setattr(worker, 'ros_command', must_not_build)
    with pytest.raises(ValueError, match='源 config.yaml 已变化'):
        worker.build_workspace(Namespace(workspace=str(workspace), expected_config_hash=applied['config_hash']))
