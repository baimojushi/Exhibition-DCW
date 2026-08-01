# 第二阶段规格：固定采集配置 + 恢复状态机 + 协议与置信度设计（基线，2026-07-31）

> 来源：用户在《HP60C SDK 能力审计》交付后的后续需求规格。本文件为需求基线，实现与实验均以本文件为准。

## 1. 固定采集配置

第一阶段不再尝试动态切换分辨率、帧率或时间戳类型。每次采集会话启动前完成一次完整配置，采集中保持不变：

```yaml
camera:
  model: HP60C
  rgb_width: 640
  rgb_height: 480
  depth_width: 640
  depth_height: 480
  fps: 15
  depth_format: uint16_le
  depth_unit_mm: 1
  registration_enabled: true
  timestamp_type: system_clock
```

建议第一版固定为 15 FPS。待长时间帧间隔测试完成后，再评估 25 FPS 或 30 FPS。

## 2. 相机状态机（以"重开设备"为恢复单位）

不要把 StopStream → StartStream 视为可靠恢复方式。业务侧统一采用：

```
帧停滞
→ StopStream
→ CloseCamera
→ 释放旧句柄
→ 重新枚举
→ OpenCamera
→ 设置 StreamParam
→ StartStream
→ 等待有效帧
→ 重新读取并校验标定
```

推荐状态机：

```
Disconnected → Enumerating → Opening → Configuring → Streaming → Stalled → Reconnecting → Streaming
```

出现以下任一条件时进入 Stalled：
- 超过 500 ms 没有新帧（15 FPS 约 7 帧缺失，足以区分普通抖动与设备停流）；
- 连续帧时间戳不增长；
- RGB 或 Depth 数据为空；
- 图像尺寸发生异常变化；
- 帧回调仍发生但内容长期全零。

连续多次恢复失败时，应停止当前采集会话，而不是无限循环重连。

## 3. GetCamParameter 调用顺序（写进 Adapter）

参数缓存需要首次 StartStream 后才可读取。SDK Adapter 初始化流程固定为：

```
OpenCamera
→ SetStreamParam
→ StartStream
→ 等待首个有效帧
→ GetCamParameter
→ 生成 calibration snapshot
→ 正式开始记录
```

每次重开相机后都重新执行一次，并计算标定参数哈希。即使数值没有变化，也应在会话数据中保存新的标定快照：

```json
{
  "calibration_id": "sha256:...",
  "camera_serial": "ASC60CF08000539",
  "sdk_version": "1.2.14.20240129",
  "firmware_version": "DLS_VERc20865=:V01.09_2024110714:V01.19_2024110714:L01",
  "config_version": "v2.0.0.20230704",
  "registration_enabled": true
}
```

## 4. 网络帧时间戳协议

rgbImg.ts 与 depthImg.ts 实测完全相同。协议保留两个字段（兼容其他设备），同时增加同步类型标记：

```json
{
  "sync_mode": "sdk_paired_shared_timestamp",
  "rgb_timestamp_ms": 1722420000000,
  "depth_timestamp_ms": 1722420000000,
  "timestamp_source": "system_clock",
  "exposure_sync_known": false
}
```

服务器算法只能解释为：RGB 与 Depth 属于同一个 SDK 配对帧。**不能**把时间差为零解释为真正的传感器同时曝光。

每帧还应继续记录：
- SDK 时间戳；
- Windows QueryPerformanceCounter 对应的单调时间；
- 主机 UTC；
- 回调到达时间；
- 帧进入压缩队列时间。

用于定位 SDK、USB、编码和网络各阶段的延迟。

## 5. 深度置信度改为软件估算

无硬件置信度通道，几何融合阶段生成软件置信度：

```
confidence = validity_weight × distance_weight × temporal_weight × edge_weight × incidence_weight × local_variance_weight
```

第一版规则：

| 条件 | 处理 |
|---|---|
| 深度为 0 | 权重 0 |
| 深度超出工作范围 | 权重 0 |
| 邻域有效像素过少 | 权重降低 |
| 3×3 邻域深度极差过大 | 标记为边缘或飞点 |
| 连续帧深度波动大 | 权重降低 |
| 距离增加 | 权重逐渐降低 |
| 接近遮挡边缘 | 权重降低 |
| 表面斜入射 | 权重降低 |

第一阶段不使用神经网络置信度模型。先使用可解释的规则和 HP60C 精度摸底数据建立基线。

