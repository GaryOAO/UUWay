## 中文

- 修复首次 `uuway setup` 前运行 `uuway update` 检查更新导致后续启动失败的问题：新建的 `~/.local/state/uurb` 目录使用私有权限 `0700`，更新缓存文件保持 `0600`。（#16，感谢 @mikezom）
- 已有状态目录的权限保持不变；此修复不会自动修复旧版本已创建的非私有目录。
- 更新中英文兼容性表格，补充 Debian 社区反馈，并完善兼容性反馈模板。

## English

- Fix startup failing after running `uuway update` before the first `uuway setup`: create `~/.local/state/uurb` with private permissions (`0700`) while keeping the update cache file at `0600`. (#16, thanks to @mikezom)
- Existing state directory permissions are preserved; this fix does not automatically repair non-private directories created by earlier versions.
- Update the Chinese and English compatibility tables, add Debian community feedback, and improve the compatibility report template.
