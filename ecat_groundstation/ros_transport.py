"""A UDP-only Fast DDS profile avoids root/user shared-memory permissions.

Explicit user middleware/profile settings take precedence. This only changes
the environment of the station process and its generated root launch shell.
"""
import os
from pathlib import Path


def configure_ros_transport(env=None):
    env = os.environ if env is None else env
    if env.get('RMW_IMPLEMENTATION', 'rmw_fastrtps_cpp') not in ('rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp'):
        return '沿用所选 ROS 中间件'
    profile = str(Path(__file__).with_name('fastdds_udp.xml').resolve())
    if any(env.get(key) and env[key] != profile for key in ('FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_DEFAULT_PROFILES_FILE')):
        return '沿用自定义 DDS 配置；无数据时请检查 root / 普通用户的传输兼容性'
    env['FASTRTPS_DEFAULT_PROFILES_FILE'] = profile
    env['FASTDDS_DEFAULT_PROFILES_FILE'] = profile
    return 'Fast DDS UDPv4（兼容 root 主站与普通用户上位机）'
