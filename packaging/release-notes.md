## 中文

- 避免升级时重复启动 UU；监测真实画面服务进程，退出后自动恢复，更新进度显示健康检查状态。
- 修复回滚恢复文件和目录权限的问题，保留更新前的运行时，不再用旧安装器覆盖已恢复的配置。
- 多显示器独立采集与模式切换，新增超级屏创建、独占、退出恢复及异常清理。
- 控制台集中管理显示器和软件更新，支持手动/自动检查；确认后才下载安装。
- 修复超级屏切换时失效的显示确认事务阻断采集，保留原生光标元数据；超级屏仍可能出现双光标。
- 清理实验性 GPU 光标抑制和内容追踪，归档旧 VKMS 实验，更新安装流程与中英文文档。
- 同时提供 NVIDIA NVENC 和 AMD VA-API 后端。Intel 单显卡、镜像布局、旋转及 GDM 登录界面仍不支持。

## English

- Avoid starting UU twice during upgrades. Recover when the actual video server exits, and show health-check progress during updates.
- Restore rollback files atomically without changing directory permissions. Keep the previous runtime instead of overwriting its restored configuration with the old installer.
- Independent multi-display capture and mode changes, with Super Screen creation, exclusive mode, restoration and crash cleanup.
- Console display management and manual/automatic update checks; installation always requires confirmation.
- Fix stale display confirmations blocking capture after Super Screen transitions. Preserve native cursor metadata; duplicate Super Screen cursors remain a known issue.
- Remove experimental GPU cursor suppression and content tracing, archive the old VKMS experiment, and update setup and documentation.
- Includes NVIDIA NVENC and AMD VA-API. Intel-only encoding, mirroring, rotation and GDM capture remain unsupported.
