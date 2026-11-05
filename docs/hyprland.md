# Hyprland 实验支持

支持单屏采集和基础键鼠控制，受条件限制暂未做真机测试。欢迎提交
[Issue](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml)
反馈兼容性，也欢迎提交 [PR](https://github.com/GaryOAO/UUWay/compare) 一起完善。

当前需要一块活动显示器，位于桌面原点，100% 缩放、无旋转，刷新率不超过
144 Hz。暂不支持远程修改分辨率、缩放、多屏和超级屏。

安装 `xdg-desktop-portal-hyprland` 和 UUWay 后，在 Hyprland 终端运行：

```bash
uuway setup
```

按提示登录 UU 并允许共享屏幕。连接时可能需要再次在本机确认共享，暂不适合
无人值守使用。首次安装默认将光标嵌入画面；从 GNOME 切换过来的现有配置，
请在控制台将光标模式设为嵌入式，再运行 `uuway refresh --restart-display`。

手机输入法文字提交需要运行 Fcitx5 并加载 UUWay 插件；基础键鼠控制使用
`/dev/uinput`。建议通过 UWSM 启动 Hyprland，以便用户服务跟随桌面会话运行。

实现使用 `hyprctl -j monitors` 读取显示信息、公共 ScreenCast portal 采集画面，
不修改 Hyprland 配置。当前仅做接口和自动化测试，实际串流效果仍需用户反馈。
