"""ROS access is confined to one worker; a short GUI lease gates each command.

This software interlock does not replace a physical emergency stop. A killed
process cannot deliver a disable message; the firmware/master watchdog remains
responsible for that failure mode.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from collections import deque
import math
import threading
import time
import traceback

from .schema import message_types
from .commands import neutral_values, validate_command, feedback_problem, verify_message_class
from .ros_transport import configure_ros_transport


@dataclass
class Endpoint:
    key: str
    task: object
    feedback: dict = field(default_factory=dict)
    feedback_at: float = 0
    feedback_count: int = 0
    command_count: int = 0
    receiver_count: int = 0
    foreign_publishers: int = 0
    reason: str = ''
    active: bool = False
    armed: bool = False
    neutral_until: float = 0
    lease: tuple | None = None
    publisher: object = None
    message_class: object = None
    feedback_publishers: int = 0
    read_types: list = field(default_factory=list)
    write_types: list = field(default_factory=list)
    feedback_times: object = field(default_factory=lambda: deque(maxlen=200))
    last_command: dict = field(default_factory=dict)
    command_at: float = 0


class Bridge:
    LEASE_TIMEOUT = 0.30
    FEEDBACK_TIMEOUT = 0.60

    def __init__(self):
        self.lock = threading.RLock()
        self.thread = None
        self.halt = threading.Event()
        self.ready = threading.Event()
        self.endpoints = {}
        self.graph = []
        self.nodes = []
        self.latencies = {}
        self.logs = deque(maxlen=300)
        self.mode = 'disconnected'
        self.error = ''
        self.stopped = False

    def log(self, text):
        with self.lock:
            self.logs.append((time.strftime('%H:%M:%S'), text))

    def connect(self, project, demo=True):
        errors = project.validate()
        if errors:
            raise ValueError('\n'.join(errors))
        self.disconnect()
        with self.lock:
            self.endpoints = {f'sn{s.sn}/app_{i}': Endpoint(f'sn{s.sn}/app_{i}', copy.deepcopy(t))
                              for s in project.slaves for i, t in enumerate(s.tasks, 1)}
            self.graph, self.nodes, self.latencies = [], [], {}
            self.error = ''
            self.stopped = False
            self.mode = 'demo' if demo else 'connecting'
        self.halt.clear()
        self.ready.clear()
        self.thread = threading.Thread(target=self._run_demo if demo else self._run_ros,
                                       args=(copy.deepcopy(project),), daemon=True, name='ecat-ros-worker')
        self.thread.start()

    def disconnect(self):
        self.stop_all('断开连接')
        self.halt.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                raise RuntimeError('ROS 工作线程仍未退出，拒绝创建第二个控制连接')
        self.thread = None
        self.mode = 'disconnected'

    def stop_all(self, reason='软件停止已锁定'):
        with self.lock:
            self.stopped = True
            for ep in self.endpoints.values():
                ep.lease = None
                ep.armed = False
                ep.reason = reason
        self.log(reason)

    def reset_stop(self):
        with self.lock:
            self.stopped = False
            for ep in self.endpoints.values():
                ep.lease = None
                ep.armed = False
                ep.reason = ''
        self.log('停止锁定已复位；需重新勾选控制并按住运行')

    def release(self, key):
        with self.lock:
            if key in self.endpoints:
                self.endpoints[key].lease = None

    def arm(self, key, enabled):
        with self.lock:
            ep = self.endpoints[key]
            ep.lease = None
            ep.armed = bool(enabled) and not self.stopped
            ep.reason = '' if ep.armed else (ep.reason or '控制未授权')
            return ep.armed

    def renew(self, key, values):
        with self.lock:
            if self.stopped or self.mode not in ('demo', 'ros') or not self.endpoints[key].armed:
                return False
            ep = self.endpoints[key]
            validate_command(ep.task, values)
            ep.lease = (time.monotonic(), copy.deepcopy(values))
            return True

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return {key: {'feedback': copy.deepcopy(ep.feedback),
                          'age': now - ep.feedback_at if ep.feedback_at else float('inf'),
                          'feedback_count': ep.feedback_count, 'command_count': ep.command_count,
                          'receivers': ep.receiver_count, 'reason': ep.reason, 'active': ep.active,
                          'armed': ep.armed, 'feedback_at': ep.feedback_at,
                          'feedback_publishers': ep.feedback_publishers,
                          'last_command': copy.deepcopy(ep.last_command), 'command_at': ep.command_at,
                          'foreign_publishers': ep.foreign_publishers,
                          'read_types': list(ep.read_types), 'write_types': list(ep.write_types),
                          'feedback_hz': ((len(ep.feedback_times) - 1) / (ep.feedback_times[-1] - ep.feedback_times[0])
                                          if now - ep.feedback_at <= self.FEEDBACK_TIMEOUT and len(ep.feedback_times) > 1
                                          and ep.feedback_times[-1] > ep.feedback_times[0] else 0),
                          'link_status': self.link_status(ep, now)}
                    for key, ep in self.endpoints.items()}

    def link_status(self, ep, now):
        read, write = message_types(ep.task)
        if read:
            if ep.read_types and read not in ep.read_types:
                return '反馈消息类型不匹配'
            if self.mode == 'ros' and not ep.feedback_publishers:
                return '未发现反馈发布者 · 检查主站 / Domain / 话题'
            if not ep.feedback_at:
                return '已发现发布者但未收到数据 · 检查 DDS 传输 / QoS'
            if now - ep.feedback_at > self.FEEDBACK_TIMEOUT:
                return '反馈已过期 · 主站停止或数据链路中断'
            return '反馈接收正常'
        if write:
            if ep.write_types and write not in ep.write_types:
                return '命令消息类型不匹配'
            if self.mode == 'ros' and ep.receiver_count < 1:
                return '未发现命令订阅者 · 检查主站 / Domain / 话题'
            if ep.foreign_publishers:
                return '存在其他命令发布者 · 请关闭其他控制连接'
            return '命令订阅已匹配 · 此任务无反馈'
        return '无收发话题'

    def _gate(self, ep, now, real):
        if self.stopped:
            return '软件停止已锁定'
        if not ep.armed:
            return ep.reason or '控制未授权'
        if not ep.lease:
            return '待机 · 按住运行'
        if now - ep.lease[0] > self.LEASE_TIMEOUT:
            return '操作心跳超时，已释放输出'
        if real and ep.receiver_count < 1:
            return '命令话题未发现匹配的主站订阅者'
        if real and ep.foreign_publishers:
            return '命令话题存在其他发布者，已锁定输出'
        if message_types(ep.task)[0]:
            if now - ep.feedback_at > self.FEEDBACK_TIMEOUT:
                return '反馈超时，已释放输出'
            problem = feedback_problem(ep.task, ep.feedback)
            if problem:
                return problem
        return ''

    def _tick(self, now, real):
        for ep in self.endpoints.values():
            if not message_types(ep.task)[1]:
                continue
            problem = self._gate(ep, now, real)
            if problem:
                if ep.active:
                    ep.neutral_until = now + 0.4
                    self.log(f'{ep.key}: {problem}')
                if ep.neutral_until > now:
                    self._publish(ep, neutral_values(ep.task), real)
                ep.active = False
                if ep.lease:
                    ep.lease = None  # Never replay a command after an outage.
                    ep.armed = False
                ep.reason = problem
            else:
                ep.neutral_until = 0
                self._publish(ep, ep.lease[1], real)
                ep.active = True
                ep.reason = ''

    def _publish(self, ep, values, real):
        if real:
            msg = ep.message_class()
            from rosidl_runtime_py.set_message import set_message_fields
            set_message_fields(msg, values)
            ep.publisher.publish(msg)
        ep.command_count += 1
        ep.last_command = copy.deepcopy(values)
        ep.command_at = time.monotonic()

    def _run_demo(self, project):
        self.ready.set()
        self.log('离线演示已连接；所有反馈为模拟数据')
        start = time.monotonic()
        try:
            while not self.halt.wait(0.05):
                now = time.monotonic()
                with self.lock:
                    for ep in self.endpoints.values():
                        if message_types(ep.task)[0]:
                            phase = (now - start) * 0.8
                            ep.feedback = {'online': 1, 'position': math.sin(phase) * 0.25,
                                           'velocity': math.cos(phase) * 0.2, 'torque': 0.1,
                                           'mos_temperature': 29, 'enabled': int(ep.active)}
                            if ep.task.type_id in (5, 15) or (ep.task.type_id == 2 and ep.task.values['sdowrite_control_type'] == 8):
                                for i in range(1, 5):
                                    ep.feedback.update({f'motor{i}_online': 1, f'motor{i}_rpm': int(100 * math.sin(phase)),
                                                        f'motor{i}_error_code': 0})
                            ep.feedback_at = now
                            ep.feedback_count += 1
                            ep.feedback_times.append(now)
                            ep.feedback_publishers = 1
                            if ep.task.type_id == 3:
                                ep.feedback = {'orientation': {'w': math.cos(phase / 2), 'x': 0., 'y': 0., 'z': math.sin(phase / 2)},
                                               'angular_velocity': {'x': .1 * math.sin(phase), 'y': .2 * math.cos(phase), 'z': .8},
                                               'linear_acceleration': {'x': .1 * math.cos(phase), 'y': 0., 'z': 9.81}}
                        ep.receiver_count = 1
                    self._tick(now, False)
                    self.latencies = {s.sn: 0.18 for s in project.slaves}
        finally:
            with self.lock:
                for ep in self.endpoints.values():
                    ep.active = False

    def _run_ros(self, project):
        context = node = executor = None
        try:
            self.log(configure_ros_transport())
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
            from rosidl_runtime_py.utilities import get_message
            from rosidl_runtime_py.convert import message_to_ordereddict
            context = Context()
            context.init(args=[], domain_id=project.ros_domain)
            node = rclpy.create_node('ecat_groundstation', context=context)
            executor = SingleThreadedExecutor(context=context)
            executor.add_node(node)
            qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=1,
                             reliability=ReliabilityPolicy.BEST_EFFORT)

            for ep in self.endpoints.values():
                read, write = message_types(ep.task)
                if write:
                    ep.message_class = get_message(write)
                    verify_message_class(ep.task, ep.message_class)
                    ep.publisher = node.create_publisher(ep.message_class, ep.task.values['sub_topic'], qos)
                if read:
                    def receive(msg, endpoint=ep):
                        with self.lock:
                            endpoint.feedback = dict(message_to_ordereddict(msg))
                            endpoint.feedback_at = time.monotonic()
                            endpoint.feedback_count += 1
                            endpoint.feedback_times.append(endpoint.feedback_at)
                    node.create_subscription(get_message(read), ep.task.values['pub_topic'], receive, qos)
            from std_msgs.msg import Float32
            for slave in project.slaves:
                def latency(msg, sn=slave.sn):
                    with self.lock:
                        self.latencies[sn] = msg.data
                node.create_subscription(Float32, slave.latency_topic, latency, qos)
            self.mode = 'ros'
            self.ready.set()
            self.log(f'ROS 2 节点已启动，Domain {project.ros_domain}；等待匹配主站话题和实际数据')
            last_tick = last_graph = 0
            while not self.halt.is_set() and context.ok():
                executor.spin_once(timeout_sec=0.005)
                now = time.monotonic()
                if now - last_tick >= 0.05:
                    with self.lock:
                        for ep in self.endpoints.values():
                            if ep.publisher:
                                ep.receiver_count = ep.publisher.get_subscription_count()
                                ep.foreign_publishers = max(0, node.count_publishers(ep.task.values['sub_topic']) - 1)
                        self._tick(now, True)
                    last_tick = now
                if now - last_graph >= 1:
                    graph = node.get_topic_names_and_types()
                    nodes = node.get_node_names_and_namespaces()
                    endpoint_graph = {}
                    for key, ep in self.endpoints.items():
                        read, write = message_types(ep.task)
                        pubs = node.get_publishers_info_by_topic(ep.task.values['pub_topic']) if read else []
                        subs = node.get_subscriptions_info_by_topic(ep.task.values['sub_topic']) if write else []
                        endpoint_graph[key] = (sum(p.topic_type == read for p in pubs),
                                               sorted({p.topic_type for p in pubs}), sorted({p.topic_type for p in subs}))
                    with self.lock:
                        self.graph, self.nodes = graph, nodes
                        for key, (count, read_types, write_types) in endpoint_graph.items():
                            ep = self.endpoints[key]
                            ep.feedback_publishers, ep.read_types, ep.write_types = count, read_types, write_types
                    last_graph = now
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.log('ROS 2 错误: ' + self.error)
            self.log(traceback.format_exc())
            self.mode = 'error'
            self.ready.set()
        finally:
            with self.lock:
                for ep in self.endpoints.values():
                    if (ep.active or ep.neutral_until > time.monotonic()) and ep.publisher:
                        try:
                            for _ in range(3):
                                self._publish(ep, neutral_values(ep.task), True)
                        except Exception as exc:
                            self.log(f'{ep.key}: 释放消息发送失败: {exc}')
                    ep.active = False
                    ep.lease = None
            if executor:
                executor.shutdown(timeout_sec=1)
            if node:
                node.destroy_node()
            if context and context.ok():
                context.shutdown()
