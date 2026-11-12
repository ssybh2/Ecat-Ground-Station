from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import time

from PyQt5.QtCore import Qt, QTimer, QProcess, QProcessEnvironment
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QStackedWidget, QListWidget, QTreeWidget, QTreeWidgetItem, QSplitter,
    QComboBox, QLineEdit, QFormLayout, QScrollArea, QTableWidget, QTableWidgetItem,
    QHeaderView, QPlainTextEdit, QTabWidget, QFileDialog, QMessageBox, QInputDialog,
    QAction, QSpinBox, QDoubleSpinBox, QFrame, QAbstractItemView,
)
from .config import Project, Slave, Task, atomic_write, save_project, load_project
from .schema import SPECS, Field, active_fields, pdo_size, resources, message_types
from .bridge import Bridge
from .widgets import Topology, CommandPanel


STYLE = '''
QWidget { background: #131c25; color: #dce7ef; font-size: 13px; }
QMainWindow { background: #0e151e; }
QLabel { background: transparent; }
QLabel#brand { font-size: 20px; font-weight: 700; color: #f1f7fa; }
QLabel#pageTitle { font-size: 24px; font-weight: 600; padding: 6px 0; }
QLabel#muted { color: #8fa5b6; }
QLabel#notice { color: #63d7c4; background: #17322f; padding: 10px; border-radius: 5px; }
QLabel#metric { font-size: 28px; font-weight: 600; color: #64ddc4; }
QFrame#card { background: #1a2732; border: 1px solid #2a3a48; border-radius: 8px; }
QFrame#header { background: #192631; border-bottom: 1px solid #2a3a48; }
QPushButton { background: #263846; border: 1px solid #354b5c; padding: 8px 14px; border-radius: 5px; }
QPushButton:hover { background: #304c5d; border-color: #5f91a5; }
QPushButton:pressed { background: #285a58; }
QPushButton:disabled { color: #607382; background: #1a2732; border-color: #293845; }
QPushButton#primary { color: #0d2626; background: #5bd6bc; border-color: #5bd6bc; font-weight: 600; }
QPushButton#primary:pressed { background: #38b29a; }
QPushButton#danger { background: #943b42; border-color: #bb5961; color: white; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: #101922; border: 1px solid #354857; padding: 6px; border-radius: 4px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #58c8b3; }
QComboBox QAbstractItemView { background: #1b2b37; selection-background-color: #2a5b61; }
QTreeWidget, QListWidget, QTableWidget, QPlainTextEdit { background: #101922; border: 1px solid #293b49; border-radius: 5px; selection-background-color: #254e59; }
QTreeWidget::item, QListWidget::item { padding: 9px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #254e59; color: #85edd9; }
QTableWidget { gridline-color: #263845; }
QTableWidget::item { padding: 7px; }
QHeaderView::section { background: #1c2c39; color: #94abba; padding: 9px; border: none; border-right: 1px solid #293b49; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #14202a; width: 9px; }
QScrollBar::handle:vertical { background: #3a5262; border-radius: 4px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTabWidget::pane { border: 1px solid #293b49; }
QTabBar::tab { background: #192631; color: #8fa5b6; padding: 10px 20px; }
QTabBar::tab:selected { color: #6bdfc7; background: #243b46; }
QCheckBox { padding: 7px 0; }
QSplitter::handle { background: #273a48; }
QStatusBar { background: #111c25; color: #8ca7b8; }
QMenuBar, QMenu { background: #192631; }
QMenu::item:selected { background: #254e59; }
QToolTip { color: #e0edf3; background: #243845; border: 1px solid #466173; padding: 6px; }
'''


def button(text, slot, name=None):
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.clicked.connect(slot)
    return b


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.verticalHeader().hide()
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill_table(t, rows):
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            t.setItem(i, j, QTableWidgetItem(str(value)))


