import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
from pathlib import Path

import pytest
from PyQt5.QtWidgets import QApplication, QMessageBox, QFileDialog
from PyQt5.QtTest import QTest

from ecat_groundstation.app import MainWindow
from ecat_groundstation.first_setup import FirstSetupPage
from ecat_groundstation.config import Project, Slave, Task
from ecat_groundstation.setup_core import FIRST_RUN_YAML, Cpu, choose_isolation
from ecat_groundstation.workspace_config import WorkspaceAccessError, write_workspace_config


@pytest.fixture(scope='module')
def app():
    app = QApplication.instance() or QApplication([])
    yield app


def make_project(tmp_path):
    ws = tmp_path / 'EGC_ws'
    package = ws / 'src/soem_bringup'
    (package / 'config').mkdir(parents=True)
    (package / 'package.xml').write_text('<package><name>soem_bringup</name></package>')
    (package / 'config/config.yaml').write_text(FIRST_RUN_YAML)
    slave = Slave('9876543')
    slave.tasks = [Task.create(12, slave.sn, 1)]
    return Project([slave], workspace=str(ws))


def test_navigation_order_and_removed_pages(app):
    w = MainWindow()
    assert w.nav.count() == w.pages.count() == 5
    names = [w.nav.item(i).text() for i in range(w.nav.count())]
    for text, expected in zip(names, ['工作台总览', '首次配置', '从站与任务', '执行器调试', '配置与日志']):
        assert text.endswith(expected)
    assert not hasattr(w, 'resource_table') and not hasattr(w, 'graph_table')
    assert w.pages.widget(1) is w.first_setup
    w.dirty = False
    w.close()
    app.processEvents()


def test_apply_task_replaces_workspace_yaml_and_build_uses_user_job(app, tmp_path):
    project = make_project(tmp_path)
    w = MainWindow(project)
    w.first_setup.state_path = tmp_path / 'setup-state.json'
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.field_editors['sdowrite_can_id'][1].setValue(21)
    applied = w.apply_task()
    assert applied and Path(applied['config']).read_text() == w.project.export_yaml()
    assert '已写入' in w.config_apply_status.text()
    assert '21' in Path(applied['config']).read_text()
    calls = []
    w.first_setup.build_workspace = lambda **kwargs: calls.append(kwargs) or True
    w.build_task_workspace()
    assert calls[0]['workspace'] == project.workspace
    assert calls[0]['expected_hash'] == w.last_config_write['config_hash']
    assert calls[0]['open_terminal'] is False
    w.first_setup.jobs.busy = True
    w.poll()
    assert not w.config_split.isEnabled() and w.config_cancel_btn.isEnabled()
    w.first_setup.jobs.busy = False
    w.workspace_build_finished(True, {**w.last_config_write, 'installed_config': str(tmp_path / 'install/config.yaml')})
    assert '安装配置已更新并核对一致' in w.config_apply_status.text()
    w.dirty = False
    w.close()
    app.processEvents()


def test_apply_dm_task_with_matching_command_and_feedback_ids(app, tmp_path):
    project = make_project(tmp_path)
    w = MainWindow(project)
    w.first_setup.state_path = tmp_path / 'setup-state.json'
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.field_editors['sdowrite_master_id'][1].setValue(1)
    can = w.field_editors['sdowrite_can_inst'][1]
    can.setCurrentIndex(can.findData(2))
    result = w.apply_task()
    assert result
    loaded, warnings = Project.import_yaml(Path(result['config']).read_text())
    task = loaded.slaves[0].tasks[0]
    assert not warnings
    assert task.values['sdowrite_can_id'] == task.values['sdowrite_master_id'] == 1
    assert task.values['sdowrite_can_inst'] == 2
    assert not w.project.validate()
    w.dirty = False
    w.close()
    app.processEvents()


@pytest.mark.parametrize('build_after', [False, True])
@pytest.mark.parametrize('authorized', [False, True])
def test_root_metadata_repair_preserves_edits_and_retries_after_authorization(app, tmp_path, monkeypatch, build_after, authorized):
    project = make_project(tmp_path)
    w = MainWindow(project)
    w.first_setup.state_path = tmp_path / 'state.json'
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.field_editors['sdowrite_can_id'][1].setValue(22)
    attempts, jobs, builds = [], [], []

    def initial_failure_then_write(p):
        attempts.append(p)
        if len(attempts) == 1:
            raise WorkspaceAccessError('root-owned app data')
        return write_workspace_config(p)

    monkeypatch.setattr('ecat_groundstation.app.write_workspace_config', initial_failure_then_write)
    w.first_setup.start_job = lambda action, args=(): jobs.append((action, args)) or True
    w.first_setup.build_workspace = lambda **kwargs: builds.append(kwargs) or True
    if build_after:
        w.build_task_workspace()
    else:
        w.apply_task()
    target = Path(project.workspace) / 'src/soem_bringup/config/config.yaml'
    assert target.read_text() == FIRST_RUN_YAML
    assert jobs == [('repair-workspace-access', ['--workspace', project.workspace])]
    w.config_access_repaired('repair-workspace-access', authorized, {'message': 'cancelled' if not authorized else 'fixed'})
    QTest.qWait(30)
    assert project.slaves[0].tasks[0].values['sdowrite_can_id'] == 22
    if authorized:
        assert target.read_text() == project.export_yaml()
        assert len(attempts) == 2 and bool(builds) == build_after
    else:
        assert target.read_text() == FIRST_RUN_YAML and not builds
        assert len(attempts) == 1 and 'cancelled' in w.config_apply_status.text()
    w.config_build_active = False
    w.dirty = False
    w.close()
    app.processEvents()


