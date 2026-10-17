## UUWay 1.3.0

### 中文

- **发布包同时携带 NVIDIA 和 AMD 两套后端并自动选择。** 安装时优先验证 NVIDIA NVENC，失败后验证 AMD VA-API H.264/HEVC；首次成功的后端会写入用户运行时配置，刷新不会因环境短暂变化悄悄切换。可用 `UURB_BACKEND=nvidia|amd` 做诊断覆盖。
- AMD 使用 host-memory copy 路径，Intel 仍不支持。UUWay 只检测驱动和编码能力，不会自动安装或切换显卡驱动。
- AMD 后端由 [@AliyahZombie](https://github.com/AliyahZombie) 通过 [PR #4](https://github.com/GaryOAO/UUWay/pull/4) 贡献，感谢参与。
- 发布物没有签名，仍提供同一 Release 内的 `SHA256SUMS`。

### English

- The package ships both NVIDIA and AMD backends. Setup probes NVIDIA NVENC first, then AMD VA-API H.264/HEVC, and persists the first working choice. Use `UURB_BACKEND=nvidia|amd` for diagnostics.
- AMD uses a host-memory copy path; Intel-only systems are not supported. UUWay detects driver and encoder capability but does not install or switch GPU drivers.
- The AMD backend was contributed by [@AliyahZombie](https://github.com/AliyahZombie) in [PR #4](https://github.com/GaryOAO/UUWay/pull/4).
- The release is unsigned; `SHA256SUMS` is included for integrity checks.
