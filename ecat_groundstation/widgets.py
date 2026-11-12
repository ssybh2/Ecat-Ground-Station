from collections import deque
import csv
from datetime import datetime
import json
import math

from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF
from PyQt5.QtGui import QPainter, QColor, QPen, QPainterPath, QFont
from PyQt5.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QComboBox, QPushButton, QCheckBox, QDoubleSpinBox,
                             QFormLayout, QScrollArea, QSplitter, QTableWidget,
                             QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox)
from .schema import SPECS, resources, message_types
from .commands import fields_for, limits_for, is_enable, neutral_values


def flatten(value, prefix=''):
    result = {}
    for key, item in value.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(flatten(item, name))
        elif isinstance(item, (list, tuple)):
            for i, v in enumerate(item):
                result[f'{name}[{i}]'] = v
        else:
            result[name] = item
    return result


class Scope(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples = deque(maxlen=240)
        self.setMinimumHeight(185)
        self.setMinimumWidth(220)

    def push(self, value):
        if isinstance(value, (int, float)) and math.isfinite(value):
            self.samples.append(value)
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor('#101820'))
        rect = QRectF(55, 15, max(50, self.width() - 70), self.height() - 43)
        p.setPen(QPen(QColor('#233340'), 1))
        for i in range(5):
            y = rect.top() + rect.height() * i / 4
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for i in range(7):
            x = rect.left() + rect.width() * i / 6
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        p.setPen(QColor('#8298a9'))
        p.setFont(QFont('DejaVu Sans', 9))
        if not self.samples:
            p.drawText(rect, Qt.AlignCenter, '等待反馈数据')
            return
        lo, hi = min(self.samples), max(self.samples)
        span = max(hi - lo, abs(hi) * .1, .01)
        lo -= span * .15
        hi += span * .15
        p.drawText(QRectF(0, rect.top() - 8, 50, 20), Qt.AlignRight, f'{hi:.3g}')
        p.drawText(QRectF(0, rect.bottom() - 8, 50, 20), Qt.AlignRight, f'{lo:.3g}')
        p.drawText(QRectF(rect.left(), rect.bottom() + 5, rect.width(), 20), Qt.AlignRight, '最近 12 秒 · 界面采样 20 Hz')
        path = QPainterPath()
        for i, sample in enumerate(self.samples):
            x = rect.left() + i / 239 * rect.width()
            y = rect.bottom() - (sample - lo) / (hi - lo) * rect.height()
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        p.setPen(QPen(QColor('#53d8c1'), 2))
        p.drawPath(path)


