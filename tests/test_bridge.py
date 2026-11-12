import time

from ecat_groundstation.bridge import Bridge, Endpoint
from ecat_groundstation.config import Project, Task
from ecat_groundstation.commands import neutral_values


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_demo_lease_expires_and_cannot_auto_resume():
    bridge = Bridge()
    p = Project.demo()
    key = 'sn4587574/app_1'
    try:
        bridge.connect(p)
        wait_until(lambda: bridge.snapshot()[key]['feedback_count'] > 0)
        values = neutral_values(p.slaves[0].tasks[0])
        values['enable'] = 1
        assert not bridge.renew(key, values)
        bridge.arm(key, True)
        assert bridge.renew(key, values)
        wait_until(lambda: bridge.snapshot()[key]['active'])
        wait_until(lambda: not bridge.snapshot()[key]['armed'])
        assert not bridge.snapshot()[key]['active']
        assert not bridge.renew(key, values)
        bridge.arm(key, True)
        bridge.renew(key, values)
        wait_until(lambda: bridge.snapshot()[key]['active'])
        bridge.stop_all()
        wait_until(lambda: not bridge.snapshot()[key]['active'])
        assert not bridge.renew(key, values)
        bridge.reset_stop()
        assert not bridge.renew(key, values)
    finally:
        bridge.disconnect()
    assert bridge.mode == 'disconnected'
    assert bridge.thread is None


def test_feedback_stale_fault_and_other_publisher_gate():
    t = Task.create(12, '1', 1)
    ep = Endpoint('key', t, feedback={'online': 1}, feedback_at=10,
                  receiver_count=1, armed=True, lease=(10, {}))
    b = Bridge()
    assert not b._gate(ep, 10.1, True)
    ep.feedback_at = 1
    assert '反馈超时' in b._gate(ep, 10.1, True)
    ep.feedback_at = 10
    ep.feedback['overload'] = 1
    assert '故障' in b._gate(ep, 10.1, True)
    ep.feedback = {'online': 1}
    ep.foreign_publishers = 1
    assert '其他发布者' in b._gate(ep, 10.1, True)


def test_neutral_repeated_and_fault_latches_arm():
    b = Bridge()
    t = Task.create(12, '1', 1)
    ep = Endpoint('key', t, armed=True, active=True, receiver_count=1,
                  feedback={'online': 1}, feedback_at=1, lease=(10, {'enable': 1}))
    b.endpoints = {'key': ep}
    b._tick(10.1, False)
    assert not ep.active and not ep.armed
    assert ep.command_count == 1
    b._tick(10.2, False)
    assert ep.command_count == 2
    b._tick(11, False)
    assert ep.command_count == 2
