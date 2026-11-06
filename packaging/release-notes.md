## 中文

- 支持 UU 4.46.0.4716。
- 修复 UU 内置升级重命名 `bin` 后被 UUWay 中断、导致被控端一直离线的问题。识别原有适配链接的目录迁移，等待安装完成，再清理旧链接并自动重启。
- UU 更新后自动恢复缺失的远程终端入口；保留已有的第三方文件。
- 增加更新等待、超时、取消、旧目录清理和服务重启的回归测试；已重放官方 4.42 → 4.46 升级提交并验证自动恢复。

已被旧版 UUWay 中断的 UU 更新，需要先停止 `uu-native-bridge.service` 并重新运行官方安装包完成安装，再启动服务。本次修复防止后续内置更新再次被中断。

## English

- Support UU 4.46.0.4716.
- Fix UUWay interrupting UU's in-app upgrade after it renames `bin`, leaving the host offline. Recognize relocation of the original bridge links, wait for the installer to finish, then clean up and restart automatically.
- Restore the missing remote-terminal entrypoint after UU updates while preserving existing third-party files.
- Add regression tests for installer waits, timeouts, cancellation, retired-directory cleanup and service restarts. Replay the official 4.42 → 4.46 upgrade commit and verify automatic recovery.

An update already interrupted by an older UUWay needs the bridge service stopped and the official UU installer rerun before restarting the service. This fix prevents subsequent in-app updates from being interrupted again.
