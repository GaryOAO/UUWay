## UUWay：网易 UU 远程 Linux 被控端

让官方 Windows 版 UU 远程被控端在 Ubuntu 上原生运行：Wayland / PipeWire 零拷贝采集、NVENC 硬件编码、uinput 键鼠、Fcitx5 手机中文输入、剪贴板、文件传输，以及直达 Linux shell 的远程终端。用手机打开 UU 远程，就能像控制 Windows 电脑一样控制你的 Linux 桌面。

### 安装

需要 Ubuntu 24.04、GNOME 46 **Wayland** 会话、支持 NVENC 的 **NVIDIA** 显卡和官方驱动，以及 UU 远程官方 **Windows 版**安装包和一个 UU 账号。

```bash
sudo apt install ./uuway_*_amd64.deb
uuway setup --installer ~/Downloads/UURemote_Setup.exe
```

`uuway setup` 会添加并安装 WineHQ stable 11.0、授权键鼠注入、把 UU 装进独立的 Wine 前缀、授权屏幕共享并启动服务。需要你亲手做三件事：登录 UU 账号、在屏幕共享对话框里选显示器并勾选「记住」、在随后的文字服务对话框里点允许。可以反复运行；出问题先运行 `uuway doctor`。

升级：`sudo apt install ./新版本.deb`，再运行 `uuway refresh`（会重启桥接服务，远程会话会短暂断开）。
卸载：先 `uuway uninstall`，再 `sudo apt remove uuway`。

### 已验证的环境

Ubuntu 24.04 · GNOME 46 Wayland · RTX 3090 · WineHQ stable 11.0 · UU 4.42.0.2770。

其他显卡、驱动和显示器配置还缺少验证，欢迎提交[兼容性报告](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml)，成功和失败的都有帮助。

### 不支持

- 超级屏（虚拟显示器）：依赖 Windows 内核驱动，Wine 无法加载
- 非 NVIDIA 显卡、非 GNOME、X11 会话
- 1440p / 4K 60 Hz 受显示器（或诱骗器）提供的刷新率限制

### 校验与源码

用 `SHA256SUMS` 校验下载：`sha256sum -c SHA256SUMS`。

UUWay 采用 [GNU AGPL-3.0](https://github.com/GaryOAO/UUWay/blob/main/LICENSE)。对应源码就是本版本的 tag；安装包由 `packaging/build-deb.sh` 在干净的 Ubuntu 24.04 容器里从该源码构建，构建所需的第三方源码都按哈希固定。

UUWay 是独立的非官方项目，与网易无关，也未获得网易的认可。"UU"、"UU 远程"为其各自所有者的商标。安装包不含任何 UU 程序文件，客户端需由你自行从官方渠道安装。

---

**English summary.** UUWay runs NetEase's official UU Remote Windows host under Wine and hands every Windows interface it uses to a native Linux implementation (Wayland/PipeWire zero-copy capture, NVENC, uinput, Fcitx5, clipboard, file transfer, a Linux login shell as the remote terminal). Install with `sudo apt install ./uuway_*_amd64.deb`, then run `uuway setup` as your normal user. Verified on Ubuntu 24.04 + GNOME 46 Wayland + RTX 3090; compatibility reports for other setups are welcome. Not included: the UU client itself.
