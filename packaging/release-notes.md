## 中文

- 新增 Debian 13、Fedora 44 / 45、Arch Linux 原生安装包；Fedora 44 / 45 共用一个 RPM。（#19）
- 新增 Hyprland 单屏实验支持，暂不支持修改显示模式和超级屏。
- 安装向导可自动下载网易 UU 官方安装包；Wine 11.0 使用官方预编译包。
- 自动更新按发行版选择安装包，并保留更新前备份、健康检查和失败回滚。

以上新环境为实验性支持，受条件限制暂未做真机测试。欢迎提交 Issue 反馈兼容性，也欢迎提交 PR 一起完善。

## English

- Add native Debian 13, Fedora 44 / 45 and Arch Linux packages, with one shared Fedora RPM. (#19)
- Add experimental single-monitor Hyprland support; display-mode changes and Super Screen remain unavailable.
- Download the official NetEase UU installer during setup and use official prebuilt Wine 11.0 packages.
- Select distribution-specific update assets, with a previous-package backup, health checks and rollback.

These new environments are experimental and have not been tested on real hardware. Compatibility reports and code contributions are welcome.