def test_create_bringup_automatically_checks_and_then_generates(app, tmp_path):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    calls = []
    page.start_job = lambda action, args=(): calls.append((action, args)) or True
    page.interface.setCurrentText('enp3s0')
    page.create_bringup()
    assert calls == [('inspect', ())]
    plan = choose_isolation([Cpu(0, (0, 1)), Cpu(1, (0, 1)), Cpu(2, (2, 3)), Cpu(3, (2, 3))]).to_dict()
    data = {'kernel': 'test-realtime', 'realtime': True, 'boot_id': 'test',
            'pro': {'attached': True, 'rt_enabled': True}, 'cpus': [], 'plan': plan,
            'isolation_checks': {'isolated': True, 'nohz_full': True, 'rcu_nocbs': True, 'irqaffinity': True, 'nohz': True}}
    page.job_finished('inspect', True, data)
    QTest.qWait(30)
    assert calls[-1][0] == 'create-bringup'
    assert '--rt-cpu' in calls[-1][1]
    page.timer.stop()
    page.close()


def test_create_bringup_warns_only_for_actual_missing_settings(app, tmp_path, monkeypatch):
    page = FirstSetupPage(state_path=tmp_path / 'state.json')
    calls, messages = [], []
    page.start_job = lambda action, args=(): calls.append(action)
    monkeypatch.setattr(QMessageBox, 'information', lambda parent, title, message: messages.append(message))
    page.current_plan = {'rt_cpu': 2, 'non_rt_cpus': '0-1'}
    page.inspection = {'realtime': True, 'isolation_checks': {'isolated': True, 'nohz_full': False}}
    page.create_bringup_checked()
    assert not calls and 'nohz_full' in messages[0]
    page.timer.stop()
    page.close()


@pytest.mark.parametrize('destination', [MainWindow.PAGE_CONFIG, MainWindow.PAGE_CONTROL])
def test_load_workspace_populates_editor_and_debug_topics_then_edits_selected_workspace(app, tmp_path, monkeypatch, destination):
    existing = make_project(tmp_path)
    slave = existing.slaves[0]
    slave.tasks = [Task.create(4, slave.sn, 1)]
    slave.tasks[0].values['sub_topic'] = '/custom/real/throttle'
    target = Path(existing.workspace) / 'src/soem_bringup/config/config.yaml'
    target.write_text(existing.export_yaml())
    original = target.read_bytes()
    w = MainWindow()
    w.first_setup.state_path = tmp_path / 'ui-state.json'
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: existing.workspace)
    w.load_existing_workspace(destination)
    assert w.nav.currentRow() == destination
    assert w.project.workspace == existing.workspace and not w.dirty
    assert w.config_workspace.text() == w.control_workspace.text() == existing.workspace
    assert w.first_setup.workspace_edit.text() == existing.workspace
    assert w.tree.topLevelItem(0).text(0).endswith(slave.sn)
    assert w.control_keys == [f'sn{slave.sn}/app_1']
    assert w.pending_task.values['sub_topic'] == '/custom/real/throttle'
    assert target.read_bytes() == original  # Loading must not write or regenerate YAML.
    w.mode_combo.setCurrentIndex(0)
    w.toggle_connection()
    assert w.bridge.mode == 'demo'
    ep = w.bridge.endpoints[w.control_keys[0]]
    assert ep.task.values['sub_topic'] == '/custom/real/throttle'
    w.open_panel(0)
    assert len(w.panels) == 1
    w.poll()
    assert not w.load_control_workspace_btn.isEnabled()
    w.toggle_connection()
    w.field_editors['sdowrite_init_value'][1].setValue(100)
    assert w.apply_task()
    actual, _ = Project.import_yaml(target.read_text())
    assert actual.slaves[0].tasks[0].values['sdowrite_init_value'] == 100
    assert actual.slaves[0].tasks[0].values['sub_topic'] == '/custom/real/throttle'
    w.dirty = False
    w.close()
    app.processEvents()


def test_loading_bad_workspace_or_cancelling_discard_keeps_current_project(app, tmp_path, monkeypatch):
    existing = make_project(tmp_path)
    target = Path(existing.workspace) / 'src/soem_bringup/config/config.yaml'
    target.write_text('slaves: [')
    w = MainWindow()
    original = w.project
    messages = []
    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: existing.workspace)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: messages.append(args))
    w.load_existing_workspace()
    assert w.project is original and messages
    target.write_text(existing.export_yaml())
    w.dirty = True
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Cancel)
    w.load_existing_workspace()
    assert w.project is original and w.dirty
    w.dirty = False
    w.close()
    app.processEvents()
