"""Qt first-run workflow. Jobs only run after the corresponding button click."""
from __future__ import annotations

import codecs
import copy
import json
from pathlib import Path
import re
import shutil
import sys
import time

from PyQt5.QtCore import QObject, QProcess, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFont
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox, QPlainTextEdit,
    QProgressBar, QTabWidget, QScrollArea, QFileDialog, QMessageBox, QCheckBox,
)
from .setup_core import (
    STATE_VERSION, load_state, atomic_json, compact_cpus, cpu_list,
    network_snapshot, changed_interfaces, detect_stlinks, validate_imu_ids,
    flash_ranges, sha256_file, parse_slave_sns, workspace_path, IsolationPlan,
    verify_isolation, read_text, verify_workspace_install,
)
from .config import Project, Slave, Task


PRIVILEGED = {'enable-rt', 'isolate-cpu', 'flash-eeprom', 'diagnose-eeprom', 'probe-mcu', 'flash-mcu', 'install-tools', 'repair-workspace-access'}
JOB_LABELS = {'inspect': '检查当前电脑', 'enable-rt': '启用实时内核', 'isolate-cpu': 'CPU 核心隔离',
              'create-workspace': '创建工作空间', 'flash-eeprom': '刷写 EEPROM', 'probe-mcu': '检测 MCU',
              'flash-mcu': '烧录与回读校验', 'prepare-imu': '准备 IMU 源码', 'compile-imu': '编译 IMU',
              'install-tools': '安装所选工具的必要依赖', 'create-bringup': '生成 bringup 包', 'build-workspace': 'colcon build',
              'prepare-eeprom-tool': '准备逐字校验工具', 'diagnose-eeprom': 'EEPROM 只读诊断',
              'repair-workspace-access': '修复工作空间写入权限'}


def action_button(text, slot, primary=False):
    button = QPushButton(text)
    if primary:
        button.setObjectName('primary')
    button.clicked.connect(slot)
    return button


def note(text, important=False):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName('notice' if important else 'muted')
    label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    label.setOpenExternalLinks(True)
    return label


class SetupJobs(QObject):
    output = pyqtSignal(str)
    done = pyqtSignal(str, bool, dict)
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None, log_dir=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.failed_to_start)
        self.action = ''
        self.buffer = ''
        self.data = {}
        self.decoder = None
        self.busy = False
        self.cancelled = False
        self.log_dir = Path(log_dir) if log_dir else Path.home() / '.local/share/ecat-groundstation/logs'
        self.log_stream = None
        self.log_path = None

    @staticmethod
    def command(action, arguments):
        command = ['/usr/bin/python3', '-I', '-u', str(Path(__file__).with_name('setup_worker.py')), action, *arguments]
        if action in PRIVILEGED:
            if not shutil.which('pkexec'):
                raise ValueError('缺少 pkexec。请安装 policykit-1 并确保桌面认证代理正在运行。')
            command.insert(0, 'pkexec')
        return command

    def start(self, action, arguments=()):
        if self.busy:
            raise RuntimeError('首次配置中已有任务正在执行，请等待完成')
        command = self.command(action, list(map(str, arguments)))
        self.action, self.buffer, self.data = action, '', {}
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.cancelled = False
        self.log_path = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = self.log_dir / f'{time.time_ns()}-{action}.log'
            self.log_stream = self.log_path.open('x', encoding='utf-8')
            self.log_path.chmod(0o600)
        except OSError as exc:
            self.log_stream = None
            self.output.emit(f'无法保存任务日志：{exc}')
        self.busy = True
        self.busy_changed.emit(True)
        self.handle_line(f'开始：{JOB_LABELS.get(action, action)}' + (' · 请在系统授权窗口输入管理员密码' if action in PRIVILEGED else ''))
        if self.log_path:
            self.output.emit(f'日志自动保存：{self.log_path}')
        self.process.start(command[0], command[1:])

    def read_output(self):
        if self.decoder is None:
            return
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self.handle_line(line)

    def handle_line(self, line):
        if self.log_stream:
            try:
                self.log_stream.write(line + '\n')
                self.log_stream.flush()
            except OSError:
                self.log_stream.close()
                self.log_stream = None
                self.output.emit('任务日志保存失败，请手动保存界面日志')
        prefix = '@@ECAT_RESULT@@'
        if line.startswith(prefix):
            try:
                self.data = json.loads(line[len(prefix):])
            except ValueError:
                self.output.emit('无法读取任务返回数据')
        elif line.startswith('@@ECAT_ERROR@@'):
            try:
                self.data = json.loads(line[len('@@ECAT_ERROR@@'):])
                self.data['_failed'] = True
                self.output.emit(self.data.get('message', '任务失败'))
            except ValueError:
                self.output.emit(line)
        else:
            self.output.emit(line)

    def finished(self, code, status):
        if not self.busy:
            return
        self.read_output()
        if self.buffer:
            self.handle_line(self.buffer)
        self.buffer = ''
        action = self.action
        success = code == 0 and status == QProcess.NormalExit and bool(self.data) and not self.cancelled and not self.data.get('_failed')
        if not success and not self.data:
            self.data = {'message': '任务已取消' if self.cancelled else f'任务失败（退出码 {code}），请查看下方日志。管理员授权取消也会结束任务。'}
        self.busy = False
        if self.log_stream:
            self.log_stream.close()
            self.log_stream = None
        if self.log_path:
            self.data['log_path'] = str(self.log_path)
        self.busy_changed.emit(False)
        self.done.emit(action, success, dict(self.data))

    def failed_to_start(self, error):
        if error == QProcess.FailedToStart:
            self.output.emit(self.process.errorString())
            self.finished(-1, QProcess.CrashExit)

    def cancel(self):
        if self.busy and self.action not in PRIVILEGED:
            self.cancelled = True
            self.process.terminate()


