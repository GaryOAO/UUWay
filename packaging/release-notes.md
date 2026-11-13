## 中文

- 提高原生桥接在高负载和 UU 4.46 启动较慢时的稳定性，延长显示拓扑查询、输入就绪检测和启动等待时间。（#23）
- 为显示、文本和桥接用户服务补齐会话 D-Bus 环境，并为直接启动增加环境回退。
- 将桥接服务内存软限制/硬限制提高至 5/6 GiB，允许最多 2 GiB swap，减少 4K/NVENC 与内置 WebView 同时运行时的内存回收停顿。
- 修复启动等待成功后跳过服务端输入 hook 的逻辑；重试保留同一进程句柄，并在服务端退出或收到停止请求时终止。
- 补充正常启动、失败重试、超时预算、进程退出及取消的回归测试。

### 贡献者

感谢 [@lihenga](https://github.com/lihenga) 提交 [#23](https://github.com/GaryOAO/UUWay/pull/23)，改进原生桥接在高负载下的启动稳定性，并提供问题复现与真机验证。

## English

- Improve native bridge startup under load and during slower UU 4.46 initialization, with longer display-topology, input-readiness and startup deadlines. (#23)
- Supply session D-Bus environment variables to display, text and bridge user services, with an environment fallback for direct launches.
- Raise the bridge memory high/max limits to 5/6 GiB and allow up to 2 GiB swap, reducing reclaim stalls with 4K/NVENC and the embedded WebView.
- Fix the server input hook being skipped after a successful startup grace period. Keep retries tied to the same process handle and stop when the server exits or shutdown is requested.
- Add regression coverage for normal startup, retries, timeout budgets, process exit and cancellation.

### Contributors

Thanks to [@lihenga](https://github.com/lihenga) for [#23](https://github.com/GaryOAO/UUWay/pull/23), improving native bridge startup under load and providing reproduction details and validation on the affected host.
