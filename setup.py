from setuptools import setup

setup(
    name='ecat_groundstation',
    version='0.4.4',
    packages=['ecat_groundstation'],
    package_data={'ecat_groundstation': ['message_fields.json', 'eeprom_native.c', 'h750_runtime_sources.json']},
    description='AIM EtherCAT desktop configuration and ROS 2 commissioning station',
    python_requires='>=3.10',
    install_requires=['PyQt5>=5.15', 'PyYAML>=5.4'],
    license='LGPL-2.1-or-later',
    entry_points={'console_scripts': ['ecat-groundstation=ecat_groundstation.app:main']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/ecat_groundstation']),
        ('share/ecat_groundstation', ['package.xml']),
    ],
)