## 6. 当前最关键风险：畸变参数全零

SDK 返回全零有三种可能：
1. SDK 输出已经去畸变；
2. 配置中存在畸变，但没有通过 API 暴露；
3. SDK 直接忽略了真实畸变。

**不能仅根据参数为零判断图像不存在畸变。**

建议分别对 RGB 和原始 Depth 做标定验证：
- RGB：棋盘格 / ChArUco → 估计 fx、fy、cx、cy 和畸变 → 与 SDK 内参比较 → 检查去畸变前后的直线误差；
- Depth：平面标定板 → 多位置、多距离采集 → 估计深度投影模型 → 检查边缘和画面四角残差。

需要特别确认 is_Registration=True 后的 Depth 是：
- 注册后的深度图；还是
- 原始 Depth 经过 RGB 视角重采样后的结果。

当前 RGB 和 Depth 分辨率相同，但两套内参差异明显（RGB fx=566.16 vs Depth fx=421.89），**不能在服务器上混用**：RGB 内参 ≠ Depth 内参。

若传输的是 SDK 已注册 Depth，服务器生成点云时必须明确应使用哪套内参。建议实测：
- 选取 RGB 图像中的物体角点 → 找到注册 Depth 中对应像素 → 分别使用 RGB 内参和 Depth 内参反投影 → 检查点云边缘和物理尺寸。

该测试必须在正式 RTAB-Map 和 TSDF 之前完成。

## 7. 需要补充的三个验证实验

### A. 60 分钟稳定帧率实验

0.66 s 窗口不足以冻结流参数。连续记录至少 60 分钟：
- SDK 时间戳间隔；
- 主机回调间隔；
- 帧号；
- 全零帧；
- RGB/Depth 缺失；
- USB 重传或停顿；
- 内存占用；
- SDK 线程数量。

重点统计：平均帧间隔、P95、P99、最大帧间隔、连续缺帧长度、实际 FPS。

判定：目标 15 FPS 时正常帧间隔约 66.7 ms；若集中在 33.3 ms，实际流可能是 30 FPS。

#### 前置发现（2026-07-31 实测，fps 档位矩阵）

固定 640x480、SYSTEM_CLOCK、flags=RGB|DEPTH，每档 6s 实测（diag_fps.py）：

| SetStreamParam fps | StartStream | 回调率 | RGB 有效 | Depth 有效 | 结论 |
|---|---|---|---|---|---|
| 10 | **-91** | 0 | - | - | ❌ SDK checkFpsCfg 拒绝（depth rate ≠ rgb rate） |
| 15 | 0 | 16.7~23.2/s | 100% | **49%**（隔帧 size=0） | ❌ depth 有效帧减半，不可用 |
| 25 | **-91** | 0 | - | - | ❌ 拒绝 |
| 30 | 0 | 15.7~23.8/s | 100% | 100%（成对） | ✅ **唯一完整成对档位** |

- **结论：实验 A 采用 SetStreamParam(fps=30)**。配置 15 时 depth 流在 MF 后端隔帧丢失（两轮独立验证 depth_valid≈49%）；10/25 直接启动失败。
- 同档位回调率跨会话波动（30fps 档两次实测 15.7 与 23.8 fps），ts 间隔分布偏斜（p95≈65ms 而 mean≈42ms）→ 60 分钟实验必须量化真实分布。
- 配置参数语义修正：请求 fps=30 实际输出 15~24 fps，请求 fps=15 输出 depth 残缺流。**"15 FPS 工作点"在本 SDK 上只能通过 fps=30 配置获得。**

### B. 注册深度投影模型验证

验证服务器应使用 RGB 内参还是 Depth 内参生成已注册深度点云。输出至少包括：
- 平面尺寸误差；
- 点云边缘与 RGB 边缘重合度；
- RGB 内参反投影结果；
- Depth 内参反投影结果；
- SDK PointCloud 结果；
- 三者差异。

SDK 自带点云作为验证参考，但网络中只传 Depth，不传点云。

### C. 自行标定重复性实验

至少三次独立标定，检查结果离散程度：
- RGB fx/fy 差异；
- RGB 主点差异；
- 畸变参数差异；
- Depth 内参差异；
- Depth→RGB 平移差异；
- Depth→RGB 旋转差异。

若每次标定结果差异较大，说明标定板、采集姿态或深度角点提取方法需要改进，不能直接采用单次结果。
