<div align="center">

# UUWay

**让网易 UU 远程在 Linux 上成为真正的被控端 —— Wayland 原生、GPU 加速、不再绕道 RDP。**

[English](README.md) · [简体中文](README.zh-CN.md)

![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420?logo=ubuntu&logoColor=white)
![GNOME 46 Wayland](https://img.shields.io/badge/GNOME_46-Wayland-4A86CF?logo=gnome&logoColor=white)
![NVIDIA NVENC](https://img.shields.io/badge/NVIDIA-NVENC-76B900?logo=nvidia&logoColor=white)
![Wine 11](https://img.shields.io/badge/Wine-11-A30000)
![Status](https://img.shields.io/badge/状态-开发者预览-f59e0b)
![License MIT](https://img.shields.io/badge/license-MIT-0ea5e9)

</div>

UU 远程只提供 Windows 被控端。UUWay 让**官方原版** Windows 被控端（磁盘上的文件不做任何修改）运行在 Wine 中，
再把它依赖的每一个 Windows 接口 —— 桌面采集、硬件编码、输入、显示模式、鼠标指针、远程终端、
剪贴板 —— 全部交给 Linux 原生实现。账号、中继、编码与码率协商仍由 UU 自己负责；其余工作都在
Linux 上直接由 GPU 完成。

<p align="center">
  <img src="docs/images/architecture.svg" alt="UUWay 架构" width="900">
</p>

## 亮点

| | |
| --- | --- |
| 🎞️ **零拷贝画面** | Wayland 屏幕共享 → DMA-BUF → Vulkan → D3D11 纹理 → CUDA → NVENC，像素全程不回到内存；1080p 稳定 60 帧。 |
| 🖱️ **原生指针** | 指针按 Windows 的标准方式以 DXGI 指针数据传给 UU，手机上显示的就是你本机的光标主题（箭头、文本、手形、缩放），放大画面也会跟随指针移动。 |
| ⌨️ **输入与输入法** | 鼠标、滚轮、键盘（包括 Win / ⌘ 键）经 libei/uinput 注入；手机输入法的文字通过 Fcitx5 直接提交到当前输入框，中文无障碍。 |
| 🖥️ **显示控制** | 在手机上切换分辨率、刷新率、DPI，会同步到 Mutter；新模式未能进入画面时自动回滚。 |
| 💻 **远程终端** | UU 的远程终端直接打开你的 Linux 登录 shell。PTY 与 UU 之间原样转发字节；离开页面会话仍在，可同时开多个，在 UU 里关闭即结束对应 shell。 |
| 📋 **剪贴板** | 文本在手机与桌面应用之间双向同步；手机上复制的文件和图片只在粘贴时才传输。 |
| 🧩 **不惧升级** | UUWay 实现的是公开的 Windows 接口，而不是 UU 内部的偏移地址。UU 从 4.39 升级到 4.42 只改了一处脚本，无需任何二进制补丁。 |
| 🛠️ **控制台** | 一个小巧的 GTK 应用，查看服务状态、调节鼠标与滚轮速度、重启 UU。 |

## 工作原理

UUWay 是一层**适配层**：UU 照常调用它在任何一台 Windows 电脑上都会调用的 API，而每一次调用
都落到对应的 Linux 实现上。

| UU 调用（Windows） | UUWay 的实现（Linux） |
| --- | --- |
| `IDXGIOutputDuplication` 取帧 | PipeWire 屏幕共享经 Vulkan/DXVK 导入为 D3D11 纹理 |
| DXGI 指针位置与 `GetFramePointerShape` | 屏幕共享的光标元数据（画面本身不含指针） |
| `NvEncodeAPICreateInstance` / NVENC 函数表 | 同一块 GPU 上的 Linux CUDA + NVENC |
| `SendInput` | Wayland 会话中的 libei / uinput |
| `KEYEVENTF_UNICODE` 文字输入 | Fcitx5 插件把文字提交到焦点输入框 |
| `ChangeDisplaySettingsEx`、DPI 查询 | Mutter 显示配置 + 回滚守护 |
| 终端所用的 `conpty.dll` 与 `powershell.exe` | 支持持久命名会话的 PTY broker |
| Windows 剪贴板 | Wine ⇄ Xwayland 剪贴板桥，由 Mutter 同步给 Wayland 应用 |

<p align="center">
  <img src="docs/images/video-pipeline.svg" alt="画面链路" width="900">
</p>

更多细节见[设计说明](docs/design.md)（英文）。

## 运行要求

- Ubuntu 24.04，GNOME 46 **Wayland** 会话，且桌面已登录（无显示器的主机建议插一个显示器诱骗器）
- 支持 NVENC 的 **NVIDIA** 显卡及官方驱动（开发环境为 RTX 3090、驱动 580）
- WineHQ stable 11，安装在 `/opt/wine-stable`
- UU 远程官方 Windows 安装包，以及一个 UU 账号
- 构建工具：gcc、mingw-w64、winegcc、meson/ninja（DXVK、Mutter）、Rust（控制台）

## 快速开始

UUWay 目前是**开发者预览版**：它在开发机上每天都在使用，但安装需要从源码构建，还没有一键安装包。

```bash
git clone https://github.com/GaryOAO/UUWay.git && cd UUWay
```

然后按照[构建与安装指南](docs/build.md)（英文）操作，大致步骤：

1. 把 UU 安装到独立的 Wine 前缀，并登录一次账号。
2. 构建原生运行时（DXVK 采集，DXGI/NVENC/显示/输入后端，辅助程序）。
3. 授权一次屏幕共享，保存恢复令牌。
4. 打包运行时并安装用户服务。
5. 用手机连接。

## 当前状态

| 功能 | 状态 |
| --- | --- |
| 画面、输入、输入法、指针、分辨率切换 | ✅ 日常使用中 |
| 远程终端（持久会话） | ✅ 日常使用中 |
| 剪贴板：文本 | ✅ 双向可用 |
| 剪贴板：从手机复制文件 | ✅ 粘贴时才传输 |
| 剪贴板：复制文件到手机 | 🧪 已实现，待手机端验收 |
| UU 4.42.0.2770 | ✅ 无需补丁即可运行（[审计记录](docs/releases/4.42.0.2770-native-review.md)） |
| 超级屏（虚拟显示器） | ❌ 依赖 Windows IddCx 内核驱动，Wine 无法加载 |
| 1440p/4K 60Hz | ⚠️ 受显示器（或诱骗器）所提供的刷新率限制 |

## 目录结构

```
src/        原生后端（Linux 侧与 Windows/Wine 侧）及辅助程序
scripts/    服务、打包、构建与版本审计工具
patches/    DXVK 采集、Mutter 帧节奏、Portal 会话生命周期补丁
config/     固定版本的构建依赖与 udev 规则
systemd/    用户服务模板
native/     控制台（Rust + GTK）
tests/      单元测试与探针
docs/       设计说明、构建指南、版本审计
```

## 免责声明

UUWay 是独立的非官方项目，与网易无关，也未获得网易的认可。"UU"、"UU 远程"为其各自所有者的商标。
UUWay 不分发任何 UU 程序文件，客户端需由你自行从官方渠道安装。

## 致谢

UUWay 源于 Lachlan Chen 的 [uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge)
（MIT 许可），并以原生 Wayland 链路取代了其中的 RDP 中继设计。项目同样建立在
[DXVK](https://github.com/doitsujin/dxvk)、[Wine](https://www.winehq.org/)、PipeWire 与 Mutter 之上。

## 许可证

[MIT](LICENSE)
