"""Produce local Qt screenshots without connecting to ROS or hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont
from PyQt5.QtTest import QTest
from ecat_groundstation.app import MainWindow, STYLE

app = QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Noto Sans CJK SC', 10))
app.setStyleSheet(STYLE)
window = MainWindow()
window.show()
out = Path(__file__).resolve().parents[1] / 'artifacts'
out.mkdir(exist_ok=True)
QTest.qWait(100)
window.grab().save(str(out / 'overview.png'))
window.nav.setCurrentRow(1)
window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
QTest.qWait(100)
window.grab().save(str(out / 'configuration.png'))
window.toggle_connection()  # Default is explicitly the in-process demo.
QTest.qWait(200)
window.nav.setCurrentRow(3)
window.open_panel(0)
panel = window.panels['sn4587574/app_1']
QTest.qWait(1100)
panel.grab().save(str(out / 'actuator-demo.png'))
window.close_panels()
window.bridge.disconnect()
window.dirty = False
window.close()
app.processEvents()
print('Saved overview, configuration, and demo actuator screenshots')
