"""Check exported tags against the same yaml-cpp library as the C++ master."""
import shutil
import subprocess
from pathlib import Path

import pytest

from ecat_groundstation.config import Project
from ecat_groundstation.schema import SPECS
from ecat_groundstation.commands import MESSAGE_FIELDS


def test_message_definitions_match_installed_ros_overlay():
    util = pytest.importorskip('rosidl_runtime_py.utilities')
    pytest.importorskip('custom_msgs.msg')
    for name, expected in MESSAGE_FIELDS.items():
        cls = util.get_message('custom_msgs/msg/' + name)
        actual = cls.get_fields_and_field_types()
        aliases = {'sequence<uint16>': 'uint16[]', 'float': 'float32', 'double': 'float64'}
        normalized = {key: aliases.get(typ, typ) for key, typ in actual.items()}
        assert normalized == expected, name


def test_yaml_cpp_reads_generated_tags_and_counts(tmp_path):
    if not shutil.which('g++') or not Path('/usr/include/yaml-cpp/yaml.h').exists():
        pytest.skip('yaml-cpp development files not installed')
    source = tmp_path / 'check.cpp'
    source.write_text(r'''
#include <yaml-cpp/yaml.h>
#include <map>
#include <string>
#include <cassert>
#include <cmath>
int main(int argc, char **argv) {
    auto root = YAML::LoadFile(argv[1]);
    const std::map<std::string,int> widths = {{"!uint8_t",1},{"!int8_t",1},
       {"!uint16_t",2},{"!int16_t",2},{"!uint32_t",4},{"!int32_t",4},{"!float",4}};
    for (auto entry : root["slaves"]) for (auto slave : entry) {
        auto body = slave.second;
        assert(body["sdo_len"].Tag() == "!uint16_t");
        assert(body["task_count"].Tag() == "!uint8_t");
        assert(body["task_count"].as<int>() == body["tasks"].size());
        int count = 1;
        for (auto row : body["tasks"]) for (auto app : row) for (auto field : app.second) {
            auto key = field.first.as<std::string>();
            auto val = field.second;
            auto tag = val.Tag();
            if (tag == "!std::string") assert(!val.as<std::string>().empty());
            else if (tag == "!float") assert(std::isfinite(val.as<float>()));
            else if (tag == "!uint8_t") (void) val.as<uint8_t>();
            else if (tag == "!uint16_t") (void) val.as<uint16_t>();
            else if (tag == "!uint32_t") (void) val.as<uint32_t>();
            else assert(false);
            if (key.rfind("sdowrite_", 0) == 0) count += widths.at(tag);
        }
        assert(count == body["sdo_len"].as<int>());
    }
}
''')
    executable = tmp_path / 'check'
    subprocess.run(['g++', '-std=c++17', str(source), '-lyaml-cpp', '-o', str(executable)], check=True)
    config = tmp_path / 'config.yaml'
    config.write_text(Project.demo().export_yaml())
    subprocess.run([str(executable), str(config)], check=True)


def test_dji_partial_init_footprint_guard():
    from ecat_groundstation.config import Slave, Task
    slave = Slave('123')
    slave.tasks = [Task.create(5, '123', 1), Task.create(6, '123', 2)]
    project = Project([slave])
    assert any('初始化写入 12 B' in e for e in project.validate())
    slave.tasks.reverse()
    assert not project.validate()


def test_backend_init_must_fit_even_with_packed_pdo():
    from ecat_groundstation.config import Slave, Task
    # Synthetic output allocation: external PWM 16 channels = 32 B each,
    # plus board PWM 8 B, then partial DJI group 3 B. Total 75 B;
    # backend init would reach byte 84 despite that packed length.
    slave = Slave('123')
    slave.tasks = [Task.create(7, '123', 1), Task.create(7, '123', 2),
                   Task.create(6, '123', 3), Task.create(5, '123', 4)]
    slave.tasks[0].values.update(sdowrite_channel_num=16, sdowrite_uart_id=1)
    slave.tasks[1].values.update(sdowrite_channel_num=16, sdowrite_uart_id=4)
    p = Project([slave])
    assert slave.allocations()[2] == 75
    assert any('末尾空间不足' in e for e in p.validate())
