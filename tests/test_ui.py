import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest

from ecat_groundstation.app import MainWindow, STYLE
from ecat_groundstation.config import Project


@pytest.fixture(scope='module')
def app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    yield app


def test_task_editor_mode_switch_save_and_demo_panel(app, tmp_path):
    window = MainWindow(Project.demo())
    window.show()
    try:
        window.nav.setCurrentRow(window.PAGE_CONFIG)
        window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
        mode_editor = window.field_editors['sdowrite_control_type'][1]
        mode_editor.setCurrentIndex(2)
        QTest.qWait(40)
        window.apply_task()
        assert window.project.slaves[0].tasks[0].values['sdowrite_control_type'] == 3
        assert window.project.slaves[0].allocations()[2] == 13
        # Navigating away retains field edits.
        editor = window.field_editors['sdowrite_can_id'][1]
        editor.setValue(21)
        window.tree.setCurrentItem(window.tree.topLevelItem(0).child(1))
        assert window.project.slaves[0].tasks[0].values['sdowrite_can_id'] == 21
        window.project_path = str(tmp_path / 'project.json')
        assert window.save()
        window.toggle_connection()
        QTest.qWait(150)
        assert window.bridge.mode == 'demo'
        window.open_panel(0)
        QTest.qWait(150)
        panel = window.panels['sn4587574/app_1']
        assert 'speed' in panel.values()
        assert panel.feedback_table.rowCount() > 0
        window.emergency()
        assert window.bridge.stopped
        window.toggle_connection()
        assert window.bridge.mode == 'disconnected'
    finally:
        window.dirty = False
        window.close()
        app.processEvents()
