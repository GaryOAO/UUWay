## 中文

- 为 UU 被控端自身升级增加本地程序快照和失败回退；安装中断、目录缺失或新版启动失败时自动恢复之前的程序。
- 新版必须通过 GUI、服务和输入适配检查，稳定运行 30 秒，并获得当前 UU 进程的真实采集帧确认，才会更新恢复点。采集验证最多等待 3 分钟，未通过则自动回退。
- 升级和回退状态持久保存，服务异常退出或机器重启后继续恢复；目录迁移识别不再依赖具体版本号命名。
- 回退不覆盖账号、下载文件或用户配置，自动重建远程终端入口；保留最近两份程序快照及两次失败程序以便排查。
- `uuway doctor` 可查看恢复点、待验证版本和自动回退结果。增加中断、坏备份、新版适配失败、采集超时及重复回退的回归测试。

自动恢复依赖已建立且完整的本地程序快照。此机制降低升级失败后持续离线的风险，不代表对尚未发布的 UU 版本作出兼容性保证。

## English

- Add local program snapshots and rollback for UU host upgrades. Restore the previous program after an interrupted install, missing program directory or new-version startup failure.
- Require successful GUI, service and input adaptation, 30 seconds of stable operation, and a real capture-frame acknowledgement from the current UU process before replacing the recovery point. Roll back if capture verification does not succeed within three minutes.
- Persist upgrade and rollback state for recovery after process exits or machine restarts. Recognize relocated directories without requiring a numeric version name.
- Preserve accounts, downloads and user configuration during rollback, and restore the remote-terminal entrypoint. Retain the latest two program snapshots and two failed payloads for troubleshooting.
- Report recovery points, pending validation and rollback results through `uuway doctor`. Add regression tests for interruptions, corrupt backups, rejected adaptation, capture timeouts and repeated rollback.

Automatic recovery requires an existing, intact local program snapshot. It reduces prolonged offline failures after upgrades; it does not guarantee compatibility with unreleased UU versions.
