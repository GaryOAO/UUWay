<div align="center">

<img src="docs/images/hero.zh.svg" alt="UUWay — 你的 Linux，随时连接。桌面、终端与文件，都在手边。" width="100%">

[简体中文](README.md) · [English](README.en.md)

**[快速开始](#快速开始)　 / 　[控制台](#本机控制台)　 / 　[功能](#可以做什么)　 / 　[文档](#继续了解)**

开源 · AGPL-3.0 · 开发者预览

</div>

<br>

**让网易 UU 远程连接你的 Linux 桌面。** UUWay 在 Wine 中运行官方 Windows 被控端，将它需要的画面采集、输入和系统接口交给 Linux 原生实现。UU 程序文件保持原样，账号和连接仍由 UU 自己管理。

| 桌面在手边 | 终端一直在线 | 文件自由往来 |
| :--- | :--- | :--- |
| Wayland 原生采集与 NVENC 编码，1080p 可达 60 帧。 | 直接打开 Linux 登录 shell，离开页面后会话仍然保留。 | 双向剪贴板与文件传输，接收目录直接落到 Linux。 |

## 快速开始

目前验证的环境如下。UUWay 仍处于**开发者预览**阶段，日常验证机器为 Ubuntu 24.04 + RTX 3090。

| 准备项 | 要求 |
| :--- | :--- |
| 系统与桌面 | Ubuntu 24.04，GNOME 46 **Wayland**；桌面保持登录 |
| 显卡 | 支持 NVENC 的 **NVIDIA** 显卡和官方驱动 |
| 显示器 | 实体显示器；无显示器的主机可插显示器诱骗器 |
| UU 远程 | 官方 **Windows 被控端安装包**和一个 UU 账号 |

```bash
git clone https://github.com/GaryOAO/UUWay.git
cd UUWay
./install.sh --installer ~/Downloads/UURemote_Setup.exe
```

安装脚本会准备依赖与 Wine、安装 UU、构建原生运行时并启动服务。你需要完成两步：

1. 在弹出的 UU 窗口中**登录账号**。
2. 在屏幕共享对话框中**选择显示器，并勾选「记住」**。

随后打开手机上的 UU 远程，选择这台 Linux 设备即可连接。

中断后用 `./install.sh --from 步骤号` 继续；`./install.sh --dry-run` 可预览安装操作。手动安装、可选的 60 帧帧节奏补丁与排错步骤见[构建与安装指南](docs/build.md)。

## 本机控制台

从应用菜单打开 **UUWay 控制台**。连接状态、日常设置和诊断信息各有自己的位置。

<img src="docs/images/console-overview.png" alt="UUWay 控制台：深蓝侧栏、连接状态与显示、输入、文件三个常用入口" width="100%">

<sub>界面预览：图中的服务状态与显示参数为演示数据。</sub>

| 页面 | 你可以做什么 |
| :--- | :--- |
| **概览** | 查看本机服务状态，启动或重新连接，进入常用操作 |
| **显示与外观** | 调整分辨率、刷新率和缩放；更换客户端里的设备封面 |
| **鼠标与输入** | 调整鼠标、滚轮与滚动方向，选择光标和文字输入方式 |
| **文件传输** | 选择文件或文件夹发送到手机，打开或更改接收目录 |
| **诊断** | 检查各项服务、展开目录映射，生成和复制诊断报告 |

输入速度保存后直接生效；需要重启的设置会集中提示。控制台中的显示切换提供 **30 秒试用确认**，未确认则自动恢复。

## 可以做什么

| 能力 | 支持情况 |
| :--- | :--- |
| 画面串流 | GPU 零拷贝采集 + NVENC 硬件编码，1080p 可达 60 帧 |
| 键盘与鼠标 | 经 libei / uinput 注入，包含 Win / ⌘ 键；支持本机光标主题 |
| 手机文字输入 | 通过 Fcitx5 将中文等文字直接提交到当前输入框 |
| 显示模式 | 控制台与 UU 客户端可选择 Linux 已公布的模式，切换过程带回滚保护 |
| 远程终端 | Linux 登录 shell，多会话常驻；vim、htop 与中文显示正常 |
| 剪贴板 | 文本双向同步 |
| 手机 → Linux 文件 | 粘贴时传输，下载完成后继续粘贴 |
| 其他设备 → Linux 文件 | 接收目录映射到 Linux 下载目录，也可自定义 |
| Linux → 手机文件 | 支持文件与目录，经现有文件桥传输 |

**当前边界：**「超级屏」虚拟显示器依赖 Windows 内核驱动，尚不支持。1440p / 4K 的刷新率受显示器或诱骗器公布的模式限制。

已验证 **UU 4.42.0.2770**，官方程序无需补丁，详见[版本审计记录](docs/releases/4.42.0.2770-native-review.md)。UUWay 实现公开的 Windows 接口，不依赖 UU 内部的固定偏移。

<details>
<summary><strong>远程终端与路径映射的细节</strong></summary>

- 终端字节在 PTY 与 UU 之间原样转发。离开终端页后，会话继续运行；返回时重新绘制。关闭一个终端会结束对应的 shell。
- Wine 的 `C:` 保留为 UU 独立前缀，`Z:` 对应 Linux 根目录；桌面、文档、下载、音乐、图片等用户目录映射到 Linux 的 XDG 目录。
- UU 文件接收目录与配置的 Linux 下载目录对接；其他设备盘符保留 Wine 检测到的 Linux 设备路径。

</details>

## 如何工作

UUWay 是一层适配层。UU 照常调用 Windows API，画面、输入、显示和终端等接口由 Linux 侧承接；账号、中继、编码与码率协商仍交给 UU。

<img src="docs/images/architecture.zh.svg" alt="架构：UU 官方被控端调用的 Windows 接口，由 UUWay 对接 Linux 原生实现" width="100%">

<details>
<summary><strong>查看 GPU 画面链路</strong></summary>

<img src="docs/images/video-pipeline.zh.svg" alt="画面从 Wayland 合成器到 NVENC 编码器，像素保持在显存中" width="100%">

技术细节见[设计说明](docs/design.md)。

</details>

## 继续了解

| 文档 | 内容 |
| :--- | :--- |
| [构建与安装](docs/build.md) | 环境准备、手动部署、可选补丁与故障排查 |
| [设计说明](docs/design.md) | 原生适配层与各条数据链路 |
| [UU 版本审计](docs/releases/4.42.0.2770-native-review.md) | 已验证版本及兼容性检查 |

<details>
<summary><strong>仓库结构</strong></summary>

```text
install.sh  一键安装
src/        Linux 与 Windows/Wine 侧原生后端、辅助程序
scripts/    服务、构建、打包、版本审计与 Python/GTK 控制台
assets/     控制台图标、桌面封面与矢量源文件
patches/    DXVK 采集、Mutter 帧节奏、Portal 生命周期补丁
config/     固定构建依赖与 udev 规则
systemd/    用户服务模板
native/     原生运行时组件
tests/      单元测试与探针
docs/       设计、安装与版本记录
```

</details>

---

**许可证**　[GNU AGPL-3.0](LICENSE)。UUWay 是独立的非官方项目，与网易无关，也未获得网易认可。「UU」「UU 远程」为各自所有者的商标。项目不分发任何 UU 程序文件，请从官方渠道获取客户端。

**致谢**　项目源于 Lachlan Chen 的 [uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge)，以原生 Wayland 链路取代了 RDP 中继设计；同时建立在 [DXVK](https://github.com/doitsujin/dxvk)、[Wine](https://www.winehq.org/)、PipeWire 与 Mutter 之上。
