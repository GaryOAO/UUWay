## UUWay：网易 UU 远程 Linux 被控端

### 1.1.2 更新

- **修复 PC 端远程终端打开约一秒后闪退。** 终端窗口初始化时，控制端可能先发一次不合理的窗口大小（[#2](https://github.com/GaryOAO/UUWay/issues/2) 里从 PC 端观察到 120×9001，随后才是 120×30）。以前桥接服务把这一帧当作协议错误，直接断开连接，整个终端随之结束。现在越界的窗口大小会被忽略，沿用上一次的大小；长度不对的畸形帧仍然会断开。
- **UU 先撤掉终端面板时，还连着的终端不再被连带结束。** 以前只要 UU 这一侧的面板进程没了，桥接服务就立刻结束 shell，哪怕手机或电脑上还开着这个终端。现在只要还有终端连着，shell 就保留；面板和终端都走了才结束。关闭 UU 里的终端、在 shell 里 `exit` 的行为不变。
- 这两处都有桥接服务的单元测试覆盖，但还没有在真实的 PC 客户端上端到端验证过。如果升级后 PC 端终端仍然闪退，或重连后回不到原来的 shell，欢迎在 [#2](https://github.com/GaryOAO/UUWay/issues/2) 或新 Issue 里附上 `uu-terminal-proxy.trace` 和桥接服务的日志。
- 感谢 [@dororo42](https://github.com/dororo42) 报告并定位这个问题。

### 1.1.1 更新

- **桌面崩溃或注销后，UU 不再一直离线。** 1.1.0 的开机自动登录只在 GDM 启动时触发：GNOME Shell 一旦崩溃，机器就停在登录界面，UU 离线，直到有人登录。现在 `uuway autologin on` 还会开启 GDM 的 `TimedLogin`，登录界面停留约 10 秒就自动重新登录，UU 随桌面回来。已经自己开过开机自动登录的人，`on` 只补这一层，`off` 也只撤销 UUWay 自己加的那部分。`uuway doctor` 会在缺这一层时给出警告，`uuway autologin status` 会分别显示两层。
  代价：手动注销后 10 秒也会自动重新登录；想在登录界面停留，先 `uuway autologin off`。
- **`uuway refresh --restart-services` 现在会先警告。** 重启文字服务时，它会向 GNOME 无障碍总线重新注册监听；桌面运行很久之后，这在一次实测里让 GNOME Shell 崩溃（崩溃点在 GNOME 的无障碍桥，不在 UUWay 或 Mutter）。不带这个参数的 `uuway refresh` 只重启桥接服务，不受影响。

### 1.1.0 更新

- **显卡高负载时，远程画面跳回几秒前的旧画面：采集端先修好一半。** 以前只要有别的程序占满显卡（渲染、训练、OCR），画面就会在当前帧和两三秒前的帧之间来回跳。现在采集端遇到帧积压时只送最新的一帧，旧帧直接放回，画面不会越拖越后。
  另一半在合成器：NVIDIA 驱动不给 DMA-BUF 挂隐式同步，Mutter 46 又只 flush 不等 GPU 画完，采集端可能读到缓冲区里的旧内容。修复是一个 Mutter 补丁，需要按 [构建与安装指南](https://github.com/GaryOAO/UUWay/blob/main/docs/build.md) 自己构建私有 Mutter（安装包里不含）。实测同样的满载下，倒退帧从约 24% 降到 0，代价是满载录屏时采集帧率约降 20%。
- **新增 `uuway autologin on|off|status`**：给只用远程的人。重启或断电恢复后 GDM 自动登录，UU 随桌面自动上线。这是安全开关，`uuway setup` 和 `--yes` 都不会替你开启；`uuway doctor` 会提示它没开。UU 在 GDM 登录界面是离线的，因为 UUWay 只在你的桌面会话里工作（1.1.1 起，登录界面只停留约 10 秒）。
- 新增两个不改动桌面的诊断工具：`tests/probes/dmabuf_implicit_fence_probe.c`（检查你的驱动会不会读到旧缓冲区）和 `tests/probes/capture_order_probe.py`（在真实录屏里统计画面倒退次数）。

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

**English summary (1.1.2).** The PC terminal no longer flash-quits: an out-of-range resize frame (reported: 120x9001 right before 120x30, issue #2) is ignored instead of dropping the connection, and a viewer that is still attached keeps its shell when UU removes its pane first. Both are covered by broker unit tests; they have not been verified end to end against a real PC client yet. Thanks to @dororo42 for the report.

**English summary (1.1.1).** After a desktop crash or a logout UU no longer stays offline: `uuway autologin on` now also enables GDM's `TimedLogin`, so the login screen logs your account in again after about 10 seconds. `uuway refresh --restart-services` now warns before restarting the text service, which crashed a long-running GNOME Shell once in testing.

**English summary (1.1.0).** Under heavy GPU load the remote picture no longer falls behind: the capture worker now delivers only the newest queued frame. The other half of the stale-frame fix is a Mutter patch that you build yourself (see docs/build.md). New: `uuway autologin on|off|status` for remote-only machines (opt-in; never enabled by `--yes`).

**English summary.** UUWay runs NetEase's official UU Remote Windows host under Wine and hands every Windows interface it uses to a native Linux implementation (Wayland/PipeWire zero-copy capture, NVENC, uinput, Fcitx5, clipboard, file transfer, a Linux login shell as the remote terminal). Install with `sudo apt install ./uuway_*_amd64.deb`, then run `uuway setup` as your normal user. Verified on Ubuntu 24.04 + GNOME 46 Wayland + RTX 3090; compatibility reports for other setups are welcome. Not included: the UU client itself.