class FirmwarePane(QWidget):
    def __init__(self, owner, target='h750', image='', expected_hash='', description=''):
        super().__init__()
        self.owner, self.target = owner, target
        self.expected_hash = expected_hash
        self.recorded_image = image
        layout = QVBoxLayout(self)
        layout.addWidget(note(description or ('H750 · 400 kHz · init → halt → Flash 擦写 → 回读校验；不自动复位。' if target == 'h750'
                                             else 'STM32G431 IMU · 1000 kHz · reset halt → Flash 擦写 → 回读校验 → reset run。'), True))
        form = QFormLayout()
        image_row = QHBoxLayout()
        self.image = QLineEdit(image)
        image_row.addWidget(self.image, 1)
        image_row.addWidget(action_button('选择固件…', self.browse))
        form.addRow('固件路径', image_row)
        self.base = QLineEdit('0x08000000')
        form.addRow('BIN 加载地址', self.base)
        self.speed = QSpinBox()
        self.speed.setRange(50, 4000)
        self.speed.setValue(400 if target == 'h750' else 1000)
        form.addRow('SWD 时钟 / kHz', self.speed)
        self.link = QComboBox()
        form.addRow('ST-Link', self.link)
        layout.addLayout(form)
        self.link_status = note('')
        layout.addWidget(self.link_status)
        row = QHBoxLayout()
        row.addWidget(action_button('安装 OpenOCD', lambda: self.owner.start_job('install-tools', ['--tool-set', 'flash'])))
        row.addWidget(action_button('检测 ST-Link', self.refresh_links))
        row.addWidget(action_button('检测 MCU / halt', lambda: self.execute(False)))
        row.addWidget(action_button('烧录并校验', lambda: self.execute(True), True))
        layout.addLayout(row)
        layout.addWidget(note('支持 ARM ELF32、Intel HEX 和 BIN。ELF/HEX 自带地址；仅 BIN 使用上方加载地址。\n'
                              '烧录完成后请在首次配置的执行日志查看结果。H750 请按 RESET 或重新上电；校验成功不代表应用功能验收。'))
        layout.addStretch()
        self.owner.jobs.busy_changed.connect(self.setDisabled)
        self.refresh_links()

    def refresh_links(self):
        previous = self.link.currentData()
        self.link.clear()
        links = detect_stlinks()
        if len(links) != 1:
            self.link.addItem('请选择 ST-Link' if links else '未连接 ST-Link', None)
        for item in links:
            self.link.addItem(f"{item['product']} · {item['serial'] or item['path']}", item['serial'])
        if previous is not None and self.link.findData(previous) >= 0:
            self.link.setCurrentIndex(self.link.findData(previous))
        self.link_status.setText(f'已检测到 {len(links)} 个 ST-Link。' if links else '未检测到 ST-Link，请连接 ST-Link USB、SWD 与目标板逻辑供电。')

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 MCU 固件', str(Path.home()), '固件 (*.elf *.hex *.bin)')
        if path:
            self.image.setText(path)

    def execute(self, write):
        if not shutil.which('openocd'):
            QMessageBox.information(self, '缺少烧录工具', '请点击本窗口的「安装 OpenOCD」，无需安装 IMU 编译工具链。')
            return
        self.refresh_links()
        serial = self.link.currentData()
        if serial is None:
            QMessageBox.information(self, 'ST-Link', '请连接并选择用于本次操作的 ST-Link。')
            return
        args = ['--target', self.target, '--serial', serial, '--speed', str(self.speed.value())]
        if write:
            try:
                image = Path(self.image.text().strip()).expanduser().resolve()
                flash_ranges(image, int(self.base.text().strip(), 0))
                expected = self.expected_hash if str(image) == self.recorded_image else ''
                if expected and sha256_file(image) != expected:
                    raise ValueError('所选固件已变化，请重新编译后再烧录')
                text = f'目标：{self.target.upper()}\nST-Link：{serial or "唯一设备"}\n固件：{image}\n\n将擦写对应 Flash 扇区。请确认板子与固件匹配，执行器动力已断开、逻辑供电正常。'
                if QMessageBox.question(self, '确认烧录', text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
                args += ['--image', str(image), '--base', self.base.text().strip()]
                if expected:
                    args += ['--expected-hash', expected]
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, '固件检查失败', str(exc))
                return
        self.owner.start_job('flash-mcu' if write else 'probe-mcu', args)


