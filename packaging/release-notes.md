## 中文

- 修复 UU 服务启动或升级后设备离线、桥接服务反复重启的问题：在新发现的 UU 服务进程启动后等待 8 秒，再注入原生输入模块，规避 Wine 11 下观察到的启动时序冲突。（#17）
- 等待期间可立即响应停止请求或服务进程退出；输入模块就绪检查、进程存活检测及升级健康检查保持启用。启动就绪时间相应增加约 8 秒。
- 已在受影响设备上验证连续三次启动、90 秒升级健康检查及远端恢复；CI 为 655 项通过、25 项跳过。

## English

- Fix devices staying offline and the bridge repeatedly restarting after UU startup or an upgrade: wait eight seconds after discovering a new UU server process before injecting native input, avoiding the startup timing conflict observed with Wine 11. (#17)
- Shutdown requests and server exit interrupt the wait immediately. Input readiness, process liveness, and upgrade health checks remain enabled. Startup readiness takes about eight seconds longer.
- Validated with three consecutive starts, a 90-second upgrade health soak, and confirmed remote recovery on the affected machine. CI: 655 tests passed, 25 skipped.
