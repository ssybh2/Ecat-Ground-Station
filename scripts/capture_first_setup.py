"""Screenshot first-run pages using read-only inspection and temporary state."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont
from PyQt5.QtTest import QTest
from ecat_groundstation.app import MainWindow, STYLE
from ecat_groundstation.setup_core import inspect_system

app = QApplication([])
app.setStyle('Fusion')
app.setStyleSheet(STYLE)
app.setFont(QFont('Noto Sans CJK SC', 10))
root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='ecat-first-setup-preview-') as temporary:
    window = MainWindow()
    window.first_setup.state_path = Path(temporary) / 'state.json'
    window.show()
    window.nav.setCurrentRow(window.PAGE_FIRST_SETUP)
    window.first_setup.display_inspection(inspect_system())
    for index, name in enumerate(('system', 'workspace', 'h750', 'imu', 'bringup')):
        window.first_setup.tabs.setCurrentIndex(index)
        QTest.qWait(100)
        window.grab().save(str(root / 'artifacts' / f'first-setup-{name}.png'))
    window.dirty = False
    window.close()
    app.processEvents()
print('First-setup screenshots saved; no system settings or hardware changed.')
