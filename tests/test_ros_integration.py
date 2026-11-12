"""Runs only when the built ROS overlay is sourced. Uses an isolated DDS domain.

No EtherCAT socket, backend, hardware scan or real actuator is started.
"""
import os
import time
import threading

import pytest

from ecat_groundstation.config import Project, Slave, Task
from ecat_groundstation.bridge import Bridge
from ecat_groundstation.commands import neutral_values


def test_real_ros_transport_command_feedback_watchdog():
    rclpy = pytest.importorskip('rclpy')
    msgs = pytest.importorskip('custom_msgs.msg')
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    domain = 170 + os.getpid() % 30
    context = Context()
    context.init(args=[], domain_id=domain)
    node = rclpy.create_node('ecat_test_virtual_motor', context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    p = Project([Slave('9990001')], ros_domain=domain)
    task = Task.create(12, '9990001', 1)
    task.values['sub_topic'] = '/ecat_test/command'
    task.values['pub_topic'] = '/ecat_test/feedback'
    p.slaves[0].tasks.append(task)
    received = []
    pub = node.create_publisher(msgs.ReadDmMotor, task.values['pub_topic'], qos_profile_sensor_data)
    node.create_subscription(msgs.WriteDmMotorMITControl, task.values['sub_topic'],
                             lambda msg: received.append(msg), qos_profile_sensor_data)
    halt = threading.Event()
    publish_feedback = threading.Event()
    publish_feedback.set()

    def fake_motor():
        while not halt.is_set():
            if publish_feedback.is_set():
                msg = msgs.ReadDmMotor()
                msg.online = 1
                msg.position = 0.25
                pub.publish(msg)
            executor.spin_once(timeout_sec=.01)

    thread = threading.Thread(target=fake_motor, daemon=True)
    thread.start()
    bridge = Bridge()
    key = 'sn9990001/app_1'

    def wait_for(pred, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(.02)
        assert pred(), bridge.error or bridge.snapshot()

    try:
        bridge.connect(p, demo=False)
        assert bridge.ready.wait(5)
        assert bridge.mode == 'ros', bridge.error
        wait_for(lambda: bridge.snapshot()[key]['feedback_count'] >= 3 and bridge.snapshot()[key]['receivers'] > 0)
        assert abs(bridge.snapshot()[key]['feedback']['position'] - .25) < 1e-6
        values = neutral_values(task)
        values.update(enable=1, p_des=.1, kp=0.0, kd=.1)
        bridge.arm(key, True)
        deadline = time.monotonic() + .4
        while time.monotonic() < deadline:
            assert bridge.renew(key, values)
            time.sleep(.04)
        wait_for(lambda: any(m.enable == 1 and abs(m.p_des - .1) < 1e-5 for m in received))
        # Stop GUI renewal: worker must emit disable and latch arm off.
        wait_for(lambda: not bridge.snapshot()[key]['armed'])
        wait_for(lambda: bool(received) and received[-1].enable == 0)
        assert not bridge.renew(key, values)
        # Then keep the GUI heartbeat alive while motor feedback disappears.
        bridge.arm(key, True)
        assert bridge.renew(key, values)
        publish_feedback.clear()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and bridge.snapshot()[key]['armed']:
            bridge.renew(key, values)
            time.sleep(.04)
        assert not bridge.snapshot()[key]['armed']
        assert '反馈超时' in bridge.snapshot()[key]['reason']
        wait_for(lambda: received[-1].enable == 0)
        publish_feedback.set()
        wait_for(lambda: bridge.snapshot()[key]['age'] < .1)
        assert not bridge.renew(key, values)  # Recovery cannot replay the last command.
    finally:
        bridge.disconnect()
        halt.set()
        thread.join(2)
        executor.shutdown(timeout_sec=1)
        node.destroy_node()
        context.shutdown()