class FirstSetupPage(QWidget):
    configured = pyqtSignal(object)
    workspace_built = pyqtSignal(bool, object)

    def __init__(self, parent=None, allow_action=None, state_path=None, project=None):
        super().__init__(parent)
        self.allow_action = allow_action or (lambda: True)
        self.project = project
        self.state_path = Path(state_path) if state_path else Path.home() / '.local/share/ecat-groundstation/first-setup.json'
        self.state = load_state(self.state_path)
        self.inspection = {}
        self.current_plan = self.state.get('plan')
        self.dialogs = []
        self.started_at = 0
        self.after_inspection = ''
        self.pending_eeprom = None
        self.build_auto_terminal = None
        self.jobs = SetupJobs(self)
        self.jobs.output.connect(self.append_log)
        self.jobs.done.connect(self.job_finished)
        self.jobs.busy_changed.connect(self.job_busy)
        self.build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

    @property
    def busy(self):
        return self.jobs.busy

    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 20)
        title = QLabel('首次配置')
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        layout.addWidget(note('依次准备实时内核、CPU 隔离、工作空间与板卡，最后发现 SN 并进入任务配置。所有操作由按钮触发。'))
        self.summary = note('点击「检查当前电脑」开始；步骤与编译产物会保存，重启后可继续。', True)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.build_system_tab()
        self.build_workspace_tab()
        self.build_h750_tab()
        self.build_imu_tab()
        self.build_bringup_tab()
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(160)
        progress_row.addWidget(self.progress)
        self.job_status = QLabel('等待操作')
        progress_row.addWidget(self.job_status, 1)
        self.cancel_button = action_button('取消当前任务', self.jobs.cancel)
        self.cancel_button.setEnabled(False)
        progress_row.addWidget(self.cancel_button)
        progress_row.addWidget(action_button('保存日志', self.save_log))
        layout.addLayout(progress_row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFont('DejaVu Sans Mono', 10))
        self.log.setMinimumHeight(100)
        self.log.setMaximumHeight(150)
        layout.addWidget(self.log)

    def tab(self, name):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, name)
        return layout

    def build_system_tab(self):
        layout = self.tab('1  系统与 CPU')
        row = QHBoxLayout()
        row.addWidget(action_button('检查当前电脑', self.check_system, True))
        row.addWidget(action_button('realtime-kernel', self.enable_rt))
        row.addWidget(action_button('Ubuntu Pro 绑定教程', self.pro_guide))
        row.addWidget(action_button('CPU核心隔离', self.isolate_cpu))
        row.addStretch()
        layout.addLayout(row)
        self.system_info = note('尚未检查。实时内核是否运行以 /sys/kernel/realtime 为准；安装成功后需重启。')
        layout.addWidget(self.system_info)
        self.cpu_info = note('隔离将包括同一物理核心的全部超线程，并保留其余核心给系统。', True)
        layout.addWidget(self.cpu_info)
        self.grub_preview = QPlainTextEdit()
        self.grub_preview.setReadOnly(True)
        self.grub_preview.setMaximumHeight(90)
        layout.addWidget(self.grub_preview)
        self.cpu_topology = QPlainTextEdit()
        self.cpu_topology.setReadOnly(True)
        self.cpu_topology.setMaximumHeight(130)
        layout.addWidget(self.cpu_topology)
        layout.addWidget(note('隔离参数会备份后写入本软件管理的 GRUB 配置片段，并执行 update-grub。重启后重新检查实际生效状态。\n'
                              '已有完整隔离会优先沿用；首次选择时保留 CPU0 给系统。混合架构按教程优先选基础频率 >2 GHz 的 E 核，否则选 P 核。'))
        layout.addWidget(note('BIOS 中的 C-State、虚拟化、频率锁定和 Turbo 等项目需按主板手册自行设置，软件不会修改 BIOS。'))
        layout.addStretch()

    def build_workspace_tab(self):
        layout = self.tab('2  工作空间与 EEPROM')
        form = QFormLayout()
        self.workspace_name = QLineEdit(self.state.get('workspace_name', 'EGC_ws'))
        form.addRow('Home 下的工作空间名称', self.workspace_name)
        self.workspace_edit = QLineEdit(self.state.get('workspace', str(Path.home() / 'EGC_ws')))
        self.workspace_edit.setReadOnly(True)
        form.addRow('工作空间路径', self.workspace_edit)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(action_button('确定 · 创建工作空间', self.create_workspace, True))
        row.addWidget(action_button('选择已有工作空间…', self.choose_workspace))
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(note('将自动创建 src、git init、添加 EcatV2_Master 子模块并递归拉取 SOEM。工具属于普通用户，直接设置执行权限，无需保持 root 身份。'))
        row = QHBoxLayout()
        row.addWidget(action_button('网口检测', self.detect_network, True))
        self.interface = QComboBox()
        self.interface.setEditable(True)
        self.interface.setCurrentText(self.state.get('interface', ''))
        row.addWidget(self.interface, 1)
        row.addWidget(QLabel('从站链位置'))
        self.slave_index = QSpinBox()
        self.slave_index.setRange(1, 255)
        self.slave_index.setValue(int(self.state.get('eeprom_slave', 1)))
        row.addWidget(self.slave_index)
        layout.addLayout(row)
        self.network_info = note('接好 Type-C 与网线检测一次，再拔掉网线检测一次；依据 carrier / LOWER_UP 变化识别有线网口。')
        layout.addWidget(self.network_info)
        self.eeprom_kind = QComboBox()
        self.eeprom_kind.addItem('H750 标准版 · 80 / 80 B', '58100H750_UniversalModule.bin')
        self.eeprom_kind.addItem('H750 Large PDO · 112 / 80 B', '58100H750_UniversalModule_LargePDOV.bin')
        self.eeprom_kind.setCurrentIndex(1 if self.state.get('model') == 'large' else 0)
        self.eeprom_kind.currentIndexChanged.connect(self.update_eeprom_path)
        form2 = QFormLayout()
        form2.addRow('EEPROM 类型', self.eeprom_kind)
        image_row = QHBoxLayout()
        self.eeprom_image = QLineEdit()
        image_row.addWidget(self.eeprom_image, 1)
        image_row.addWidget(action_button('自选 BIN…', self.choose_eeprom))
        form2.addRow('EEPROM 镜像', image_row)
        layout.addLayout(form2)
        flash_row = QHBoxLayout()
        flash_row.addWidget(action_button('刷写eeprom', self.write_eeprom, True))
        flash_row.addWidget(action_button('只读诊断 / 对比镜像', lambda: self.prepare_eeprom('diagnose-eeprom')))
        layout.addLayout(flash_row)
        layout.addWidget(note('写入前自动备份；只补写不一致的16位字，每字回读校验并最多重试8次，最后两次全量读取确认。完成后需要断电重上电。\n'
                              '链位置 1 指直接连接主站网口的第一块 EtherCAT 板；开始前停止其他 EtherCAT 主站。'))
        layout.addStretch()
        self.update_eeprom_path()
        saved_image = self.state.get('eeprom_image', '')
        if saved_image and Path(saved_image).is_file():
            self.eeprom_image.setText(saved_image)

    def build_h750_tab(self):
        layout = self.tab('3  H750 固件')
        layout.addWidget(action_button('打开独立 H750 烧录窗口', lambda: self.open_firmware('h750'), True))
        self.h750 = FirmwarePane(self, 'h750')
        layout.addWidget(self.h750)

    def build_imu_tab(self):
        layout = self.tab('4  IMU 固件与 ID')
        layout.addWidget(note('为同一 CAN 口的三颗 IMU 分别生成独立固件。每颗使用三个不重复 ID，默认 123 / 456 / 789；编译不会修改原始源码。', True))
        form = QFormLayout()
        self.imu_repo = QLineEdit(self.state.get('imu_repo', str(Path.home() / '.local/share/ecat-groundstation/firmware/hipnucimu')))
        repo_row = QHBoxLayout()
        repo_row.addWidget(self.imu_repo, 1)
        repo_row.addWidget(action_button('选择源码…', self.choose_imu_repo))
        form.addRow('hipnucimu 源码目录', repo_row)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(action_button('获取 / 检查 IMU 源码', self.prepare_imu, True))
        row.addWidget(action_button('安装 IMU 编译依赖', lambda: self.start_job('install-tools', ['--tool-set', 'imu'])))
        row.addWidget(action_button('自选 IMU 固件烧录…', lambda: self.open_firmware('g431')))
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(note('仅安装缺失的 C 编译依赖，不安装大型 C++ 库；OpenOCD 可在烧录窗口单独安装。'))
        grid = QGridLayout()
        for c, text in enumerate(('IMU', 'PACKET1 CAN ID', 'PACKET2 CAN ID', 'PACKET3 CAN ID', '编译', '烧录')):
            grid.addWidget(QLabel(text), 0, c)
        self.id_editors, self.artifact_labels = [], []
        saved = self.state.get('imu_profiles', [[1, 2, 3], [4, 5, 6], [7, 8, 9]])
        if not isinstance(saved, list) or len(saved) != 3:
            saved = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        for i in range(3):
            grid.addWidget(QLabel(f'IMU {i + 1}'), i * 2 + 1, 0)
            editors = []
            for j in range(3):
                edit = QLineEdit(f'0x{saved[i][j]:03X}')
                edit.setToolTip('支持 0x 十六进制或十进制；范围 1–2047')
                edit.editingFinished.connect(self.persist_profiles)
                grid.addWidget(edit, i * 2 + 1, j + 1)
                editors.append(edit)
            self.id_editors.append(editors)
            grid.addWidget(action_button(f'编译 IMU {i + 1}', lambda checked=False, n=i + 1: self.compile_imu(n), True), i * 2 + 1, 4)
            grid.addWidget(action_button(f'烧录 IMU {i + 1}', lambda checked=False, n=i + 1: self.flash_profile(n)), i * 2 + 1, 5)
            label = note('尚未编译')
            self.artifact_labels.append(label)
            grid.addWidget(label, i * 2 + 2, 1, 1, 5)
        layout.addLayout(grid)
        layout.addWidget(note('每次编译使用独立源码快照与独立 build 目录，记录 ID、ELF 路径和 SHA256。修改 ID 后，旧固件会标为不匹配。\n'
                              '逐块连接并烧录 IMU 板；先确认 ST-Link 对应哪一块板，再点击该 IMU 的烧录按钮。'))
        layout.addStretch()
        self.refresh_artifacts()

    def build_bringup_tab(self):
        layout = self.tab('5  Bringup 与首次运行')
        form = QFormLayout()
        self.ros_setup = QComboBox()
        available = sorted(Path('/opt/ros').glob('*/setup.bash'))
        for path in available:
            self.ros_setup.addItem(path.parent.name, str(path))
        saved = self.state.get('ros_setup', '')
        if self.ros_setup.findData(saved) >= 0:
            self.ros_setup.setCurrentIndex(self.ros_setup.findData(saved))
        form.addRow('ROS 2 发行版', self.ros_setup)
        self.launch_preview = note('从上一步的网口与 CPU 检查自动生成 interface、rt_cpu、non_rt_cpus。')
        layout.addLayout(form)
        layout.addWidget(self.launch_preview)
        row = QHBoxLayout()
        row.addWidget(action_button('Creat bringup package', self.create_bringup, True))
        row.addWidget(action_button('colcon build', self.build_workspace, True))
        row.addWidget(action_button('打开首次运行 root 终端', self.open_root_terminal))
        layout.addLayout(row)
        self.auto_terminal = QCheckBox('编译成功后自动打开已加载工作空间的 root 终端')
        self.auto_terminal.setChecked(True)
        layout.addWidget(self.auto_terminal)
        layout.addWidget(note('生成教程要求的 config/config.yaml 与 launch/bringup.launch.py，并自动补齐 CMake 安装规则。\n'
                              'root 终端中需由你自行执行：ros2 launch soem_bringup bringup.launch.py\n'
                              '首次示例仅用于读出真实 SN，取得 SN 后请使用下方入口生成完整任务配置。', True))
        self.sn_log = QPlainTextEdit()
        self.sn_log.setPlaceholderText('可粘贴 Found slave id=1, sn=... 的日志，然后点「提取 SN」')
        self.sn_log.setMaximumHeight(90)
        layout.addWidget(self.sn_log)
        row = QHBoxLayout()
        row.addWidget(action_button('提取 SN', self.extract_sn))
        self.real_sn = QLineEdit()
        self.real_sn.setPlaceholderText('输入或提取真实 SN')
        row.addWidget(self.real_sn)
        row.addWidget(action_button('进入从站任务配置 →', self.adopt_setup, True))
        layout.addLayout(row)
        layout.addWidget(note('系统认证窗口负责管理员密码；软件不保存密码。普通用户执行 Git、源码准备和 colcon，只有系统配置与烧录才提权。'))
        layout.addStretch()

    def append_log(self, text):
        self.log.appendPlainText(text.rstrip())

    def persist(self):
        self.state['version'] = STATE_VERSION
        try:
            atomic_json(self.state_path, self.state)
        except OSError as exc:
            self.append_log(f'首次配置状态保存失败（重启后可能无法恢复步骤）：{exc}')

    def start_job(self, action, arguments=()):
        if action != 'inspect' and not self.allow_action():
            return False
        try:
            self.jobs.start(action, arguments)
            self.started_at = time.monotonic()
            return True
        except (RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, '无法开始操作', str(exc))
            return False

    def job_busy(self, busy):
        self.tabs.setDisabled(busy)
        self.progress.setRange(0, 0 if busy else 1)
        self.progress.setValue(0)
        self.cancel_button.setEnabled(busy and self.jobs.action not in PRIVILEGED)

    def tick(self):
        if self.busy:
            self.job_status.setText(f'{JOB_LABELS.get(self.jobs.action, self.jobs.action)} · 已执行 {int(time.monotonic() - self.started_at)} 秒')

    def check_system(self):
        self.start_job('inspect')

    def job_finished(self, action, success, data):
        message = data.get('message', '')
        self.job_status.setText(('完成' if success else '失败') + f' · {JOB_LABELS.get(action, action)}')
        if message:
            self.append_log(message)
        if not success:
            self.after_inspection = ''
            self.pending_eeprom = None
            self.summary.setText(message)
            if action == 'build-workspace':
                self.workspace_built.emit(False, data)
                self.build_auto_terminal = None
            return
        if action == 'prepare-eeprom-tool' and self.pending_eeprom:
            pending, self.pending_eeprom = self.pending_eeprom, None
            self.start_job(*pending)
            return
        if action == 'inspect':
            self.display_inspection(data)
        elif action == 'create-workspace':
            self.workspace_edit.setText(data['workspace'])
            self.state.update(workspace=data['workspace'], workspace_name=self.workspace_name.text().strip())
            self.update_eeprom_path()
            QMessageBox.information(self, '工作空间就绪', message)
        elif action == 'isolate-cpu':
            self.current_plan = data['plan']
            self.state['plan'] = self.current_plan
            self.display_plan()
        elif action == 'compile-imu':
            self.state.setdefault('imu_artifacts', {})[str(data['profile'])] = data
            self.refresh_artifacts()
        if action in ('enable-rt', 'isolate-cpu'):
            if data.get('reboot_required'):
                self.state['reboot_pending'] = {'action': action, 'boot_id': read_text('/proc/sys/kernel/random/boot_id'),
                                                'isolated': data.get('plan', {}).get('isolated')}
            self.summary.setText(message)
            QMessageBox.information(self, '系统配置', message)
        elif action in ('probe-mcu', 'flash-mcu', 'flash-eeprom', 'diagnose-eeprom'):
            self.summary.setText(message)
            QMessageBox.information(self, '操作结果', message + ('\n备份：' + data['backup'] if data.get('backup') else ''))
        elif message:
            self.summary.setText(message)
        self.persist()
        if action == 'inspect' and self.after_inspection:
            followup, self.after_inspection = self.after_inspection, ''
            next_step = {'enable-rt': self.enable_rt_checked, 'isolate-cpu': self.isolate_cpu_checked,
                         'create-bringup': self.create_bringup_checked}
            QTimer.singleShot(0, next_step[followup])
        if action == 'build-workspace':
            automatic = self.auto_terminal.isChecked() if self.build_auto_terminal is None else self.build_auto_terminal
            self.build_auto_terminal = None
            self.workspace_built.emit(True, data)
            if automatic:
                self.open_root_terminal()

    def display_inspection(self, data):
        self.inspection = data
        self.current_plan = data['plan']
        self.state['plan'] = data['plan']
        pro = data['pro']
        self.system_info.setText(f"运行内核：{data['kernel']}\nPREEMPT_RT：{'已运行' if data['realtime'] else '尚未运行'}\n"
                                 f"Ubuntu Pro：{'已绑定' if pro.get('attached') else '未绑定 / 尚未确认'}\n"
                                 f"realtime-kernel 服务：{'已启用' if pro.get('rt_enabled') else '未启用 / 尚未确认'}"
                                 + ('\n' + pro['error'] if pro.get('error') else ''))
        self.cpu_topology.setPlainText('\n'.join(f"CPU {c['id']} · 物理核 {c['package']}:{c['core']} · siblings {compact_cpus(c['siblings'])} · {c['kind']} · 基频 {c['base_khz'] / 1000:g} MHz" for c in data['cpus']))
        checks = data['isolation_checks']
        active = all(checks.values())
        text = ('实时内核已运行' if data['realtime'] else '请先启用实时内核并重启') + ' · ' + ('CPU 隔离已生效' if active else '隔离未完整生效 / 等待配置或重启')
        pending = self.state.get('reboot_pending')
        if pending:
            matches_expected = not pending.get('isolated') or (data.get('plan') is not None and
                                                               pending['isolated'] == list(data['plan']['isolated']))
            if pending['boot_id'] == data['boot_id']:
                text += ' · 已保存的系统变更仍需重启'
            elif (pending['action'] == 'enable-rt' and data['realtime']) or (pending['action'] == 'isolate-cpu' and active and matches_expected):
                self.state.pop('reboot_pending', None)
                text += ' · 重启验证通过'
            else:
                text += ' · 已重启但目标设置尚未生效，请检查 GRUB'
        self.summary.setText(text)
        self.display_plan()
        if data.get('plan_error'):
            self.cpu_info.setText(data['plan_error'])
        self.persist()

    def display_plan(self):
        if not self.current_plan:
            return
        p = self.current_plan
        self.cpu_info.setText(f"整颗隔离核心（逻辑 CPU）：{compact_cpus(p['isolated'])}\n"
                              f"SOEM 使用 rt_cpu：{p['rt_cpu']}\n其余 non_rt_cpus：{p['non_rt_cpus']}\n{p['reason']}")
        self.grub_preview.setPlainText(p['parameters'])
        self.launch_preview.setText(f"interface：{self.interface.currentText() or '待网口检测'}    rt_cpu：{p['rt_cpu']}    non_rt_cpus：{p['non_rt_cpus']}")

    def pro_guide(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Ubuntu Pro 绑定引导')
        dialog.resize(650, 360)
        layout = QVBoxLayout(dialog)
        layout.addWidget(note('1. 注册或登录 <a href="https://login.ubuntu.com/">Ubuntu One</a>。\n'
                              '2. 在 <a href="https://ubuntu.com/pro/dashboard">Ubuntu Pro Dashboard</a> 确认订阅。\n'
                              '3. 点击下方按钮打开认证终端，执行官方交互式 pro attach，按终端提示完成账号绑定。\n'
                              '4. 回到这里点击「检查当前电脑」，确认已绑定，再点 realtime-kernel。'))
        layout.addWidget(note('如未安装 pro 客户端，在终端执行 sudo apt update && sudo apt install ubuntu-advantage-tools。\n'
                              '软件不收集或保存 Ubuntu Pro token / 管理员密码。'))
        layout.addWidget(action_button('打开 Ubuntu Pro 绑定终端', lambda: self.open_terminal('attach-pro'), True))
        layout.addWidget(action_button('关闭', dialog.accept))
        dialog.exec_()

    def enable_rt(self):
        self.after_inspection = 'enable-rt'
        self.check_system()

    def enable_rt_checked(self):
        if self.inspection['realtime']:
            QMessageBox.information(self, '实时内核', '当前已运行实时内核，无需重复启用。可以继续 CPU 核心隔离。')
            return
        if not self.inspection['pro'].get('attached'):
            self.pro_guide()
            return
        self.start_job('enable-rt')

    def isolate_cpu(self):
        self.after_inspection = 'isolate-cpu'
        self.check_system()

    def isolate_cpu_checked(self):
        if not self.inspection.get('realtime'):
            QMessageBox.information(self, '实时内核未确认', '请重启进入实时内核，并先点击「检查当前电脑」。')
            return
        if not self.current_plan:
            QMessageBox.information(self, '无法隔离', self.inspection.get('plan_error', '没有可用物理核心'))
            return
        if all(self.inspection['isolation_checks'].values()):
            self.summary.setText('CPU 隔离已完整生效，沿用现有配置。核心编号已显示，可继续准备工作空间。')
            self.display_plan()
            return
        self.start_job('isolate-cpu')

    def create_workspace(self):
        try:
            path = workspace_path(self.workspace_name.text().strip())
            self.workspace_edit.setText(str(path))
            self.start_job('create-workspace', ['--name', self.workspace_name.text().strip()])
        except ValueError as exc:
            QMessageBox.warning(self, '工作空间名称', str(exc))

    def choose_workspace(self):
        path = QFileDialog.getExistingDirectory(self, '选择 EtherCAT 工作空间', str(Path.home()))
        if path:
            self.workspace_edit.setText(path)
            self.state['workspace'] = path
            self.persist()
            self.update_eeprom_path()

    def update_eeprom_path(self):
        root = self.workspace_edit.text().strip()
        if root:
            self.eeprom_image.setText(str(Path(root) / 'src/EcatV2_Master/eeproms' / self.eeprom_kind.currentData()))

    def choose_eeprom(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 EtherCAT EEPROM', self.workspace_edit.text(), 'EEPROM (*.bin)')
        if path:
            self.eeprom_image.setText(path)

    def detect_network(self):
        current = network_snapshot()
        previous = self.state.get('network_previous', {})
        changed = changed_interfaces(previous, current) if previous else []
        old = self.interface.currentText()
        self.interface.clear()
        physical = [name for name, info in current.items() if info['physical']]
        if physical:
            changed = [name for name in changed if name in physical]
        self.interface.addItems(physical or sorted(current))
        if len(changed) == 1:
            self.interface.setCurrentText(changed[0])
            self.network_info.setText(f'检测到 {changed[0]} 链路变化，已选中此网口。重新接好 EtherCAT IN 网线后再刷写。')
        elif len(changed) > 1:
            self.interface.setCurrentText(old)
            self.network_info.setText('多个网口状态改变：' + ', '.join(changed) + '。请手动选择正确网口。')
        else:
            if old:
                self.interface.setCurrentText(old)
            self.network_info.setText('已记录链路快照。插入或拔出网线后再次点击「网口检测」。' + (' 仅发现一个有线网口，可直接选择。' if len(physical) == 1 else ''))
        self.state['network_previous'] = current
        self.state['interface'] = self.interface.currentText()
        self.persist()
        self.append_log('网口快照：' + json.dumps(current, ensure_ascii=False))
        self.display_plan()

    def write_eeprom(self):
        interface = self.interface.currentText().strip()
        image = self.eeprom_image.text().strip()
        if not interface or not Path(image).is_file():
            QMessageBox.warning(self, 'EEPROM', '请先检测网口，并选择工作空间中存在的 EEPROM BIN。')
            return
        text = f'网口：{interface}\n从站链位置：{self.slave_index.value()}\n镜像：{image}\n\n将备份并改写该板 EEPROM，完成后需要重新上电。是否继续？'
        if QMessageBox.question(self, '确认 EEPROM 写入', text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.state['interface'] = interface
            self.state['model'] = 'large' if self.eeprom_kind.currentIndex() == 1 else 'standard'
            self.state['eeprom_image'] = image
            self.state['eeprom_slave'] = self.slave_index.value()
            self.persist()
            self.prepare_eeprom('flash-eeprom')

    def prepare_eeprom(self, action):
        interface = self.interface.currentText().strip()
        image = self.eeprom_image.text().strip()
        if not interface or not Path(image).is_file():
            QMessageBox.warning(self, 'EEPROM', '请先选择有效网口和 EEPROM BIN 文件。')
            return
        arguments = ['--workspace', self.workspace_edit.text(), '--interface', interface,
                     '--slave', str(self.slave_index.value()), '--image', image]
        self.pending_eeprom = (action, arguments)
        if not self.start_job('prepare-eeprom-tool', ['--workspace', self.workspace_edit.text()]):
            self.pending_eeprom = None

    def open_firmware(self, target, image='', expected_hash='', description=''):
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowFlag(Qt.Window, True)
        dialog.setWindowTitle('H750 固件烧录' if target == 'h750' else 'IMU · STM32G431 固件烧录')
        dialog.resize(850, 540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(FirmwarePane(self, target, image, expected_hash, description))
        layout.addWidget(note('操作进度与结果见主界面「首次配置」底部的执行日志。'))
        self.dialogs.append(dialog)
        dialog.destroyed.connect(lambda: self.remove_dialog(dialog))
        dialog.show()
        return dialog

    def remove_dialog(self, dialog):
        if dialog in self.dialogs:
            self.dialogs.remove(dialog)

    def choose_imu_repo(self):
        path = QFileDialog.getExistingDirectory(self, '选择 hipnucimu 源码目录', str(Path.home()))
        if path:
            self.imu_repo.setText(path)

    def prepare_imu(self):
        self.state['imu_repo'] = self.imu_repo.text().strip()
        self.persist()
        self.start_job('prepare-imu', ['--repo', self.imu_repo.text().strip()])

    def profiles(self):
        profiles = [[int(edit.text().strip(), 16 if edit.text().strip().lower().startswith('0x') else 10) for edit in row] for row in self.id_editors]
        validate_imu_ids(profiles)
        return profiles

    def persist_profiles(self):
        try:
            self.state['imu_profiles'] = self.profiles()
            self.persist()
        except ValueError:
            pass
        self.refresh_artifacts()

    def compile_imu(self, index):
        try:
            profiles = self.profiles()
            self.state['imu_profiles'] = profiles
            self.state['imu_repo'] = self.imu_repo.text().strip()
            self.persist()
            started = self.start_job('compile-imu', ['--repo', self.imu_repo.text().strip(), '--profile', str(index),
                                                    '--profiles', json.dumps(profiles), '--output-root', str(self.state_path.parent / 'imu-builds')])
            if started:
                # A failed rebuild must not leave an older ELF labelled as the
                # result of this attempt, even if its ID triplet is unchanged.
                self.state.setdefault('imu_artifacts', {}).pop(str(index), None)
                self.persist()
                self.refresh_artifacts()
        except ValueError as exc:
            QMessageBox.warning(self, 'CAN ID', str(exc))

    def refresh_artifacts(self):
        try:
            profiles = self.profiles()
        except ValueError:
            profiles = None
        for i, label in enumerate(self.artifact_labels, 1):
            record = self.state.get('imu_artifacts', {}).get(str(i))
            if not record:
                label.setText('尚未编译')
                continue
            match = profiles is not None and record.get('ids') == profiles[i - 1]
            valid = Path(record['image']).is_file()
            label.setText(('已编译' if match and valid else 'ID 已变更或产物缺失，需重新编译') + ' · ' + record['image'])

    def flash_profile(self, index):
        try:
            profiles = self.profiles()
            record = self.state.get('imu_artifacts', {}).get(str(index))
            if not record or record.get('ids') != profiles[index - 1] or not Path(record['image']).is_file():
                raise ValueError('当前 ID 没有对应的编译产物，请先编译该 IMU')
            if sha256_file(record['image']) != record['sha256']:
                raise ValueError('编译产物 SHA256 不匹配，请重新编译')
            self.open_firmware('g431', record['image'], record['sha256'],
                               f'IMU {index} · CAN ID ' + ' / '.join(hex(x) for x in record['ids']) + '\n请将 ST-Link 接到这一颗 IMU 板，再执行烧录。')
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'IMU 固件', str(exc))

    def create_bringup(self):
        self.after_inspection = 'create-bringup'
        self.check_system()

    def create_bringup_checked(self):
        if not self.current_plan:
            QMessageBox.information(self, 'CPU 设置', '请先检查系统并完成 CPU 核心隔离。')
            return
        if not self.inspection.get('realtime') or not all(self.inspection.get('isolation_checks', {'unverified': False}).values()):
            missing = [name for name, ok in self.inspection.get('isolation_checks', {}).items() if not ok]
            detail = ('当前未运行 PREEMPT_RT 实时内核。\n' if not self.inspection.get('realtime') else '')
            if missing:
                detail += '尚未生效的隔离项目：' + ', '.join(missing)
            QMessageBox.information(self, '自动检查未通过', detail + '\n请完成对应设置并重启后重试；无需返回其它页面手动确认。')
            return
        if not self.interface.currentText().strip():
            QMessageBox.information(self, '网口', '请先执行网口检测或选择正确的 EtherCAT 网口。')
            return
        self.state['ros_setup'] = self.ros_setup.currentData()
        self.state['interface'] = self.interface.currentText().strip()
        self.persist()
        p = self.current_plan
        self.start_job('create-bringup', ['--workspace', self.workspace_edit.text(), '--interface', self.interface.currentText().strip(),
                                         '--rt-cpu', str(p['rt_cpu']), '--non-rt-cpus', p['non_rt_cpus'],
                                         '--ros-setup', self.ros_setup.currentData() or ''])

    def build_workspace(self, checked=False, *, workspace=None, expected_hash='', open_terminal=None):
        if workspace is not None:
            self.workspace_edit.setText(workspace)
        self.state['workspace'] = self.workspace_edit.text()
        self.state['ros_setup'] = self.ros_setup.currentData()
        self.persist()
        args = ['--workspace', self.workspace_edit.text(), '--ros-setup', self.ros_setup.currentData() or '']
        if expected_hash:
            args += ['--expected-config-hash', expected_hash]
        if self.start_job('build-workspace', args):
            self.build_auto_terminal = open_terminal
            return True
        return False

    def open_terminal(self, action, arguments=()):
        if not self.allow_action():
            return
        terminal = shutil.which('x-terminal-emulator')
        if not terminal or not shutil.which('pkexec'):
            QMessageBox.warning(self, '终端不可用', '需要 x-terminal-emulator、pkexec 和桌面管理员认证代理。')
            return
        command = ['pkexec', '/usr/bin/python3', '-I', '-u', str(Path(__file__).with_name('setup_worker.py')), action, *map(str, arguments)]
        # PyQt5's two-argument overload returns bool on Ubuntu; other bindings
        # expose a (started, pid) tuple. Do not unpack a bool after spawning.
        started = QProcess.startDetached(terminal, ['-e', *command])
        ok = started[0] if isinstance(started, tuple) else started
        if not ok:
            QMessageBox.warning(self, '终端启动失败', '无法打开终端，请查看桌面环境配置。')

    def open_root_terminal(self):
        try:
            verify_workspace_install(self.workspace_edit.text())
        except (ValueError, OSError) as exc:
            QMessageBox.information(self, '启动前检查未通过', str(exc))
            return
        domain = self.project.ros_domain if self.project else 0
        self.open_terminal('root-shell', ['--workspace', self.workspace_edit.text(), '--ros-setup', self.ros_setup.currentData() or '', '--domain', str(domain)])

    def extract_sn(self):
        sns = parse_slave_sns(self.sn_log.toPlainText())
        if sns:
            self.real_sn.setText(sns[0])
            if len(sns) > 1:
                self.append_log('发现多个 SN：' + ', '.join(sns) + '；可先配置一个，再在从站页面添加其余设备。')
        else:
            QMessageBox.information(self, 'SN', '未找到 Found slave id=..., sn=... 日志，请直接输入真实 SN。')

    def adopt_setup(self):
        try:
            sn = self.real_sn.text().strip()
            if not re.fullmatch(r'[1-9][0-9]{0,9}', sn) or int(sn) > 0xFFFFFFFF:
                raise ValueError('请输入实际的非零 uint32 SN')
            if not self.current_plan:
                raise ValueError('请先完成系统 CPU 检查')
            model = 'large' if self.eeprom_kind.currentIndex() == 1 else 'standard'
            slave = Slave(sn, model)
            task = Task.create(3, sn, 1)
            task.values['pub_topic'] = '/imu'
            # The first profile supplies packet IDs for the first IMU. Further
            # sensors can then be added with the existing task editor.
            ids = self.profiles()[0]
            for i, value in enumerate(ids, 1):
                task.values[f'sdowrite_packet{i}_id'] = value
            slave.tasks = [task]
            p = Project([slave], interface=self.interface.currentText().strip(),
                        rt_cpu=self.current_plan['rt_cpu'], non_rt_cpus=self.current_plan['non_rt_cpus'],
                        workspace=self.workspace_edit.text().strip(), ros_domain=self.project.ros_domain if self.project else 0)
            errors = p.validate()
            if errors:
                raise ValueError('\n'.join(errors))
            self.configured.emit(p)
        except ValueError as exc:
            QMessageBox.warning(self, '首次配置', str(exc))

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, '保存首次配置日志', 'first-setup.log', '日志 (*.log)')
        if path:
            try:
                Path(path).write_text(self.log.toPlainText())
            except OSError as exc:
                QMessageBox.warning(self, '日志保存失败', str(exc))