class MainWindow(QMainWindow):
    def __init__(self, project=None):
        super().__init__()
        self.project = project or Project.demo()
        self.project_path = None
        self.dirty = False
        self.pending_task = None
        self.panels = {}
        self.bridge = Bridge()
        self.selection = (0, -1)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_process)
        self.process.finished.connect(self.master_finished)
        self.process.errorOccurred.connect(lambda e: self.append_log('主站进程: ' + self.process.errorString()))
        self.log_seen = None
        self.last_graph = None
        self.setWindowTitle('Ecat GroundStation · EtherCAT 配置与调试')
        self.resize(1500, 920)
        self.setMinimumSize(1080, 700)
        self.build_ui()
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)
        self.append_log('就绪。默认离线编辑；连接演示或 ROS 2 后可打开独立调试窗口。')

    def build_ui(self):
        for title, text, key, slot in (
            ('文件', '新建项目', 'Ctrl+N', self.new_project),
            ('文件', '打开项目 / 导入 YAML', 'Ctrl+O', self.open_file),
            ('文件', '保存项目', 'Ctrl+S', self.save),
            ('文件', '项目另存为', 'Ctrl+Shift+S', lambda: self.save(True)),
            ('文件', '导出主站 config.yaml', 'Ctrl+E', self.export),
            ('工具', '校验配置', 'F7', self.validate),
        ):
            menu = next((a.menu() for a in self.menuBar().actions() if a.text() == title), None)
            if menu is None:
                menu = self.menuBar().addMenu(title)
            action = QAction(text, self)
            action.setShortcut(key)
            action.triggered.connect(slot)
            menu.addAction(action)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName('header')
        hl = QHBoxLayout(header)
        hl.setContentsMargins(22, 14, 22, 14)
        brand = QLabel('◈  Ecat GroundStation')
        brand.setObjectName('brand')
        hl.addWidget(brand)
        tagline = QLabel('AIM EtherCAT  /  配置与调试工作台')
        tagline.setObjectName('muted')
        hl.addWidget(tagline)
        hl.addStretch()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(['离线演示', 'ROS 2 实机'])
        hl.addWidget(self.mode_combo)
        self.connect_btn = button('连接', self.toggle_connection, 'primary')
        hl.addWidget(self.connect_btn)
        hl.addWidget(button('■  软件停止', self.emergency, 'danger'))
        self.reset_button = button('复位停止', self.reset_stop)
        self.reset_button.setVisible(False)
        hl.addWidget(self.reset_button)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.addItems(['◫   工作台总览', '⚙   从站与任务', '⇄   端口与资源', '◎   执行器调试', '⌁   ROS 2 与主站', '≡   配置与日志'])
        self.nav.setFixedWidth(175)
        self.nav.setCurrentRow(0)
        self.pages = QStackedWidget()
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.build_overview()
        self.build_config()
        self.build_resources()
        self.build_control()
        self.build_connection()
        self.build_output()
        self.statusBar().showMessage('离线编辑 · 配置尚未应用至硬件')

    def page(self, title, subtitle):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(24, 18, 24, 20)
        label = QLabel(title)
        label.setObjectName('pageTitle')
        l.addWidget(label)
        sub = QLabel(subtitle)
        sub.setObjectName('muted')
        sub.setWordWrap(True)
        l.addWidget(sub)
        l.addSpacing(10)
        self.pages.addWidget(w)
        return l

    def build_overview(self):
        l = self.page('设备工作台', '从设备配置到执行器调试，在同一个工程中完成。')
        stats = QHBoxLayout()
        self.metrics = []
        for title in ('配置从站', '设备任务', '执行器任务', '配置校验'):
            card = QFrame()
            card.setObjectName('card')
            cl = QVBoxLayout(card)
            cl.setContentsMargins(20, 16, 20, 16)
            name = QLabel(title)
            name.setObjectName('muted')
            value = QLabel('—')
            value.setObjectName('metric')
            cl.addWidget(name)
            cl.addWidget(value)
            self.metrics.append(value)
            stats.addWidget(card)
        l.addLayout(stats)
        self.overview_mode = QLabel('离线编辑 · 拓扑来自配置，未连接设备')
        self.overview_mode.setObjectName('notice')
        l.addWidget(self.overview_mode)
        self.topology = Topology()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.topology)
        l.addWidget(scroll, 1)
        self.overview_table = table(['从站 SN', '模块型号', '任务数', '反馈 PDO', '命令 PDO', 'SDO 参数'])
        l.addWidget(self.overview_table, 1)
        actions = QHBoxLayout()
        actions.addWidget(button('添加从站', self.add_slave, 'primary'))
        actions.addWidget(button('导入 YAML', self.open_file))
        actions.addWidget(button('校验并查看配置', self.show_output))
        actions.addWidget(button('进入执行器调试 →', lambda: self.nav.setCurrentRow(3)))
        actions.addStretch()
        l.addLayout(actions)

    def build_config(self):
        l = self.page('从站与任务', '为每块 H750 独立配置任务、端口、控制模式与话题。SDO 长度和 PDO 偏移自动生成。')
        self.config_toolbar = QWidget()
        toolbar = QHBoxLayout(self.config_toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(button('+ 从站', self.add_slave, 'primary'))
        toolbar.addWidget(button('删除从站', self.remove_slave))
        toolbar.addWidget(button('导出 YAML', self.export))
        toolbar.addStretch()
        l.addWidget(self.config_toolbar)
        self.edit_notice = QLabel('配置仅在主站启动时载入；完成校验后，在「ROS 2 与主站」页导出当前配置并启动主站。')
        self.edit_notice.setObjectName('muted')
        self.edit_notice.setWordWrap(True)
        l.addWidget(self.edit_notice)
        self.config_split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel('项目设备')
        self.tree.currentItemChanged.connect(self.tree_selected)
        self.config_split.addWidget(self.tree)
        middle = QWidget()
        ml = QVBoxLayout(middle)
        self.slave_title = QLabel('从站设置')
        ml.addWidget(self.slave_title)
        form = QFormLayout()
        self.sn_edit = QLineEdit()
        self.model_edit = QComboBox()
        self.model_edit.addItem('H750 标准版 · 80 / 80 B', 'standard')
        self.model_edit.addItem('H750 Large PDO · 112 / 80 B', 'large')
        self.latency_edit = QLineEdit()
        form.addRow('模块 SN', self.sn_edit)
        form.addRow('硬件型号', self.model_edit)
        form.addRow('延迟反馈话题', self.latency_edit)
        ml.addLayout(form)
        ml.addWidget(button('应用从站设置', self.apply_slave))
        self.budget = QLabel()
        self.budget.setObjectName('notice')
        ml.addWidget(self.budget)
        add_row = QHBoxLayout()
        self.task_type = QComboBox()
        for spec in SPECS.values():
            self.task_type.addItem(f'{spec.id:02d} · {spec.name}', spec.id)
        add_row.addWidget(self.task_type, 1)
        add_row.addWidget(button('+ 任务', self.add_task, 'primary'))
        ml.addLayout(add_row)
        self.task_table = table(['#', '任务', '反馈 B', '命令 B'])
        self.task_table.itemSelectionChanged.connect(self.table_selected)
        ml.addWidget(self.task_table, 1)
        row = QHBoxLayout()
        row.addWidget(button('上移', lambda: self.move_task(-1)))
        row.addWidget(button('下移', lambda: self.move_task(1)))
        row.addWidget(button('删除任务', self.remove_task))
        ml.addLayout(row)
        self.config_split.addWidget(middle)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.task_editor_title = QLabel('选择一个任务以编辑')
        self.task_editor_title.setObjectName('brand')
        self.task_editor_title.setWordWrap(True)
        rl.addWidget(self.task_editor_title)
        self.task_note = QLabel()
        self.task_note.setWordWrap(True)
        self.task_note.setObjectName('muted')
        rl.addWidget(self.task_note)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        rl.addWidget(self.form_scroll, 1)
        rl.addWidget(button('应用任务修改', self.apply_task, 'primary'))
        self.config_split.addWidget(right)
        self.config_split.setSizes([220, 470, 420])
        l.addWidget(self.config_split, 1)

    def build_resources(self):
        l = self.page('端口与资源', '检查每个从站的 CAN ID、TIM、UART、DBUS 与 I²C 分配；相同端口的独占任务会阻止导出。')
        self.resource_table = table(['从站', '任务', '端口 / 总线', '资源 / 标识', '占用方式'])
        l.addWidget(self.resource_table, 1)
        l.addWidget(QLabel('CAN 允许共享总线，帧 ID 必须避免冲突。板级 UART 复用、电气接线及波特率还需按硬件手册核对。'))
        l.addWidget(button('重新校验', self.validate))

    def build_control(self):
        l = self.page('执行器与传感器调试', '双击任务打开独立窗口，可分别查看反馈、编辑目标值、绘制曲线并导出 CSV。')
        self.control_table = table(['任务', '设备', '端口', '命令消息', '反馈状态', '接收 / 发送'])
        self.control_table.cellDoubleClicked.connect(lambda row, col: self.open_panel(row))
        l.addWidget(self.control_table, 1)
        row = QHBoxLayout()
        row.addWidget(button('打开独立调试窗口', lambda: self.open_panel(self.control_table.currentRow()), 'primary'))
        row.addWidget(button('关闭全部调试窗口', self.close_panels))
        row.addStretch()
        l.addLayout(row)
        tip = QLabel('真实电机控制要求：已启动兼容主站、话题匹配、有效反馈、允许输出并持续按住运行。\n'
                     'PWM / DSHOT 没有反馈，释放时恢复配置初值；请先核对该初值对应执行器的实际行为。')
        tip.setWordWrap(True)
        tip.setObjectName('notice')
        l.addWidget(tip)

    def build_connection(self):
        l = self.page('ROS 2 与主站', '上位机作为 ROS 2 节点连接原主站；EtherCAT 实时循环继续由 soem_backend 执行。')
        self.connection_settings = QWidget()
        form = QFormLayout(self.connection_settings)
        self.workspace_edit = QLineEdit(self.project.workspace)
        self.interface_edit = QComboBox()
        self.interface_edit.setEditable(True)
        self.interface_edit.addItems(sorted(p.name for p in Path('/sys/class/net').iterdir()))
        self.interface_edit.setCurrentText(self.project.interface)
        self.domain_edit = QSpinBox()
        self.domain_edit.setRange(0, 232)
        self.domain_edit.setValue(self.project.ros_domain)
        self.cpu_edit = QSpinBox()
        self.cpu_edit.setRange(0, 4095)
        self.cpu_edit.setValue(self.project.rt_cpu)
        self.non_rt_edit = QLineEdit(self.project.non_rt_cpus)
        form.addRow('ROS 工作空间', self.workspace_edit)
        form.addRow('EtherCAT 网卡', self.interface_edit)
        form.addRow('ROS_DOMAIN_ID', self.domain_edit)
        form.addRow('实时 CPU', self.cpu_edit)
        form.addRow('非实时 CPU', self.non_rt_edit)
        l.addWidget(self.connection_settings)
        l.addWidget(button('保存连接设置', self.apply_connection))
        row = QHBoxLayout()
        self.start_master_btn = button('导出当前配置并启动主站', self.start_master, 'primary')
        row.addWidget(self.start_master_btn)
        row.addWidget(button('停止本软件启动的主站', self.stop_master))
        row.addWidget(button('刷新 ROS 图', self.refresh_graph))
        row.addStretch()
        l.addLayout(row)
        self.master_status = QLabel('主站未由本软件启动。连接现有主站无需再次启动。')
        self.master_status.setWordWrap(True)
        self.master_status.setObjectName('notice')
        l.addWidget(self.master_status)
        self.graph_table = table(['ROS 2 话题', '消息类型'])
        l.addWidget(self.graph_table, 1)
        note = QLabel('主站启动需要正确的网卡、实时权限及原仓库的构建产物。若权限不足，请按原仓库环境文档配置。\n'
                      '本软件不自动提升权限。ROS 话题发现不等于 EtherCAT 总线扫描或 OP 状态确认。')
        note.setObjectName('muted')
        note.setWordWrap(True)
        l.addWidget(note)

    def build_output(self):
        l = self.page('配置与日志', '查看主站实际使用的带类型标签 YAML，并检查校验结果和运行日志。')
        row = QHBoxLayout()
        row.addWidget(button('校验配置', self.validate, 'primary'))
        row.addWidget(button('保存项目', self.save))
        row.addWidget(button('导出 YAML', self.export))
        row.addWidget(button('导出日志', self.export_logs))
        row.addStretch()
        l.addLayout(row)
        tabs = QTabWidget()
        self.yaml_view = QPlainTextEdit()
        self.yaml_view.setReadOnly(True)
        self.yaml_view.setFont(QFont('DejaVu Sans Mono', 10))
        self.validation_view = QPlainTextEdit()
        self.validation_view.setReadOnly(True)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont('DejaVu Sans Mono', 10))
        tabs.addTab(self.yaml_view, '主站 YAML 预览')
        tabs.addTab(self.validation_view, '配置校验')
        tabs.addTab(self.log_view, '运行日志')
        l.addWidget(tabs, 1)

    def refresh(self):
        si, ti = self.selection
        self.tree.blockSignals(True)
        self.tree.clear()
        selected = None
        for i, slave in enumerate(self.project.slaves):
            root = QTreeWidgetItem([f'H750 · SN {slave.sn}'])
            root.setData(0, Qt.UserRole, (i, -1))
            self.tree.addTopLevelItem(root)
            if i == si:
                selected = root
            for j, task in enumerate(slave.tasks):
                item = QTreeWidgetItem([f'app_{j + 1} · {SPECS[task.type_id].name}'])
                item.setData(0, Qt.UserRole, (i, j))
                root.addChild(item)
                if (i, j) == (si, ti):
                    selected = item
            root.setExpanded(True)
        self.tree.setCurrentItem(selected)
        self.tree.blockSignals(False)
        if selected:
            self.selection = selected.data(0, Qt.UserRole)
        else:
            self.selection = (-1, -1)
        self.load_selection()
        errors = self.project.validate()
        try:
            self.yaml_view.setPlainText(self.project.export_yaml())
        except (ValueError, KeyError) as exc:
            self.yaml_view.setPlainText('# 导出前请修正以下问题：\n' + str(exc))
        self.validation_view.setPlainText('\n'.join(errors) if errors else '✓ 配置校验通过。\nSDO 长度、PDO 容量、端口占用、CAN ID、话题名称与类型均已检查。\n硬件型号、接线、固件版本及电机参数需在实物上确认。')
        counts = [len(self.project.slaves), sum(len(s.tasks) for s in self.project.slaves),
                  sum(SPECS[t.type_id].category == '执行器' for s in self.project.slaves for t in s.tasks),
                  f'{len(errors)} 项问题' if errors else '通过']
        for label, value in zip(self.metrics, counts):
            label.setText(str(value))
        self.topology.project = self.project
        self.topology.update()
        rows = []
        rr = []
        cr = []
        self.control_keys = []
        for s in self.project.slaves:
            _, r, w = s.allocations()
            rc, wc = s.capacity
            rows.append([s.sn, 'Large PDO' if s.model == 'large' else '标准版', len(s.tasks), f'{r} / {rc} B', f'{w} / {wc} B', f'{s.sdo_length()} B'])
            for i, t in enumerate(s.tasks, 1):
                for port, identity, exclusive in resources(t):
                    rr.append([s.sn, f'app_{i} · {SPECS[t.type_id].name}', port, identity, '独占' if exclusive else '共享总线 / ID 唯一'])
                key = f'sn{s.sn}/app_{i}'
                self.control_keys.append(key)
                cr.append([key, SPECS[t.type_id].name, ', '.join(dict.fromkeys(r[0] for r in resources(t))),
                           message_types(t)[1].rsplit('/', 1)[-1] or '仅反馈', '未连接', '0 / 0'])
        fill_table(self.overview_table, rows)
        fill_table(self.resource_table, rr)
        fill_table(self.control_table, cr)
        self.setWindowTitle(('● ' if self.dirty else '') + 'Ecat GroundStation · ' + (Path(self.project_path).name if self.project_path else '未保存项目'))

    def load_selection(self):
        si, ti = self.selection
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(0)
        self.pending_task = None
        if not 0 <= si < len(self.project.slaves):
            self.sn_edit.clear()
            self.latency_edit.clear()
            self.budget.setText('请添加一个从站')
            self.render_task_editor()
            self.task_table.blockSignals(False)
            return
        s = self.project.slaves[si]
        self.slave_title.setText(f'从站 · SN {s.sn}')
        self.sn_edit.setText(s.sn)
        self.model_edit.setCurrentIndex(self.model_edit.findData(s.model))
        self.latency_edit.setText(s.latency_topic)
        _, r, w = s.allocations()
        rc, wc = s.capacity
        self.budget.setText(f'反馈 PDO  {r} / {rc} B     命令 PDO  {w} / {wc} B\nSDO 参数  {s.sdo_length()} B')
        fill_table(self.task_table, [[i, SPECS[t.type_id].name, *pdo_size(t)] for i, t in enumerate(s.tasks, 1)])
        if 0 <= ti < len(s.tasks):
            self.task_table.selectRow(ti)
            self.pending_task = copy.deepcopy(s.tasks[ti])
        self.task_table.blockSignals(False)
        self.render_task_editor()

    def tree_selected(self, current, previous):
        if current:
            self.commit_pending()
            self.selection = current.data(0, Qt.UserRole)
            self.load_selection()

    def table_selected(self):
        row = self.task_table.currentRow()
        si, _ = self.selection
        if row >= 0 and si >= 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(si).child(row))

    def render_task_editor(self):
        self.field_editors = {}
        container = QWidget()
        form = QFormLayout(container)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        if self.pending_task:
            task = self.pending_task
            self.task_editor_title.setText(SPECS[task.type_id].name)
            self.task_note.setText(SPECS[task.type_id].note or '按接线选择端口，命令与反馈话题可以独立命名。')
            fs = list(active_fields(task))
            for key, label in (('pub_topic', '反馈话题'), ('sub_topic', '命令话题')):
                if key in task.values:
                    fs.append(Field(key, 'std::string', '', label))
            mode_editors = []
            for f in fs:
                value = task.values.get(f.key, f.default)
                if f.choices:
                    editor = QComboBox()
                    for val, text in f.choices:
                        editor.addItem(text, val)
                    editor.setCurrentIndex(editor.findData(value))
                elif f.tag == 'std::string':
                    editor = QLineEdit(str(value))
                else:
                    # uint32 exceeds QSpinBox's signed range; decimal-free double preserves it exactly.
                    editor = QDoubleSpinBox()
                    editor.setDecimals(6 if f.tag == 'float' else 0)
                    editor.setRange(f.minimum if f.minimum is not None else -1e12,
                                    f.maximum if f.maximum is not None else 1e12)
                    editor.setValue(value)
                    editor.setSingleStep(.1 if f.tag == 'float' else 1)
                editor.setToolTip(f'{f.key}: !{f.tag}')
                self.field_editors[f.key] = (f, editor)
                form.addRow(f.label, editor)
                if 'control_type' in f.key or (task.type_id in (5, 15) and f.key.startswith('sdowrite_motor') and f.key.endswith('can_id')):
                    mode_editors.append(editor)
            for editor in mode_editors:
                if isinstance(editor, QComboBox):
                    editor.currentIndexChanged.connect(self.update_dynamic_fields)
                else:
                    editor.editingFinished.connect(self.update_dynamic_fields)
        else:
            self.task_editor_title.setText('选择任务')
            self.task_note.setText('在中间列表选择任务，或添加一个新任务。')
        old = self.form_scroll.takeWidget()
        if old:
            old.deleteLater()
        self.form_scroll.setWidget(container)

    def gather_task(self):
        if not self.pending_task:
            return
        for key, (f, editor) in self.field_editors.items():
            if isinstance(editor, QComboBox):
                value = editor.currentData()
            elif isinstance(editor, QLineEdit):
                value = editor.text().strip()
            else:
                value = editor.value() if f.tag == 'float' else int(editor.value())
            self.pending_task.values[key] = value

    def commit_pending(self):
        """Keep form edits when navigating, saving, exporting, or connecting."""
        si, ti = self.selection
        if self.pending_task and si >= 0 and ti >= 0 and self.bridge.mode in ('disconnected', 'error'):
            self.gather_task()
            previous = self.project.slaves[si].tasks[ti]
            if previous != self.pending_task:
                self.project.slaves[si].tasks[ti] = copy.deepcopy(self.pending_task)
                self.dirty = True

    def update_dynamic_fields(self):
        self.gather_task()
        QTimer.singleShot(0, self.render_task_editor)

    def editable(self):
        if self.bridge.mode not in ('disconnected', 'error') or self.process.state() != QProcess.NotRunning:
            QMessageBox.information(self, '配置锁定', '请先断开上位机连接，并停止本软件启动的主站，再修改配置。')
            return False
        return True

    def changed(self):
        self.close_panels()
        self.dirty = True
        self.refresh()

    def add_slave(self):
        if not self.editable():
            return
        sn, ok = QInputDialog.getText(self, '添加 AIM H750 从站', '模块序列号 SN（十进制）')
        if ok:
            sn = sn.strip()
            if not sn.isdigit() or not 0 < int(sn) <= 0xFFFFFFFF or any(s.sn == str(int(sn)) for s in self.project.slaves):
                QMessageBox.warning(self, 'SN 不合法', '需要未重复的非零 uint32 序列号。')
                return
            self.project.slaves.append(Slave(str(int(sn))))
            self.selection = len(self.project.slaves) - 1, -1
            self.changed()
            self.nav.setCurrentRow(1)

    def remove_slave(self):
        si, _ = self.selection
        if not self.editable() or si < 0:
            return
        if QMessageBox.question(self, '删除从站', f'删除 SN {self.project.slaves[si].sn} 及其全部任务？') == QMessageBox.Yes:
            self.project.slaves.pop(si)
            self.selection = max(0, si - 1), -1
            self.changed()

    def apply_slave(self):
        si, _ = self.selection
        if not self.editable() or si < 0:
            return
        s = self.project.slaves[si]
        sn = self.sn_edit.text().strip()
        if not sn.isdigit() or not 0 < int(sn) <= 0xFFFFFFFF or any(i != si and t.sn == str(int(sn)) for i, t in enumerate(self.project.slaves)):
            QMessageBox.warning(self, 'SN 不合法', '需要未重复的非零 uint32 序列号。')
            return
        old_prefix = f'/ecat/sn{s.sn}/'
        s.sn = str(int(sn))
        for task in s.tasks:
            for key in ('pub_topic', 'sub_topic'):
                if task.values.get(key, '').startswith(old_prefix):
                    task.values[key] = f'/ecat/sn{s.sn}/' + task.values[key][len(old_prefix):]
        latency = self.latency_edit.text().strip()
        s.latency_topic = latency.replace(old_prefix, f'/ecat/sn{s.sn}/', 1) if latency.startswith(old_prefix) else latency
        s.model = self.model_edit.currentData()
        self.changed()

    def add_task(self):
        si, _ = self.selection
        if not self.editable() or si < 0:
            return
        s = self.project.slaves[si]
        task = Task.create(self.task_type.currentData(), s.sn, len(s.tasks) + 1)
        # Retain unique topic names after removing/reordering previous tasks.
        used = {v for slave in self.project.slaves for t in slave.tasks for k, v in t.values.items() if k in ('pub_topic', 'sub_topic')}
        for key in ('pub_topic', 'sub_topic'):
            if key in task.values:
                base, suffix = task.values[key], 2
                while task.values[key] in used:
                    task.values[key] = f'{base}_{suffix}'
                    suffix += 1
        s.tasks.append(task)
        self.selection = si, len(s.tasks) - 1
        self.changed()

    def apply_task(self):
        si, ti = self.selection
        if not self.editable() or not self.pending_task or ti < 0:
            return
        self.gather_task()
        self.project.slaves[si].tasks[ti] = copy.deepcopy(self.pending_task)
        self.changed()
        self.append_log('任务已更新；配置尚未应用至主站。')

    def remove_task(self):
        si, ti = self.selection
        if self.editable() and si >= 0 and ti >= 0:
            self.project.slaves[si].tasks.pop(ti)
            self.selection = si, max(-1, ti - 1)
            self.changed()

    def move_task(self, delta):
        si, ti = self.selection
        if not self.editable() or si < 0 or ti < 0:
            return
        tasks = self.project.slaves[si].tasks
        target = ti + delta
        if 0 <= target < len(tasks):
            tasks[ti], tasks[target] = tasks[target], tasks[ti]
            self.selection = si, target
            self.changed()

    def discard_ok(self):
        self.commit_pending()
        if self.dirty:
            answer = QMessageBox.question(self, '未保存项目', '当前项目有未保存修改，是否保存？',
                                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Save:
                return self.save()
        return True

    def new_project(self):
        if self.editable() and self.discard_ok():
            self.project = Project(ros_domain=int(os.environ.get('ROS_DOMAIN_ID', '0')))
            self.project_path, self.dirty = None, False
            self.close_panels()
            self.sync_connection_fields()
            self.refresh()

    def open_file(self):
        if not self.editable() or not self.discard_ok():
            return
        path, _ = QFileDialog.getOpenFileName(self, '打开工程或主站配置', str(Path.home()), '配置 (*.json *.yaml *.yml);;所有文件 (*)')
        if not path:
            return
        try:
            if path.lower().endswith(('.yaml', '.yml')):
                p, warnings = Project.import_yaml(Path(path).read_text(encoding='utf-8'))
                self.project_path, self.dirty = None, True
                for warning in warnings:
                    self.append_log('导入提示: ' + warning)
                if warnings:
                    QMessageBox.information(self, '导入提示', '\n'.join(warnings[:15]))
            else:
                p = load_project(path)
                self.project_path, self.dirty = path, False
            self.project = p
            self.selection = 0, -1
            self.close_panels()
            self.sync_connection_fields()
            self.refresh()
            self.nav.setCurrentRow(1)
            self.append_log('已打开 ' + path)
        except Exception as exc:
            QMessageBox.warning(self, '导入失败', str(exc))

    def save(self, save_as=False):
        self.commit_pending()
        path = None if save_as else self.project_path
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, '保存 GroundStation 工程', 'project.ecat.json', 'GroundStation (*.json)')
        if not path:
            return False
        try:
            save_project(self.project, path)
            self.project_path, self.dirty = path, False
            self.refresh()
            self.append_log('已保存工程 ' + path)
            return True
        except Exception as exc:
            QMessageBox.warning(self, '保存失败', str(exc))
            return False

    def export(self):
        self.commit_pending()
        try:
            content = self.project.export_yaml()
            path, _ = QFileDialog.getSaveFileName(self, '导出主站配置（不会热更新硬件）', 'config.yaml', 'YAML (*.yaml)')
            if path:
                atomic_write(path, content)
                self.append_log('已导出 ' + path + '；需主站重启加载后才应用于从站。')
                self.statusBar().showMessage('YAML 已导出，硬件尚未更新')
                return path
        except Exception as exc:
            QMessageBox.warning(self, '无法导出', str(exc))
            self.nav.setCurrentRow(5)
        return None

    def validate(self):
        self.commit_pending()
        self.refresh()
        self.nav.setCurrentRow(5)
        errors = self.project.validate()
        QMessageBox.information(self, '配置校验', '\n'.join(errors[:15]) if errors else '配置校验通过。主站启动前请核对模块型号与实际接线。')

    def show_output(self):
        self.commit_pending()
        self.refresh()
        self.nav.setCurrentRow(5)

    def sync_connection_fields(self):
        self.workspace_edit.setText(self.project.workspace)
        self.interface_edit.setCurrentText(self.project.interface)
        self.domain_edit.setValue(self.project.ros_domain)
        self.cpu_edit.setValue(self.project.rt_cpu)
        self.non_rt_edit.setText(self.project.non_rt_cpus)

    def apply_connection(self):
        if not self.editable():
            return False
        self.commit_pending()
        self.project.workspace = self.workspace_edit.text().strip()
        self.project.interface = self.interface_edit.currentText().strip()
        self.project.ros_domain = self.domain_edit.value()
        self.project.rt_cpu = self.cpu_edit.value()
        self.project.non_rt_cpus = self.non_rt_edit.text().strip()
        self.changed()
        return True

    def toggle_connection(self):
        if self.bridge.mode not in ('disconnected', 'error'):
            self.close_panels()
            self.bridge.disconnect()
            return
        if self.process.state() == QProcess.NotRunning and not self.apply_connection():
            return
        errors = self.project.validate()
        if errors:
            QMessageBox.warning(self, '配置校验失败', '\n'.join(errors[:15]))
            return
        self.bridge.connect(self.project, demo=self.mode_combo.currentIndex() == 0)

    def emergency(self):
        self.bridge.stop_all()
        for panel in list(self.panels.values()):
            panel.disarm()
        self.reset_button.setVisible(True)
        self.append_log('软件停止已锁定；发送禁用 / 初值。请通过硬件急停处理失控或通信中断。')

    def reset_stop(self):
        self.bridge.reset_stop()
        self.reset_button.setVisible(False)

    def open_panel(self, row):
        if not 0 <= row < len(self.control_keys):
            return
        if self.bridge.mode not in ('demo', 'ros'):
            QMessageBox.information(self, '尚未连接', '请先在右上角选择离线演示或 ROS 2 实机并连接。')
            return
        key = self.control_keys[row]
        if key in self.panels:
            self.panels[key].showNormal()
            self.panels[key].raise_()
            self.panels[key].activateWindow()
            return
        ep = self.bridge.endpoints[key]
        panel = CommandPanel(key, copy.deepcopy(ep.task), self.bridge, self)
        self.panels[key] = panel
        panel.destroyed.connect(lambda obj=None, k=key: self.panels.pop(k, None))
        panel.show()
        panel.activateWindow()

    def close_panels(self):
        for panel in list(self.panels.values()):
            panel.close()
        self.panels.clear()

    def refresh_graph(self):
        with self.bridge.lock:
            fill_table(self.graph_table, [(name, ', '.join(types)) for name, types in self.bridge.graph])

    def start_master(self):
        if self.process.state() != QProcess.NotRunning:
            return
        if self.bridge.mode not in ('disconnected', 'error'):
            QMessageBox.information(self, '先断开连接', '请先断开上位机连接，启动主站后再选择 ROS 2 实机连接。')
            return
        if not self.apply_connection():
            return
        try:
            self.project.export_yaml()
        except ValueError as exc:
            QMessageBox.warning(self, '配置错误', str(exc))
            return
        workspace = Path(self.project.workspace).expanduser().resolve()
        executable = workspace / 'install/soem_wrapper/lib/soem_wrapper/soem_backend'
        if not executable.is_file() or not (workspace / 'install/setup.bash').is_file():
            QMessageBox.warning(self, '主站未构建', f'未找到 {executable}\n请先按 EcatV2_Master 文档完成 colcon build。')
            return
        # A concrete snapshot is the exact file passed to the backend.
        snapshot = Path.home() / '.local/share/ecat-groundstation/runs' / (time.strftime('%Y%m%d_%H%M%S') + f'_{time.time_ns() % 1000000}') / 'config.yaml'
        atomic_write(snapshot, self.project.export_yaml())
        args = [str(executable), '--ros-args', '-p', f'interface:={self.project.interface}', '-p',
                f'config_file:={snapshot}', '-p', f'rt_cpu:={self.project.rt_cpu}', '-p', f'non_rt_cpus:={self.project.non_rt_cpus}']
        env = QProcessEnvironment.systemEnvironment()
        env.insert('ROS_DOMAIN_ID', str(self.project.ros_domain))
        self.process.setProcessEnvironment(env)
        self.process.start('/bin/bash', ['-c', 'source "$1/install/setup.bash" && shift && exec "$@"',
                                         'ecat-master-launch', str(workspace), *args])
        self.master_status.setText(f'正在启动主站 · 配置快照 {snapshot}\n启动成功请依据主站日志确认所有从站进入 OP。')
        self.append_log('启动主站: ' + shlex.join(args))
        self.nav.setCurrentRow(5)

    def stop_master(self):
        self.emergency()
        if self.process.state() != QProcess.NotRunning:
            pid = self.process.processId()
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                return
            self.append_log('已请求本软件启动的主站退出（SIGINT）')
            QTimer.singleShot(5000, self.terminate_master)

    def terminate_master(self):
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            self.append_log('主站未响应 SIGINT，已发送 SIGTERM')

    def master_finished(self, exit_code, exit_status):
        self.master_status.setText(f'本软件启动的主站已退出 · 代码 {exit_code}')
        self.append_log(f'主站进程退出，代码 {exit_code}')
        self.bridge.stop_all('主站已退出，输出锁定')

    def read_process(self):
        data = bytes(self.process.readAllStandardOutput()).decode('utf-8', errors='replace')
        self.log_view.appendPlainText(data.rstrip())

    def append_log(self, text):
        self.log_view.appendPlainText(f'[{time.strftime("%H:%M:%S")}] {text}')

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出运行日志', 'groundstation.log', '日志 (*.log)')
        if path:
            try:
                atomic_write(path, self.log_view.toPlainText())
            except OSError as exc:
                QMessageBox.warning(self, '保存失败', str(exc))

    def poll(self):
        mode = self.bridge.mode
        connected = mode not in ('disconnected', 'error')
        self.mode_combo.setEnabled(not connected)
        self.connect_btn.setText('断开' if connected else '连接')
        self.config_split.setEnabled(not connected and self.process.state() == QProcess.NotRunning)
        self.config_toolbar.setEnabled(not connected and self.process.state() == QProcess.NotRunning)
        self.connection_settings.setEnabled(not connected and self.process.state() == QProcess.NotRunning)
        modes = {'disconnected': '离线编辑 · 配置尚未应用至硬件', 'demo': '离线演示 · 所有数据均为模拟值',
                 'connecting': '正在初始化 ROS 2…', 'ros': f'ROS 2 已连接 · Domain {self.project.ros_domain} · 以反馈确认设备在线',
                 'error': '连接错误 · ' + self.bridge.error}
        status = modes.get(mode, mode)
        self.overview_mode.setText(status)
        self.statusBar().showMessage(status + ('  |  软件停止已锁定' if self.bridge.stopped and connected else ''))
        self.reset_button.setVisible(self.bridge.stopped and connected)
        snapshot = self.bridge.snapshot()
        for row, key in enumerate(self.control_keys):
            ep = snapshot.get(key)
            if ep and connected:
                age = ep['age']
                label = ('演示 · ' if mode == 'demo' else '') + (f'反馈 {age * 1000:.0f} ms' if age < 2 else '无有效反馈')
                if ep['active']:
                    label += ' · 输出中'
                self.control_table.setItem(row, 4, QTableWidgetItem(label))
                self.control_table.setItem(row, 5, QTableWidgetItem(f"{ep['feedback_count']} / {ep['command_count']}"))
            elif self.control_table.item(row, 4):
                self.control_table.item(row, 4).setText('未连接')
        with self.bridge.lock:
            logs = list(self.bridge.logs)
            graph = list(self.bridge.graph)
        if logs:
            if self.log_seen in logs:
                logs = logs[logs.index(self.log_seen) + 1:]
            for stamp, text in logs:
                self.log_view.appendPlainText(f'[{stamp}] {text}')
            if logs:
                self.log_seen = logs[-1]
        if graph != self.last_graph:
            self.refresh_graph()
            self.last_graph = graph

    def closeEvent(self, event):
        if not self.discard_ok():
            event.ignore()
            return
        self.close_panels()
        self.bridge.disconnect()
        if self.process.state() != QProcess.NotRunning:
            self.stop_master()
            if not self.process.waitForFinished(2500):
                self.process.terminate()
                if not self.process.waitForFinished(1500):
                    self.process.kill()
                    self.process.waitForFinished(1000)
        self.timer.stop()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AIM EtherCAT 桌面配置与调试工作台')
    parser.add_argument('--project', help='GroundStation JSON 工程')
    parser.add_argument('--config', help='导入 EcatV2_Master YAML')
    parser.add_argument('--screenshot', help='离屏 UI 截图路径（不连接 ROS、不发送命令）')
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName('Ecat GroundStation')
    app.setOrganizationName('EcatGroundStation')
    app.setFont(QFont('Noto Sans CJK SC', 10))
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    project = None
    if args.project:
        project = load_project(args.project)
    elif args.config:
        project, warnings = Project.import_yaml(Path(args.config).read_text(encoding='utf-8'))
        for warning in warnings:
            print(warning)
    window = MainWindow(project)
    if args.project:
        window.project_path = str(Path(args.project).resolve())
        window.refresh()
    window.show()
    if args.screenshot:
        def capture():
            window.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(400, capture)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
