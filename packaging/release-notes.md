## 中文

- 修复 UU 在 Wine 内自动更新后无法启动桥接的问题：UU 更新器把旧 `bin/` 改名为版本号目录、新载荷解包到新版本号目录后，在 Wine 下没有把新目录改回 `bin/`，导致报错 `Official UU component missing: bin/GameViewerServer.exe`。现在启动时会自动选择最新的版本号载荷目录并补回 `bin` 链接；UU 自己的真实 `bin/` 目录仍然优先，不完整的 `bin/` 会明确报错而不会被删除。（[#28](https://github.com/GaryOAO/UUWay/issues/28)）
- Fedora 44 和 45 的共享 RPM 改为一次打包生成，不再拼合中间安装包。

### 贡献者

感谢 @mlfc666 提交 [#29](https://github.com/GaryOAO/UUWay/pull/29)，修复 UU 自动更新后 `bin/` 缺失导致桥接无法启动的问题，并提供详细的问题分析与真机验证。

## English

- Fix the native bridge failing to start after UU auto-updates under Wine. UU's updater renames the old `bin/` to a version directory and unpacks the new payload into another version directory, but under Wine it never renames the new one back to `bin/`, causing `Official UU component missing: bin/GameViewerServer.exe`. Startup now selects the newest version payload and restores the `bin` link. A real `bin/` directory owned by UU still takes precedence, and an incomplete `bin/` is reported instead of being deleted. ([#28](https://github.com/GaryOAO/UUWay/issues/28))
- Build the shared Fedora 44/45 RPM in a single packaging pass instead of combining intermediate packages.

### Contributors

Thanks to @mlfc666 for [#29](https://github.com/GaryOAO/UUWay/pull/29), fixing the bridge startup failure caused by a missing `bin/` after UU auto-updates, and providing a detailed analysis and validation on the affected host.
