from io import BytesIO
from pathlib import Path
import hashlib
import json
import shlex
import subprocess

import pytest

from ecat_groundstation import h750_runtime as runtime


def test_source_checksum_and_size_limits_never_accept_wrong_download(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.urllib.request, 'urlopen', lambda *args, **kwargs: BytesIO(b'wrong source'))
    with pytest.raises(ValueError, match='校验失败'):
        runtime._fetch_source('gnu/source.cc', '0' * 64, 'a' * 40, tmp_path)
    assert not (tmp_path / 'gnu/source.cc').exists()
    data = b'x' * (runtime.MAX_SOURCE_BYTES + 1)
    monkeypatch.setattr(runtime.urllib.request, 'urlopen', lambda *args, **kwargs: BytesIO(data))
    with pytest.raises(ValueError, match='大小或 SHA256'):
        runtime._fetch_source('gnu/source.cc', hashlib.sha256(data).hexdigest(), 'a' * 40, tmp_path)


def test_runtime_cache_reuse_and_corrupt_library_rebuild(tmp_path, monkeypatch):
    data = b'// fixture for cache lifecycle; not used as a real runtime\n'
    catalog = {'compiler_version': '10.3.1', 'revision': 'a' * 40, 'flags': ['-mcpu=cortex-m7'],
               'files': {'libstdc++-v3/libsupc++/unit.cc': hashlib.sha256(data).hexdigest()}}
    manifest = tmp_path / 'catalog.json'
    manifest.write_text(json.dumps(catalog))
    monkeypatch.setattr(runtime, 'CATALOG_PATH', manifest)
    monkeypatch.setattr(runtime.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout='10.3.1\n'))
    downloads, commands = [], []
    monkeypatch.setattr(runtime.urllib.request, 'urlopen', lambda url, **kwargs: downloads.append(url) or BytesIO(data))
    compiler = tmp_path / 'bin/arm-none-eabi-g++'
    compiler.parent.mkdir()
    compiler.write_text('fixture')
    compiler.with_name('arm-none-eabi-ar').write_text('fixture')

    def run(command, **kwargs):
        commands.append(command)
        if '-c' in command:
            Path(command[-1]).write_bytes(b'object')
        else:
            assert command[1] == 'rcs'
            Path(command[2]).write_bytes(b'archive')

    env, flags, built = runtime.prepare_h750_runtime(str(compiler), tmp_path / 'runtime with spaces', {}, run)
    assert len(downloads) == 1 and len(commands) == 2
    assert shlex.split(flags) == ['-L' + str(Path(built['path']).parent)]
    runtime.prepare_h750_runtime(str(compiler), tmp_path / 'runtime with spaces', {}, run)
    assert len(downloads) == 1 and len(commands) == 2
    Path(built['path']).with_name('libstdc++_nano.a').write_bytes(b'corrupted')
    runtime.prepare_h750_runtime(str(compiler), tmp_path / 'runtime with spaces', {}, run)
    assert len(downloads) == 1 and len(commands) == 4
    assert Path(built['path']).with_name('libstdc++_nano.a').read_bytes() == b'archive'


def test_header_metadata_rejects_any_package_over_two_megabytes(tmp_path, monkeypatch):
    metadata = 'Package: libstdc++-arm-none-eabi-dev\nVersion: 1\nSize: 347420554\nSHA256: ' + 'a' * 64 + '\n'
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=metadata))
    calls = []
    with pytest.raises(ValueError, match='2 MB'):
        runtime._headers(tmp_path, '10.3.1', {}, lambda *args, **kwargs: calls.append(args))
    assert not calls


def test_unsupported_partial_sdk_never_downloads_a_different_toolchain(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout='14.2.1\n'))
    calls = []
    with pytest.raises(ValueError, match='完整工具链'):
        runtime.prepare_h750_runtime('/sdk/arm-none-eabi-g++', tmp_path, {}, lambda *args, **kwargs: calls.append(args))
    assert not calls
