"""Build the GNU components used by the no-exceptions H750 firmware for M7 only.

No replacement C++ implementations or system SDK modifications are used.
Sources are pinned upstream GCC files, with their runtime exception retained.
"""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request

from .setup_core import atomic_json, sha256_file


CATALOG_PATH = Path(__file__).with_name('h750_runtime_sources.json')
HEADER_PACKAGE = 'libstdc++-arm-none-eabi-dev'
MAX_HEADER_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024


def _headers(cache, version, env, run):
    """Reuse the 1 MB header archive, or download exactly that bounded package."""
    query = subprocess.run(['apt-cache', 'show', HEADER_PACKAGE], capture_output=True, text=True, timeout=30)
    package = None
    for paragraph in query.stdout.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        if fields.get('Package') == HEADER_PACKAGE:
            package = fields
            break
    if not package or not re.fullmatch(r'[a-f0-9]{64}', package.get('SHA256', '')):
        raise ValueError('找不到 ARM C++ 头文件包元数据，请先安装 H750 轻量基础依赖')
    size = int(package.get('Size', '0'))
    if not 0 < size <= MAX_HEADER_BYTES:
        raise ValueError('头文件包超过 2 MB 上限，已停止；不会转而下载大型运行库')
    header_root = cache / 'headers'
    generic = header_root / 'usr/include/newlib/c++' / version
    target = generic / 'arm-none-eabi/thumb/v7e-m+dp/hard'
    if not (target / 'bits/c++config.h').is_file():
        downloads = cache / 'downloads'
        downloads.mkdir(exist_ok=True)
        candidates = list(Path('/var/cache/apt/archives').glob(HEADER_PACKAGE + '_*.deb')) + list(downloads.glob(HEADER_PACKAGE + '_*.deb'))
        archive = next((p for p in candidates if p.stat().st_size == size and sha256_file(p) == package['SHA256']), None)
        if archive is None:
            print(f'只下载 C++ 头文件：{size / 1024 / 1024:.2f} MB；不安装多架构 C++ 库。', flush=True)
            run(['apt-get', 'download', HEADER_PACKAGE + '=' + package['Version']], cwd=downloads, timeout=180)
            archive = next((p for p in downloads.glob(HEADER_PACKAGE + '_*.deb') if p.stat().st_size == size and sha256_file(p) == package['SHA256']), None)
        if archive is None:
            raise ValueError('C++ 头文件包校验失败，未使用')
        stage = cache / ('headers-stage-' + str(time.time_ns()))
        try:
            run(['dpkg-deb', '-x', archive, stage], timeout=60)
            if not (stage / 'usr/include/newlib/c++' / version / 'arm-none-eabi/thumb/v7e-m+dp/hard/bits/c++config.h').is_file():
                raise ValueError('头文件包与编译器版本不匹配，请选择完整 ARM 工具链')
            if header_root.exists():
                shutil.rmtree(header_root)
            stage.rename(header_root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    result = env.copy()
    result['CPLUS_INCLUDE_PATH'] = os.pathsep.join([str(generic), str(target)] + ([env['CPLUS_INCLUDE_PATH']] if env.get('CPLUS_INCLUDE_PATH') else []))
    return result


def _fetch_source(path, digest, revision, source_root):
    target = source_root / path
    if target.is_file() and sha256_file(target) == digest:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = 'https://raw.githubusercontent.com/gcc-mirror/gcc/' + revision + '/' + path
    print('获取轻量 GNU 源码：' + path, flush=True)
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('GNU 运行库源码大小或 SHA256 校验失败：' + path)
    temporary = target.with_name(target.name + '.partial')
    temporary.write_bytes(data)
    temporary.replace(target)
    return target


def prepare_h750_runtime(compiler, cache_root, env, run, needs_headers=False):
    catalog = json.loads(CATALOG_PATH.read_text())
    version = subprocess.run([compiler, '-dumpfullversion'], env=env, capture_output=True, text=True, timeout=30).stdout.strip()
    if version != catalog['compiler_version']:
        raise ValueError('内置轻量运行库支持 Ubuntu 22.04 ARM GCC 10.3.1；其它版本请使用自带 C++ 库的完整工具链，不会自动下载大型库包')
    identity = hashlib.sha256((str(Path(compiler).resolve()) + json.dumps(catalog, sort_keys=True)).encode()).hexdigest()[:16]
    cache = Path(cache_root) / ('gcc-' + version + '-m7-' + identity)
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if needs_headers:
            env = _headers(cache, version, env, run)
        marker, library, nano = cache / 'manifest.json', cache / 'libstdc++.a', cache / 'libstdc++_nano.a'
        record = json.loads(marker.read_text()) if marker.is_file() else {}
        valid = (library.is_file() and nano.is_file() and
                 record.get('sha256') == sha256_file(library) == sha256_file(nano))
        if not valid:
            print('仅为 Cortex-M7 / FPv5-D16 / hard-float 编译 GNU C++ 运行库。', flush=True)
            sources = [_fetch_source(path, digest, catalog['revision'], cache / 'source') for path, digest in catalog['files'].items()]
            objects = cache / 'objects'
            objects.mkdir(exist_ok=True)
            compiled = []
            for source in sources:
                if source.suffix != '.cc':
                    continue
                obj = objects / (source.stem + '.o')
                run([compiler, *catalog['flags'], '-I' + str(cache / 'source/libstdc++-v3/libsupc++'),
                     '-c', source, '-o', obj], env=env, timeout=60)
                compiled.append(str(obj))
            ar = str(Path(compiler).with_name('arm-none-eabi-ar'))
            if not Path(ar).is_file():
                ar = shutil.which('arm-none-eabi-ar', path=env.get('PATH'))
            if not ar:
                raise ValueError('缺少 arm-none-eabi-ar，请检查 ARM 工具链')
            temporary = cache / 'new-runtime.a'
            temporary.unlink(missing_ok=True)
            run([ar, 'rcs', temporary, *compiled], env=env, timeout=60)
            temporary.replace(library)
            shutil.copyfile(library, nano)
            record = {'compiler': str(compiler), 'version': version, 'revision': catalog['revision'],
                      'flags': catalog['flags'], 'sha256': sha256_file(library), 'bytes': library.stat().st_size,
                      'source_bytes': sum(source.stat().st_size for source in sources), 'header_limit_bytes': MAX_HEADER_BYTES}
            atomic_json(marker, record)
        print(f'轻量运行库已就绪：{record["bytes"] / 1024:.1f} KiB；后续编译复用缓存。', flush=True)
        return env, '-L' + shlex.quote(str(cache)), {**record, 'path': str(library)}
