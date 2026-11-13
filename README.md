# Ecat GroundStation

Ubuntu 22.04 · ROS 2 Humble · Python 3.10

覆盖从零配置 EtherCAT 的完整流程：<br>
CPU 核心隔离、实时内核启用、工作空间创建、H750 与 IMU 固件编译烧录、从站任务配置，以及执行器调试。<br>
已验证的**常见**传感器、执行器：<br>
**超核IMU**<br>
**Dshot600,PWM信号**<br>
**DM电机**
## Releases
Releases界面提供80B以及112B 两款常用的H750固件stable版本
![Ecat GroundStation 演示](artifacts/demo.gif)

## 部署

适用于全新的 Ubuntu 22.04 桌面系统。以普通用户打开终端，依次执行；已有 Humble 可跳过第一步。

### 1. 安装 ROS 2 Humble

按 [ROS 2 官方安装说明](https://docs.ros.org/en/humble/Installation/Ubuntu-Install-Debs.html) 配置，安装满足本软件需求的基础版。

```bash
# UTF-8 环境与 Ubuntu Universe 软件源
sudo apt update
sudo apt install -y locales software-properties-common curl ca-certificates
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
sudo add-apt-repository -y universe

# 官方 ROS 软件源及签名密钥（Ubuntu 22.04 / Jammy）
curl -fL --retry 3 -o /tmp/ros2-apt-source.deb \
  https://github.com/ros-infrastructure/ros-apt-source/releases/download/1.3.0/ros2-apt-source_1.3.0.jammy_all.deb
sudo dpkg -i /tmp/ros2-apt-source.deb

# 全新系统先完成更新，再安装 Humble
sudo apt update
sudo apt upgrade -y
sudo apt install -y --no-install-recommends ros-humble-ros-base
source /opt/ros/humble/setup.bash
ros2 --help
```

若系统更新提示需要重启，请重启后再执行第二步。

### 2. 编译并启动软件

打开未加载其他工作空间的新终端。源码、编译产物和构建日志统一存放在 `~/Ecat-Ground-Station/` 专用文件夹中。

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
