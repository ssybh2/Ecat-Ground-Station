# Ecat GroundStation

Ubuntu 22.04 · ROS 2 Humble · Python 3.10

[▶ 演示视频（MP4）](artifacts/demo.mp4)

## 部署

需已安装 ROS 2 Humble，并使用普通用户在桌面环境中执行：

```bash
sudo apt update
sudo apt install -y git build-essential python3-colcon-common-extensions \
  python3-pyqt5 python3-yaml ros-humble-rosidl-default-generators

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

`custom_msgs` 随工作空间一同编译。之后每次打开新终端，先加载环境再启动：

```bash
source /opt/ros/humble/setup.bash
source ~/ecat_ws/install/setup.bash
ros2 run ecat_groundstation ecat-groundstation
```

[LGPL-2.1-or-later](LICENSE) · [第三方声明](THIRD_PARTY_NOTICES.md)
