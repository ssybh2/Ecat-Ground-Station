"""Persist task configuration to its actual ROS workspace, with recoverable backups."""
from pathlib import Path
import hashlib
import json
import os
import time

from .config import atomic_write, save_project
from .setup_core import atomic_json, sha256_file


class WorkspaceAccessError(PermissionError):
    """A root launch from an older version created the app data directory."""


def config_path(workspace):
    if not str(workspace).strip():
        raise ValueError('尚未关联工作空间，请先选择工作空间或从首次配置进入')
    root = Path(workspace).expanduser().resolve()
    package = root / 'src/soem_bringup'
    if not root.is_dir() or not (package / 'package.xml').is_file():
        raise ValueError('工作空间中没有 soem_bringup，请先在首次配置创建 bringup 包')
    target = package / 'config/config.yaml'
    if not target.resolve().is_relative_to(root):
        raise ValueError('config.yaml 指向工作空间之外，未自动覆盖')
    return target


def infer_workspace(path):
    path = Path(path).expanduser().resolve()
    if path.name == 'config.yaml' and path.parent.name == 'config' and path.parent.parent.name == 'soem_bringup':
        root = path.parent.parent.parent.parent
        if path.parent.parent.parent.name == 'src' and (path.parent.parent / 'package.xml').is_file():
            return str(root)
    return ''


def write_workspace_config(project):
    # Validate the entire project before touching an existing robot configuration.
    content = project.export_yaml()
    target = config_path(project.workspace)
    root = Path(project.workspace).expanduser().resolve()
    metadata = root / '.ecat-groundstation'
    snapshot = metadata / 'project.ecat.json'
    marker = root / 'src/soem_bringup/.ecat-groundstation-generated.json'
    if any(not path.resolve().is_relative_to(root) for path in (metadata, snapshot, marker, metadata / 'config-backups')):
        raise ValueError('工程元数据目录指向工作空间之外')
    if metadata.is_dir() and metadata.stat().st_uid == 0 and os.geteuid() != 0:
        raise WorkspaceAccessError(f'工程数据目录由 root 创建，需要修复归属：{metadata}')
    marker_data = json.loads(marker.read_text()) if marker.is_file() else None
    backup = None
    changed = not target.is_file() or target.read_text(encoding='utf-8') != content
    if changed and target.is_file():
        backup = metadata / 'config-backups' / f'{time.time_ns()}-config.yaml'
        atomic_write(backup, target.read_text(encoding='utf-8'))
    if changed:
        atomic_write(target, content)
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if sha256_file(target) != digest:
        raise RuntimeError('写入后的 config.yaml 校验失败')
    save_project(project, snapshot)
    if marker_data is not None:
        data = marker_data
        data.setdefault('hashes', {})['config/config.yaml'] = digest
        data['configuration_ready'] = True
        atomic_json(marker, data)
    return {'workspace': str(root), 'config': str(target), 'config_hash': digest,
            'project_path': str(snapshot), 'backup': str(backup) if backup else '', 'changed': changed}


def last_workspace_project(state):
    workspace = state.get('workspace', '')
    if not workspace:
        return None
    snapshot = Path(workspace).expanduser() / '.ecat-groundstation/project.ecat.json'
    return snapshot if snapshot.is_file() else None
