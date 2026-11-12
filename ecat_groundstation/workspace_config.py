"""Persist task configuration to its actual ROS workspace, with recoverable backups."""
from pathlib import Path
import ast
import hashlib
import json
import os
import time

from .config import Project, atomic_write, save_project, load_project
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


def load_workspace_project(workspace):
    """Read source YAML as truth; use a saved project only for non-YAML metadata.

    Launch Python is parsed, never imported or executed while loading a workspace.
    Loading is read-only, including for workspaces never created by this app.
    """
    target = config_path(workspace)
    root = Path(workspace).expanduser().resolve()
    project, warnings = Project.import_yaml(target.read_text(encoding='utf-8'))
    project.workspace = str(root)
    snapshot = root / '.ecat-groundstation/project.ecat.json'
    if snapshot.is_file():
        try:
            saved = load_project(snapshot)
            if saved.workspace and Path(saved.workspace).expanduser().resolve() == root:
                for key in ('interface', 'rt_cpu', 'non_rt_cpus', 'ros_domain'):
                    setattr(project, key, getattr(saved, key))
                models = {s.sn: s.model for s in saved.slaves}
                for slave in project.slaves:
                    if models.get(slave.sn) == 'large':
                        slave.model = 'large'
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            warnings.append(f'已忽略旧工程快照，任务仍从 config.yaml 读取：{exc}')

    launch = target.parent.parent / 'launch/bringup.launch.py'
    if launch.is_file():
        try:
            tree = ast.parse(launch.read_text(encoding='utf-8'))
            settings = {}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if not keys.intersection(('interface', 'rt_cpu', 'non_rt_cpus')):
                    continue
                for key, value in zip(node.keys, node.values):
                    if not isinstance(key, ast.Constant) or key.value not in ('interface', 'rt_cpu', 'non_rt_cpus'):
                        continue
                    try:
                        literal = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        warnings.append(f'bringup.launch.py 的 {key.value} 是动态表达式，请核对工程设置。')
                        continue
                    expected = int if key.value == 'rt_cpu' else str
                    if type(literal) is not expected:
                        raise ValueError(f'{key.value} 类型不正确')
                    if key.value in settings and settings[key.value] != literal:
                        raise ValueError(f'{key.value} 有多个不同值，请核对工程设置')
                    settings[key.value] = literal
            for key, value in settings.items():
                setattr(project, key, value)
        except (OSError, SyntaxError, ValueError) as exc:
            warnings.append(f'未完整读取 launch 参数，请核对网口和 CPU 设置：{exc}')
    installed = root / 'install/soem_bringup/share/soem_bringup/config/config.yaml'
    if not installed.is_file():
        installed = root / 'install/share/soem_bringup/config/config.yaml'
    if installed.is_file() and installed.read_bytes() != target.read_bytes():
        warnings.append('源 config.yaml 与已安装配置不同；实机调试前请 colcon build 并重新启动主站。')
    return project, warnings


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
