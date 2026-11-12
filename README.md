# Ecat GroundStation

Ubuntu 22.04 · ROS 2 Humble · Python 3.10

![Ecat GroundStation 演示](artifacts/demo.gif)

## 部署

需已安装 ROS 2 Humble。打开未加载其他工作空间的新终端，以普通用户在桌面环境中执行：

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git build-essential cmake python3-colcon-common-extensions python3-setuptools \
  python3-pyqt5 python3-yaml policykit-1 gnome-terminal fonts-noto-cjk \
  ros-humble-rclpy ros-humble-rosidl-runtime-py \
  ros-humble-rosidl-default-generators ros-humble-geometry-msgs ros-humble-sensor-msgs \
  ros-humble-ament-cmake-auto libfmt-dev libyaml-cpp-dev

mkdir -p ~/ecat_ws/src
cd ~/ecat_ws/src
git clone --recursive https://github.com/AIMEtherCAT/EcatV2_Master.git
git clone https://github.com/ssybh2/Ecat-Ground-Station.git ecat_groundstation

cd ~/ecat_ws
source /opt/ros/humble/setup.bash
colcon build --packages-up-to ecat_groundstation
source install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

上述命令编译上位机和 `custom_msgs`。之后每次打开新终端，先加载环境再启动：

```bash
source /opt/ros/humble/setup.bash
source ~/ecat_ws/install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

[LGPL-2.1-or-later](LICENSE) · [第三方声明](THIRD_PARTY_NOTICES.md)
