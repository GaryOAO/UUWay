## 中文

- 修复 AMD VA-API 后端在显卡缺少 ROI（空间 AQ）能力时被误判为不可用的问题。（#18）
- H.264/HEVC 硬件编码现在会在没有 32 个 QP-delta ROI 区域的驱动上继续工作，并自动关闭空间 AQ；支持 ROI 的驱动仍保留原有空间 AQ 优化。
- 增加 ROI 能力降级回归测试，并更新 AMD 后端说明。

## English

- Fix AMD VA-API setup being rejected when the GPU lacks ROI (spatial AQ) support. (#18)
- H.264/HEVC hardware encoding now continues on drivers without 32 QP-delta ROI regions and disables spatial AQ automatically; drivers with ROI support keep the existing spatial AQ optimization.
- Add a regression test for the ROI capability fallback and update the AMD backend documentation.
