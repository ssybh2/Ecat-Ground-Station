"""Transport and controls are exercised with synthetic data, never hardware."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest

from ecat_groundstation.bridge import Bridge, Endpoint
from ecat_groundstation.config import Project, Slave, Task
from ecat_groundstation.ros_transport import configure_ros_transport
from ecat_groundstation.widgets import CommandPanel, Scope


def test_transport_profile_is_packaged_udp_and_preserves_explicit_settings():
    import xml.etree.ElementTree as ET
    env = {}
    assert 'UDP' in configure_ros_transport(env)
    tree = ET.parse(env['FASTRTPS_DEFAULT_PROFILES_FILE'])
    ns = {'dds': 'http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles'}
    assert tree.find('.//dds:type', ns).text == 'UDPv4'
    assert tree.find('.//dds:useBuiltinTransports', ns).text == 'false'
    assert tree.find('.//dds:participant', ns).attrib['is_default_profile'] == 'true'
    custom = {'FASTRTPS_DEFAULT_PROFILES_FILE': '/custom/profile.xml'}
    configure_ros_transport(custom)
    assert custom == {'FASTRTPS_DEFAULT_PROFILES_FILE': '/custom/profile.xml'}
    other = {'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp'}
    configure_ros_transport(other)
    assert other == {'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp'}


def test_link_status_distinguishes_missing_publishers_transport_stale_and_output_only():
    b = Bridge()
    b.mode = 'ros'
    ep = Endpoint('imu', Task.create(3, '1', 1))
    assert '未发现反馈发布者' in b.link_status(ep, 10)
    ep.feedback_publishers = 1
    assert 'DDS' in b.link_status(ep, 10)
    ep.feedback_at = 9.9
    assert '正常' in b.link_status(ep, 10)
    ep.feedback_at = 8
    assert '过期' in b.link_status(ep, 10)
    ep.read_types = ['std_msgs/msg/Float32']
    assert '类型不匹配' in b.link_status(ep, 10)
    ep = Endpoint('pwm', Task.create(6, '1', 2))
    assert '未发现命令订阅者' in b.link_status(ep, 10)
    ep.receiver_count = 1
    assert '此任务无反馈' in b.link_status(ep, 10)
    ep.foreign_publishers = 1
    assert '其他命令发布者' in b.link_status(ep, 10)
    ep.reason = '反馈超时，已释放输出'
    b.endpoints = {'pwm': ep}
    b.arm('pwm', False)
    assert ep.reason == '反馈超时，已释放输出'  # Unchecking the UI must not hide a fault.


def test_scope_keeps_receive_timestamps_and_multiple_series():
    app = QApplication.instance() or QApplication([])
    scope = Scope()
    scope.push({'x': 1., 'y': 2., 'z': 3.}, timestamp=10)
    scope.push({'x': 4., 'y': 5., 'z': 6.}, timestamp=12)
    assert list(scope.series['x']) == [(10, 1.), (12, 4.)]
    assert set(scope.series) == {'x', 'y', 'z'}
    scope.clear()
    assert not scope.series and not scope.samples
    scope.close()


VIRTUAL_MASTER = r'''
import json, sys, time
import rclpy
from rclpy.context import Context
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from custom_msgs.msg import WriteOnBoardPWM
from sensor_msgs.msg import Imu
context=Context(); context.init(args=[], domain_id=int(sys.argv[1]))
node=rclpy.create_node('egc_virtual_master', context=context)
executor=SingleThreadedExecutor(context=context); executor.add_node(node)
log=open(sys.argv[2], 'a', buffering=1)
def receive(msg):
    log.write(json.dumps([msg.channel1,msg.channel2,msg.channel3,msg.channel4])+'\n')
node.create_subscription(WriteOnBoardPWM, '/egc_test/pwm', receive, qos_profile_sensor_data)
pub=node.create_publisher(Imu, '/egc_test/imu', qos_profile_sensor_data)
try:
    while context.ok():
        t=time.monotonic()
        msg=Imu();msg.orientation.w=1.
        msg.angular_velocity.x=t%10;msg.angular_velocity.y=2.;msg.angular_velocity.z=3.
        msg.linear_acceleration.z=9.81
        pub.publish(msg)
        executor.spin_once(timeout_sec=.005)
finally:
    executor.shutdown();node.destroy_node();context.shutdown();log.close()
'''


def test_real_cross_process_pwm_publish_and_imu_multiaxis_plot(tmp_path, monkeypatch):
    pytest.importorskip('rclpy')
    pytest.importorskip('custom_msgs.msg')
    pytest.importorskip('sensor_msgs.msg')
    app = QApplication.instance() or QApplication([])
    env = os.environ.copy()
    configure_ros_transport(env)
    for key in ('FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE'):
        monkeypatch.setenv(key, env[key])
    domain = 205 + os.getpid() % 20  # Isolated from real robot Domain 24; no EtherCAT process.
    logfile = tmp_path / 'commands.jsonl'
    childlog = (tmp_path / 'virtual-master.log').open('w')
    proc = subprocess.Popen([sys.executable, '-c', VIRTUAL_MASTER, str(domain), str(logfile)],
                            env=env, stdout=childlog, stderr=subprocess.STDOUT)
    pwm, imu = Task.create(6, '9991234', 1), Task.create(3, '9991234', 2)
    pwm.values['sub_topic'] = '/egc_test/pwm'
    imu.values['pub_topic'] = '/egc_test/imu'
    p = Project([Slave('9991234', tasks=[pwm, imu])], ros_domain=domain)
    b = Bridge()
    panels = []

    def wait_for(pred, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            QTest.qWait(20)
        assert pred(), b.error or (tmp_path / 'virtual-master.log').read_text()

    def commands():
        return [json.loads(line) for line in logfile.read_text().splitlines()] if logfile.exists() else []

    try:
        b.connect(p, demo=False)
        wait_for(lambda: b.mode == 'ros')
        pwm_key, imu_key = 'sn9991234/app_1', 'sn9991234/app_2'
        wait_for(lambda: b.snapshot()[pwm_key]['receivers'] == 1 and b.snapshot()[imu_key]['feedback_count'] > 5
                 and b.snapshot()[imu_key]['feedback_publishers'] == 1)
        panel = CommandPanel(pwm_key, pwm, b)
        panels.append(panel)
        panel.show()
        monkeypatch.setattr(panel, 'isActiveWindow', lambda: True)
        panel.editors[('channel1', None)][0].setValue(1600)
        QTest.qWait(150)
        assert not commands()  # Editing alone never actuates anything.
        panel.publish.click()
        assert not commands() and not panel.publish_until  # Explicit arm remains required.
        panel.arm.setChecked(True)
        panel.publish_duration.setValue(.3)
        panel.publish.click()
        wait_for(lambda: [1600,1500,1500,1500] in commands())
        wait_for(lambda: bool(commands()) and commands()[-1] == [1500]*4)
        assert not b.snapshot()[pwm_key]['active']
        assert b.snapshot()[pwm_key]['last_command'] == {f'channel{i}': 1500 for i in range(1, 5)}
        panel.poll()
        assert panel.feedback_table.rowCount() == 4  # Output-only tasks show sent commands, never fake feedback.
        assert set(panel.scope.series) == {f'channel{i}' for i in range(1, 5)}
        ipanel = CommandPanel(imu_key, imu, b)
        panels.append(ipanel)
        ipanel.show()
        wait_for(lambda: len(ipanel.scope.series.get('angular_velocity.x', [])) > 3)
        assert set(ipanel.scope.series) == {'angular_velocity.x', 'angular_velocity.y', 'angular_velocity.z'}
        assert b.snapshot()[imu_key]['feedback_hz'] > 10
        assert ipanel.feedback_table.rowCount() > 10
        ipanel.trace.setCurrentIndex(1)
        wait_for(lambda: len(ipanel.scope.series.get('linear_acceleration.z', [])) > 2)
        assert ipanel.scope.series['linear_acceleration.z'][-1][1] == 9.81
        # Loss of UI focus cancels a long timed publish and restores PWM initial values.
        panel.arm.setChecked(True)
        panel.publish_duration.setValue(10)
        panel.publish.click()
        wait_for(lambda: b.snapshot()[pwm_key]['active'])
        monkeypatch.setattr(panel, 'isActiveWindow', lambda: False)
        panel.poll()
        wait_for(lambda: not b.snapshot()[pwm_key]['active'] and commands()[-1] == [1500]*4)
        assert panel.publish_until == 0
    finally:
        for panel in panels:
            panel.close()
        b.disconnect()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=5)
        childlog.close()
        app.processEvents()
