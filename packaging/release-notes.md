## 中文

- 修复控制台在 KDE/Breeze 等桌面主题下窗口按钮和其他图标可能显示破碎的问题：控制台在自身进程内使用 Adwaita 控件和图标，覆盖标题栏、导航、操作按钮及原生对话框。（#22）
- 替换 Adwaita 中不存在的诊断页图标，并为 Ubuntu、Debian、Fedora 和 Arch 安装包补齐图标主题与 SVG 加载依赖。
- 保留系统窗口按钮布局、字体和深色偏好，不修改系统 GTK/KDE 配置。显式设置的 `GTK_THEME` 环境变量仍可覆盖控件主题。
- 增加异常主题、明暗模式、不同图标尺寸及 HiDPI 回归测试；安装包检查现在实际启动并绘制控制台。

## English

- Fix fragmented window controls and other console icons under desktop themes such as KDE/Breeze. Use Adwaita widgets and icons within the console process, covering the title bar, navigation, action buttons and native dialogs. (#22)
- Replace a diagnostics icon absent from Adwaita and declare icon-theme and SVG-loader dependencies for Ubuntu, Debian, Fedora and Arch packages.
- Preserve window-button layout, font and dark-mode preferences without writing system GTK/KDE settings. An explicit `GTK_THEME` environment override still takes precedence over the widget theme.
- Add regression coverage for a faulty theme, light/dark modes, icon sizes and HiDPI. Package installation checks now open and render the console.
