"""Recorded, narrow build adaptations applied only to disposable H750 snapshots."""
from pathlib import Path
import re

from .setup_core import sha256_file


KNOWN_TASK_HEADER = '5d172f21167dbe121863da482307c29ca5cc63851a6e34dfef14f7e2e0a25c33'
DEBUG_MARKER = '# EGC_H750_COMPACT_DEBUG'


def prepare_h750_snapshot(source, build_type):
    source = Path(source)
    changes = []
    if build_type == 'Debug':
        cmake = source / 'CMakeLists.txt'
        content = cmake.read_text()
        if DEBUG_MARKER not in content:
            before = sha256_file(cmake)
            # Directory compile options precede target creation and are inherited
            # by HAL/FreeRTOS subdirectories. They follow -O0 in the compiler argv.
            block = '\n' + DEBUG_MARKER + '\nadd_compile_options("$<$<CONFIG:Debug>:-Os>" "$<$<CONFIG:Debug>:-g3>")\n'
            minimum = re.search(r'cmake_minimum_required\s*\([^)]*\)', content, re.I)
            index = minimum.end() if minimum else 0
            cmake.write_text(content[:index] + block + content[index:])
            changes.append({'file': 'CMakeLists.txt', 'reason': 'Debug 保留 -g3，采用 -Os 以适配 H750 128 KiB Flash',
                            'before': before, 'after': sha256_file(cmake)})
    header = source / 'Application/task_defs.hpp'
    if header.is_file() and sha256_file(header) == KNOWN_TASK_HEADER:
        definitions = '\n'.join(p.read_text(errors='replace') for p in source.rglob('*') if p.suffix in ('.cpp', '.cc', '.cxx'))
        # Custom forks that supply base implementations do not need this fix.
        if not re.search(r'(?:CanRunnable\s*::\s*can_recv|I2CRunnable\s*::\s*i2c_recv)\s*\(', definitions):
            content = header.read_text()
            for declaration in ('virtual void can_recv(FDCAN_RxHeaderTypeDef *rx_header, uint8_t *rx_data);',
                                'virtual void i2c_recv();'):
                if content.count(declaration) != 1:
                    raise ValueError('已知 H750 回调声明与预期不一致，未自动修改')
                content = content.replace(declaration, declaration[:-1] + ' = 0;')
            header.write_text(content)
            changes.append({'file': 'Application/task_defs.hpp', 'reason': '修复已知上游无定义的基类回调，将接口声明为纯虚函数',
                            'before': KNOWN_TASK_HEADER, 'after': sha256_file(header)})
    return changes
