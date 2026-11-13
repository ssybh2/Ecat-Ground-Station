# Ecat GroundStation

Ubuntu 22.04 · ROS 2 Humble · Python 3.10

覆盖从零配置 EtherCAT 的完整流程：CPU 核心隔离、实时内核启用、工作空间创建、H750 与 IMU 固件编译烧录、从站任务配置，以及执行器调试。

![Ecat GroundStation 演示](artifacts/demo.gif)

## 部署

需已安装 ROS 2 Humble。打开未加载其他工作空间的新终端，以普通用户在桌面环境中执行：

源码、编译产物和构建日志统一存放在 home 下的 `~/Ecat-Ground-Station/` 专用文件夹中。

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git build-essential cmake python3-colcon-common-extensions python3-setuptools \
  python3-pyqt5 python3-yaml policykit-1 gnome-terminal fonts-noto-cjk \
  ros-humble-rclpy ros-humble-rosidl-runtime-py \
  ros-humble-rosidl-default-generators ros-humble-geometry-msgs ros-humble-sensor-msgs \
  ros-humble-ament-cmake-auto libfmt-dev libyaml-cpp-dev

mkdir -p ~/Ecat-Ground-Station/src
cd ~/Ecat-Ground-Station/src
git clone --recursive https://github.com/AIMEtherCAT/EcatV2_Master.git
git clone https://github.com/ssybh2/Ecat-Ground-Station.git ecat_groundstation

cd ~/Ecat-Ground-Station
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ecat_groundstation
source install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

上述命令编译上位机和 `custom_msgs`。之后每次打开新终端，先加载环境再启动：

```bash
source /opt/ros/humble/setup.bash
source ~/Ecat-Ground-Station/install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

[LGPL-2.1-or-later](LICENSE) · [第三方声明](THIRD_PARTY_NOTICES.md)
