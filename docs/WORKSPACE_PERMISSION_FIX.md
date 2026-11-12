# v0.3.2 新工作空间写入权限修复

新建工作空间的 `src/soem_bringup/config/config.yaml` 属于普通用户，但首次 root 启动时，ROS 日志会创建 `.ecat-groundstation/ros-logs/`，连带把之前不存在的 `.ecat-groundstation` 建为 `root:root 755`。

应用任务需要先向该目录中的 `config-backups/` 备份旧 YAML，再写入完整工程快照。普通用户不能在 root 所有的数据目录下创建这些文件，因此写入被拒绝。不是任务参数错误，也不需要重新刷写 EEPROM、隔离 CPU 或创建工作空间。

修复包括：

- 创建工作空间时，由普通用户预先创建 `.ecat-groundstation`。
- root 终端启动前，确保该目录属于工作空间的普通用户。
- 应用或编译时若发现旧版创建的 root 数据目录，通过系统认证执行有限的归属修复，再自动重试原操作。编辑内容保留；取消授权则保持未应用状态。
- 只修复上位机数据目录本身，不递归修改工作空间，不覆盖源码、配置、日志或编译产物，不使用 `chmod 777`。
- 拒绝数据目录符号链接，以及与发起认证用户不匹配的工作空间。

现有工作空间也可通过软件的后台入口修复（桌面系统认证，不传递或保存密码）：

```bash
pkexec /usr/bin/python3 -I -u /path/to/ecat_groundstation/setup_worker.py \
  repair-workspace-access --workspace /home/your-user/your-workspace
```

修复后，在原任务编辑窗口再次应用修改，再执行普通用户 `colcon build`。

验证：130 项自动化测试通过；软件 `colcon build` 和 wheel 构建成功。已将本机 `EGC_test2/.ecat-groundstation` 从 `root:root 755` 修复为 `hby:hby 755`，并用普通用户在临时子目录验证备份及原子替换。实际任务 YAML 保持不变，现有 ROS 日志保留。
