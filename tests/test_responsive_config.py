"""Regression for cramped slave/task controls on smaller and scaled displays."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import subprocess
import sys

import pytest
from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtWidgets import QApplication, QPushButton
from PyQt5.QtTest import QTest

from ecat_groundstation.app import MainWindow, STYLE


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    monkeypatch.setattr('ecat_groundstation.first_setup.detect_stlinks', lambda: [])
    w = MainWindow()
    w.first_setup.state_path = tmp_path / 'state.json'
    w.discard_ok = lambda: True
    w.nav.setCurrentRow(w.PAGE_CONFIG)
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.show()
    yield w
    w.close()
    app.processEvents()


@pytest.mark.parametrize('size,compact', [((1500, 920), False), ((1280, 720), False), ((960, 600), True)])
def test_forms_and_actions_do_not_overlap_when_build_log_is_visible(window, size, compact):
    w = window
    w.resize(*size)
    w.config_build_log.setPlainText('colcon build completed')
    w.config_build_log.show()
    w.config_build_progress.show()
    w.config_apply_status.setText('编译成功：/home/new-user/EtherCAT-workspace/install/soem_bringup/share/soem_bringup/config/config.yaml\n请重新运行主站加载新配置。')
    QTest.qWait(80)
    assert w.width() == size[0] and w.height() == size[1]
    assert w.compact_config is compact
    assert w.config_split.height() >= 420
    if compact:
        w.config_tabs.setCurrentIndex(0)
        QTest.qWait(30)
    middle = w.slave_scroll.widget()
    controls = [w.sn_edit, w.model_edit, w.latency_edit, w.budget, w.task_type, w.task_table]
    rectangles = [QRect(widget.mapTo(middle, QPoint()), widget.size()) for widget in controls]
    for index, (widget, rect) in enumerate(zip(controls, rectangles)):
        assert rect.height() >= 20
        assert rect.width() >= 100
        for other in rectangles[index + 1:]:
            assert not rect.intersects(other)
    assert w.task_table.height() >= 160
    if compact:
        w.config_tabs.setCurrentIndex(1)
        QTest.qWait(30)
    apply = next(b for b in w.task_panel.findChildren(QPushButton) if b.text() == '应用任务修改')
    assert w.form_scroll.geometry().bottom() < apply.geometry().top()
    assert apply.geometry().bottom() < w.config_build_btn.geometry().top()
    # The page scrolls instead of stealing height from form controls.
    assert w.config_page_scroll.verticalScrollBar().maximum() > 0


def test_window_resize_preserves_unapplied_fields_and_task_navigation(window):
    w = window
    w.resize(1500, 920)
    QTest.qWait(60)
    editor = w.field_editors['sdowrite_master_id'][1]
    editor.setValue(31)
    w.sn_edit.setText('1234567')
    w.resize(960, 600)
    QTest.qWait(60)
    assert w.compact_config and w.config_tabs.currentIndex() == 1
    assert editor is w.field_editors['sdowrite_master_id'][1]
    assert editor.value() == 31 and w.sn_edit.text() == '1234567'
    w.resize(1500, 920)
    QTest.qWait(60)
    assert not w.compact_config and w.config_split.count() == 3
    assert w.config_page_scroll.verticalScrollBar().maximum() == 0
    assert editor is w.field_editors['sdowrite_master_id'][1]
    assert editor.value() == 31 and w.sn_edit.text() == '1234567'
    w.resize(960, 600)
    QTest.qWait(60)
    w.tree.setCurrentItem(w.tree.topLevelItem(0))
    assert w.config_tabs.currentIndex() == 0
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    assert w.config_tabs.currentIndex() == 1
    assert w.field_editors['sdowrite_master_id'][1].value() == 31


@pytest.mark.parametrize('scale', ['1.25', '1.5', '2'])
def test_scaled_display_keeps_slave_form_readable_in_compact_mode(scale):
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=scale)
    code = '''
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest
from ecat_groundstation.app import MainWindow, STYLE
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
app = QApplication([])
app.setStyleSheet(STYLE)
w = MainWindow()
w.discard_ok = lambda: True
w.resize(960, 540)
w.nav.setCurrentRow(w.PAGE_CONFIG)
w.show()
QTest.qWait(100)
assert w.compact_config
assert w.config_split.height() >= 420
assert w.sn_edit.height() >= 28 and w.model_edit.height() >= 28
assert w.task_table.height() >= 160
assert w.config_page_scroll.verticalScrollBar().maximum() > 0
w.close()
app.processEvents()
'''
    result = subprocess.run([sys.executable, '-c', code], env=env, text=True,
                            capture_output=True, timeout=20, cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr
