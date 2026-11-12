"""Build the verified SII helper as the normal user, using local SOEM sources."""
from pathlib import Path
import hashlib
import json
import os
import platform
import pwd
import shutil
import subprocess
import tempfile

from .setup_core import sha256_file, atomic_json


def source_inventory(workspace):
    root = Path(workspace) / 'src/EcatV2_Master/src/soem/soem'
    native = Path(__file__).with_name('eeprom_native.c')
    if not (root / 'soem/ethercat.h').is_file():
        raise ValueError('SOEM 子模块源码不完整，请先递归初始化工作空间子模块')
    sources = sorted((root / 'soem').glob('*.c'))
    sources += sorted((root / 'osal/linux').glob('*.c'))
    sources += sorted((root / 'oshw/linux').glob('*.c'))
    includes = [root / part for part in ('soem', 'osal', 'osal/linux', 'oshw/linux')]
    headers = sorted({file for folder in includes for file in folder.glob('*.h')})
    if not sources or not all(p.is_dir() for p in includes):
        raise ValueError('缺少 SOEM Linux 源码，无法构建 EEPROM 校验工具')
    digest = hashlib.sha256(platform.machine().encode())
    for file in [native, *sources, *headers]:
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return native, sources, includes, digest.hexdigest()


def cache_paths(workspace, cache_root=None):
    _, _, _, digest = source_inventory(workspace)
    if cache_root is None:
        owner = pwd.getpwuid(Path(workspace).stat().st_uid)
        cache_root = Path(owner.pw_dir) / '.cache/ecat-groundstation/eeprom'
    directory = Path(cache_root) / digest[:24]
    return directory / 'eeprom_verified', directory / 'build.json', digest


def verified_eeprom_tool(workspace, cache_root=None):
    tool, manifest, digest = cache_paths(workspace, cache_root)
    try:
        metadata = json.loads(manifest.read_text())
        if metadata['source_hash'] == digest and tool.is_file() and os.access(tool, os.X_OK) and sha256_file(tool) == metadata['binary_hash']:
            return tool
    except (OSError, ValueError, KeyError):
        pass
    raise ValueError('新版 EEPROM 校验工具尚未准备。请在新版界面重新点击「刷写eeprom」或「只读诊断」，先由普通用户自动构建工具。')


def build_eeprom_tool(workspace, run=None, cache_root=None):
    if os.geteuid() == 0:
        raise RuntimeError('EEPROM 工具必须由普通用户构建，不能在管理员任务中编译')
    try:
        return verified_eeprom_tool(workspace, cache_root)
    except ValueError:
        pass
    compiler = shutil.which('cc') or shutil.which('gcc')
    if not compiler:
        raise ValueError('缺少 C 编译器，请安装 build-essential 后重试')
    native, sources, includes, digest = source_inventory(workspace)
    tool, manifest, _ = cache_paths(workspace, cache_root)
    tool.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='eeprom_verified-', dir=tool.parent)
    os.close(fd)
    try:
        args = [compiler, '-std=c99', '-D_GNU_SOURCE', '-O2', '-Wall', '-Wextra']
        for include in includes:
            args += ['-I', str(include)]
        args += [str(native), *map(str, sources), '-pthread', '-lrt', '-o', temp]
        if run is None:
            subprocess.run(args, check=True)
        else:
            run(args, timeout=300)
        Path(temp).chmod(0o755)
        os.replace(temp, tool)
        atomic_json(manifest, {'source_hash': digest, 'binary_hash': sha256_file(tool)})
    finally:
        Path(temp).unlink(missing_ok=True)
    return tool