class Topology(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.mode = 'disconnected'
        self.setMinimumHeight(230)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor('#101820'))
        if not self.project:
            return
        boxes = [('主站 / PC', self.project.interface)] + [(f'H750 · #{i}', f'SN {s.sn}') for i, s in enumerate(self.project.slaves, 1)]
        box_width = min(185, max(110, (self.width() - 70) / max(len(boxes), 1) - 24))
        gap, left, top = 24, 24, 65
        # Wrap long chains to keep every configured station visible.
        columns = max(1, int((self.width() - 24) / (box_width + gap)))
        for i, (name, subtitle) in enumerate(boxes):
            x = left + (i % columns) * (box_width + gap)
            y = top + (i // columns) * 105
            if i and i % columns:
                p.setPen(QPen(QColor('#4e8d9d'), 2))
                p.drawLine(QPointF(x - gap, y + 32), QPointF(x, y + 32))
            r = QRectF(x, y, box_width, 72)
            p.setPen(QPen(QColor('#436e7b' if i == 0 else '#2c404d'), 1))
            p.setBrush(QColor('#1b2a35' if i == 0 else '#18232c'))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QColor('#e5eef4'))
            p.drawText(r.adjusted(12, 9, -4, -30), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setPen(QColor('#8ba5b5'))
            p.drawText(r.adjusted(12, 34, -4, -7), Qt.AlignLeft | Qt.AlignVCenter, subtitle)
        p.setPen(QColor('#8ba5b5'))
        p.drawText(24, 30, '配置拓扑 · 根据项目绘制，非总线扫描结果')
        self.setMinimumHeight(max(230, top + math.ceil(len(boxes) / columns) * 105))


UNIT_HINTS = {'p_des': 'rad', 'v_des': 'rad/s', 'kp': '0–500', 'kd': '0–5', 'torque': '按电机消息定义',
              'position': 'rad', 'speed': '按电机消息定义', 'angle': '0.01°', 'speed_limit': '°/s',
              'direction': '0/1', 'max_charge_power': 'W', 'allow_charge_power': 'W'}


class CommandPanel(QDialog):
    def __init__(self, key, task, bridge, parent=None):
        super().__init__(parent)
        self.key, self.task, self.bridge = key, task, bridge
        self.setWindowTitle(f'{SPECS[task.type_id].name} — {key}')
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlag(Qt.Window, True)
        self.resize(1020, 700)
        self.editors = {}
        self.records = deque(maxlen=20000)
        self.last_count = -1
        self.init_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(50)

    def init_ui(self):
        layout = QVBoxLayout(self)
        title = QLabel(SPECS[self.task.type_id].name)
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        rtype, wtype = message_types(self.task)
        info = QLabel(f'{self.key}\n反馈: {self.task.values.get("pub_topic", "—")}\n命令: {self.task.values.get("sub_topic", "—")}\n{wtype or rtype}')
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.setObjectName('muted')
        layout.addWidget(info)
        self.mode_label = QLabel('离线演示 · 数据为模拟值' if self.bridge.mode == 'demo' else 'ROS 2 实时连接 · 按住运行以发送命令')
        self.mode_label.setObjectName('notice')
        layout.addWidget(self.mode_label)
        split = QSplitter()
        layout.addWidget(split, 1)
        left = QWidget()
        ll = QVBoxLayout(left)
        if wtype:
            form = QFormLayout()
            for key, typ in fields_for(self.task).items():
                if is_enable(key):
                    continue
                if typ.endswith('[]'):
                    for i in range(self.task.values['sdowrite_channel_num']):
                        self.add_editor(form, key, typ[:-2], i)
                else:
                    self.add_editor(form, key, typ)
            form_widget = QWidget()
            form_widget.setLayout(form)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(form_widget)
            ll.addWidget(scroll, 1)
            self.arm = QCheckBox('允许当前执行器输出（默认禁用）')
            self.arm.toggled.connect(lambda enabled: self.bridge.arm(self.key, enabled))
            ll.addWidget(self.arm)
            self.run = QPushButton('按住运行 · 松开释放')
            self.run.setObjectName('primary')
            self.run.setMinimumHeight(46)
            self.run.released.connect(lambda: self.bridge.release(self.key))
            ll.addWidget(self.run)
            stop = QPushButton('禁用当前执行器')
            stop.clicked.connect(self.disarm)
            ll.addWidget(stop)
            note = QLabel('电机：释放后禁用；LK 广播发送零电流。\nPWM / DSHOT：释放后恢复配置初值。\n软件无法替代硬件急停和固件看门狗。')
            note.setWordWrap(True)
            note.setObjectName('muted')
            ll.addWidget(note)
        else:
            ll.addWidget(QLabel('此任务仅提供反馈监视。'))
        self.state = QLabel('等待数据')
        self.state.setWordWrap(True)
        ll.addWidget(self.state)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.trace = QComboBox()
        self.trace.currentTextChanged.connect(lambda: self.scope.samples.clear())
        rl.addWidget(self.trace)
        self.scope = Scope()
        rl.addWidget(self.scope)
        self.feedback_table = QTableWidget(0, 2)
        self.feedback_table.setHorizontalHeaderLabels(['反馈字段', '当前值'])
        self.feedback_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.feedback_table.setEditTriggers(QTableWidget.NoEditTriggers)
        rl.addWidget(self.feedback_table, 1)
        export = QPushButton('导出当前窗口采样 CSV')
        export.clicked.connect(self.export_csv)
        rl.addWidget(export)
        split.addWidget(right)
        split.setSizes([390, 600])

    def add_editor(self, form, key, typ, index=None):
        lo, hi = limits_for(self.task, key, typ)
        editor = QDoubleSpinBox()
        editor.setDecimals(4 if typ.startswith('float') else 0)
        editor.setRange(lo, hi)
        editor.setSingleStep(0.05 if typ.startswith('float') else 1)
        initial = neutral_values(self.task)[key]
        editor.setValue(initial[index] if index is not None else initial)
        name = f'{key}[{index}]' if index is not None else key
        hint = 'μs' if self.task.type_id in (6, 7) else UNIT_HINTS.get(key, '')
        if self.task.type_id == 12:
            hint = {'speed': 'rad/s', 'torque': 'N·m'}.get(key, hint)
        label = name + (f' · {hint}' if hint else '')
        self.editors[(key, index)] = (editor, typ)
        form.addRow(label, editor)

    def values(self):
        values = neutral_values(self.task)
        for (key, idx), (editor, typ) in self.editors.items():
            value = float(editor.value()) if typ.startswith('float') else int(editor.value())
            if idx is not None:
                values[key][idx] = value
            else:
                values[key] = value
        for key in values:
            if is_enable(key):
                if key.startswith('motor'):
                    slot = key[len('motor'):].split('_')[0]
                    values[key] = int(bool(self.task.values[f'sdowrite_motor{slot}_can_id']))
                else:
                    values[key] = 1
        return values

    def disarm(self):
        if hasattr(self, 'arm'):
            self.arm.setChecked(False)
        self.bridge.release(self.key)

    def poll(self):
        snapshot = self.bridge.snapshot().get(self.key)
        if not snapshot or self.bridge.mode in ('disconnected', 'error'):
            self.disarm()
            self.state.setText('已断开 / ' + self.bridge.error)
            return
        if hasattr(self, 'arm'):
            if self.arm.isChecked() and not snapshot['armed']:
                self.arm.setChecked(False)
            if self.run.isDown() and self.arm.isChecked() and self.isActiveWindow():
                try:
                    if not self.bridge.renew(self.key, self.values()):
                        self.disarm()
                except ValueError as exc:
                    self.disarm()
                    self.state.setText(str(exc))
                    return
            elif not self.isActiveWindow():
                self.disarm()
            elif not self.run.isDown():
                self.bridge.release(self.key)
        age = '—' if not math.isfinite(snapshot['age']) else f"{snapshot['age'] * 1000:.0f} ms"
        self.state.setText(f"{'● 输出中' if snapshot['active'] else snapshot['reason'] or '待机'}\n"
                           f"反馈年龄 {age} · 匹配订阅者 {snapshot['receivers']}\n"
                           f"接收 {snapshot['feedback_count']} · 发送 {snapshot['command_count']}")
        if snapshot['feedback_count'] == self.last_count:
            return
        self.last_count = snapshot['feedback_count']
        data = flatten(snapshot['feedback'])
        numeric = [key for key, val in data.items() if isinstance(val, (int, float)) and not key.startswith('header.')]
        if numeric and self.trace.count() == 0:
            self.trace.addItems(numeric)
            preferred = next((k for k in ('position', 'velocity', 'motor1_rpm', 'motor1_speed', 'temperature') if k in numeric), numeric[0])
            self.trace.setCurrentText(preferred)
        if self.trace.currentText() in data:
            self.scope.push(data[self.trace.currentText()])
        self.feedback_table.setRowCount(len(data))
        for row, (key, value) in enumerate(data.items()):
            self.feedback_table.setItem(row, 0, QTableWidgetItem(key))
            self.feedback_table.setItem(row, 1, QTableWidgetItem(f'{value:.5g}' if isinstance(value, float) else str(value)))
        if data:
            self.records.append({'timestamp': datetime.now().isoformat(timespec='milliseconds'), **data})

    def export_csv(self):
        if not self.records:
            QMessageBox.information(self, '采样', '暂无反馈数据。')
            return
        self.disarm()
        path, _ = QFileDialog.getSaveFileName(self, '保存界面采样', self.key.replace('/', '_') + '.csv', 'CSV (*.csv)')
        if path:
            try:
                keys = list(dict.fromkeys(k for record in self.records for k in record))
                with open(path, 'w', newline='', encoding='utf-8-sig') as out:
                    writer = csv.DictWriter(out, fieldnames=keys)
                    writer.writeheader()
                    writer.writerows(self.records)
            except OSError as exc:
                QMessageBox.warning(self, '导出失败', str(exc))

    def changeEvent(self, event):
        if event.type() == event.ActivationChange and not self.isActiveWindow() and hasattr(self, 'arm'):
            self.disarm()
        super().changeEvent(event)

    def closeEvent(self, event):
        self.disarm()
        self.timer.stop()
        super().closeEvent(event)
