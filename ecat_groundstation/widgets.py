from collections import deque
import csv
from datetime import datetime
import json
import math
import time

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
        self.series = {}
        self.empty_text = '等待反馈数据'
        self.colors = ('#53d8c1', '#f4b663', '#78aaff', '#df8ef3')
        self.setMinimumHeight(185)
        self.setMinimumWidth(220)

    def clear(self):
        self.samples.clear()
        self.series.clear()
        self.update()

    def push(self, value, timestamp=None):
        timestamp = time.monotonic() if timestamp is None else timestamp
        values = value if isinstance(value, dict) else {'value': value}
        for name, sample in values.items():
            if isinstance(sample, (int, float)) and math.isfinite(sample):
                self.series.setdefault(name, deque(maxlen=240)).append((timestamp, sample))
        if values:
            first = next(iter(values.values()))
            if isinstance(first, (int, float)) and math.isfinite(first):
                self.samples.append(first)
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
        if not any(self.series.values()):
            p.drawText(rect, Qt.AlignCenter, self.empty_text)
            return
        latest = max(points[-1][0] for points in self.series.values() if points)
        visible = [v for points in self.series.values() for stamp, v in points if stamp >= latest - 12]
        lo, hi = min(visible), max(visible)
        span = max(hi - lo, abs(hi) * .1, .01)
        lo -= span * .15
        hi += span * .15
        p.drawText(QRectF(0, rect.top() - 8, 50, 20), Qt.AlignRight, f'{hi:.3g}')
        p.drawText(QRectF(0, rect.bottom() - 8, 50, 20), Qt.AlignRight, f'{lo:.3g}')
        p.drawText(QRectF(rect.left(), rect.bottom() + 5, rect.width(), 20), Qt.AlignRight, '接收时间 · −12 s → 0 s · 界面采样最多 20 Hz')
        for index, (name, points) in enumerate(self.series.items()):
            path = QPainterPath()
            first = True
            for stamp, sample in points:
                if stamp < latest - 12:
                    continue
                x = rect.left() + (stamp - latest + 12) / 12 * rect.width()
                y = rect.bottom() - (sample - lo) / (hi - lo) * rect.height()
                path.moveTo(x, y) if first else path.lineTo(x, y)
                first = False
            color = QColor(self.colors[index % len(self.colors)])
            p.setPen(QPen(color, 2))
            p.drawPath(path)
            if points:
                p.drawText(QRectF(rect.left() + 8, rect.top() + index * 18, rect.width() - 12, 18),
                           Qt.AlignLeft, f'{name}  {points[-1][1]:.4g}')


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
        self.resize(1100, 820)
        self.editors = {}
        self.records = deque(maxlen=20000)
        self.last_count = -1
        self.publish_until = 0
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
        self.mode_label = QLabel('离线演示 · 数据为模拟值' if self.bridge.mode == 'demo' else
                                ('ROS 2 · 编辑数值不会发送；允许输出后点击「发布（定时）」或按住运行' if wtype else
                                 'ROS 2 反馈监视 · 按接收时间绘图；可选择字段或 IMU 多轴曲线'))
        self.mode_label.setWordWrap(True)
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
            scroll.setMinimumHeight(180)
            ll.addWidget(scroll, 1)
            self.arm = QCheckBox('允许当前执行器输出（默认禁用）')
            self.arm.setObjectName('outputPermission')
            self.arm.setMinimumHeight(54)
            self.arm.setCursor(Qt.PointingHandCursor)
            self.arm.setToolTip('勾选此处后，才可通过发布按钮或按住运行发送指令。')
            self.arm.toggled.connect(lambda enabled: self.bridge.arm(self.key, enabled))
            ll.addWidget(self.arm)
            publish_row = QHBoxLayout()
            self.publish_duration = QDoubleSpinBox()
            self.publish_duration.setRange(.1, 10.)
            self.publish_duration.setValue(1.)
            self.publish_duration.setSuffix(' s')
            self.publish_duration.setSingleStep(.1)
            publish_row.addWidget(self.publish_duration)
            self.publish = QPushButton('发布（定时）')
            self.publish.setAutoDefault(False)
            self.publish.setObjectName('primary')
            self.publish.clicked.connect(self.begin_publish)
            publish_row.addWidget(self.publish)
            ll.addLayout(publish_row)
            self.command_preview = QLabel()
            self.command_preview.setWordWrap(True)
            self.command_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.command_preview.setObjectName('muted')
            ll.addWidget(self.command_preview)
            for editor, _ in self.editors.values():
                editor.valueChanged.connect(self.update_command_preview)
            self.update_command_preview()
            self.run = QPushButton('按住运行 · 松开释放')
            self.run.setObjectName('primary')
            self.run.setMinimumHeight(46)
            self.run.released.connect(self.stop_publish)
            ll.addWidget(self.run)
            stop = QPushButton('禁用当前执行器')
            stop.clicked.connect(self.disarm)
            ll.addWidget(stop)
            note = QLabel('20 Hz 定时发布；到时、失焦或禁用后释放。\nPWM / DSHOT 恢复初值；电机禁用，LK 广播零电流。\n发送成功不代表硬件已执行。')
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
        if wtype and not rtype:
            rl.addWidget(QLabel('已发布命令（此任务没有实物反馈）'))
        self.trace = QComboBox()
        rl.addWidget(self.trace)
        self.scope = Scope()
        if not rtype:
            self.scope.empty_text = '尚未发布命令'
        self.trace.currentTextChanged.connect(self.scope.clear)
        if self.task.type_id == 3:
            self.trace.addItem('角速度 XYZ · rad/s', ['angular_velocity.' + axis for axis in 'xyz'])
            self.trace.addItem('加速度 XYZ · m/s²', ['linear_acceleration.' + axis for axis in 'xyz'])
            self.trace.addItem('四元数 WXYZ', ['orientation.' + axis for axis in 'wxyz'])
        elif self.task.type_id in (4, 6):
            self.trace.addItem('已发布四通道指令', ['channel' + str(i) for i in range(1, 5)])
        rl.addWidget(self.scope)
        self.feedback_table = QTableWidget(0, 2)
        self.feedback_table.setHorizontalHeaderLabels(['反馈字段' if rtype else '已发送字段', '当前值'])
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
        self.publish_until = 0
        if hasattr(self, 'arm'):
            self.arm.setChecked(False)
        self.bridge.release(self.key)

    def update_command_preview(self):
        self.command_preview.setText('将发布：' + json.dumps(self.values(), ensure_ascii=False))

    def begin_publish(self):
        if not self.arm.isChecked():
            self.state.setText('尚未发送：请先勾选「允许当前执行器输出」。')
            return
        if not self.isActiveWindow():
            return
        try:
            if self.bridge.renew(self.key, self.values()):
                self.publish_until = time.monotonic() + self.publish_duration.value()
            else:
                self.disarm()
        except ValueError as exc:
            self.disarm()
            self.state.setText(str(exc))

    def stop_publish(self):
        self.publish_until = 0
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
            publishing = self.publish_until > time.monotonic()
            self.publish.setText(f'发布中 · {max(0, self.publish_until - time.monotonic()):.1f} s' if publishing else '发布（定时）')
            if (self.run.isDown() or publishing) and self.arm.isChecked() and self.isActiveWindow():
                try:
                    if not self.bridge.renew(self.key, self.values()):
                        self.disarm()
                except ValueError as exc:
                    self.disarm()
                    self.state.setText(str(exc))
                    return
            elif not self.isActiveWindow():
                self.disarm()
            elif not self.run.isDown() and not publishing:
                self.bridge.release(self.key)
        age = '—' if not math.isfinite(snapshot['age']) else f"{snapshot['age'] * 1000:.0f} ms"
        read = bool(message_types(self.task)[0])
        statistics = (f"反馈年龄 {age} · {snapshot.get('feedback_hz', 0):.1f} Hz · 发布者 {snapshot.get('feedback_publishers', 0)}"
                      if read else f"命令匹配订阅者 {snapshot['receivers']} · 无实物反馈")
        self.state.setText(f"{'● 消息发布中' if snapshot['active'] else snapshot['reason'] or '待机'}\n"
                           f"{snapshot.get('link_status', '')}\n"
                           f"{statistics}\n"
                           f"接收 {snapshot['feedback_count']} · 发送 {snapshot['command_count']}")
        count = snapshot['feedback_count'] if read else snapshot['command_count']
        if count == self.last_count:
            return
        self.last_count = count
        data = flatten(snapshot['feedback'] if read else snapshot.get('last_command', {}))
        numeric = [key for key, val in data.items() if isinstance(val, (int, float)) and not key.startswith('header.')]
        existing = {self.trace.itemText(i) for i in range(self.trace.count())}
        for key in numeric:
            if key not in existing:
                self.trace.addItem(key)
        if numeric and not existing:
            preferred = next((k for k in ('position', 'velocity', 'motor1_rpm', 'motor1_speed', 'temperature') if k in numeric), numeric[0])
            self.trace.setCurrentText(preferred)
        names = self.trace.currentData() or [self.trace.currentText()]
        selected = {name: data[name] for name in names if name in data}
        if selected:
            self.scope.push(selected, snapshot.get('feedback_at' if read else 'command_at') or time.monotonic())
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
