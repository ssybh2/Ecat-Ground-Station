# v0.3.3 精简 IMU 编译依赖

旧安装命令把 ARM C++ 库作为 APT 推荐依赖带入，导致约 443 MB 下载、2.6 GB 安装占用。其中 `libstdc++-arm-none-eabi-newlib` 下载包约 347 MB，安装占用约 1.7 GB。

已检查 hipnucimu 的当前源码：虽然 CMake 声明了 CXX，实际固件源文件为 C 和汇编，最终以 C 编译器链接，不需要 C++ 标准库。

## 新安装方式

- IMU：`cmake make gcc-arm-none-eabi libnewlib-arm-none-eabi python3-venv`。Binutils、Newlib 头文件由必要依赖自动带入。
- 烧录：`openocd`，从烧录窗口单独安装。
- 使用 `--no-install-recommends --no-upgrade`，只安装尚未安装完成的包。工具齐全时不运行 APT 更新或安装。
- IMU 构建明确使用 Unix Makefiles，不依赖 Ninja。不再通过这一按钮安装主机 build-essential、usbutils 或大型 ARM C++ 库。
- 系统 CMake 版本不足时仍使用当前用户的独立 CMake 环境；不会替换系统版本。

Ubuntu 22.04 的干净安装预演中，新增包缩减为 ARM GCC、Binutils、Newlib 和 Newlib 头文件，合计约 94 MB（加上目标机器缺失的基础工具）。已下载到 APT 缓存的包可以复用。

## 中断处理

修复后台 worker 的 SIGTERM 异常类型：旧代码抛出的 `InterruptedError` 会被 `selectors` 当成系统调用中断吞掉，新代码使用专用异常退出等待并清理子进程。安装程序处于解包或配置阶段时，不通过直接杀进程来终止。

本次旧任务第一次中断未生效，随后完成了安装，不能宣称大包下载已被提前取消。清理仅针对这次额外安装的 ARM C++ 库及 Ninja，不运行全局 `autoremove`。

## 验证

134 项测试通过，软件包和 wheel 构建成功。用不含 `libstdc++.a` 的临时工具链真实编译了 IMU 1，固件 SHA256 与此前完整工具链构建一致；没有烧录硬件。结果见 [lean-toolchain-validation.json](../artifacts/lean-toolchain-validation.json)。另外使用真实后台进程验证了 SIGTERM 能退出管道等待并回收子进程。
