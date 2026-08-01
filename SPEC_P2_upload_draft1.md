# HP60C 上传协议与深度置信度规格

版本：`draft-1.0`
适用范围：Windows Capture Agent → Tailscale → WSL Ingest Server
前置状态：服务器端 P2-12 已完成，提交 `3a7ceb6`

---

# 1. 已冻结的输入语义

```yaml
camera:
  model: HP60C
  resolution: [640, 480]
  requested_fps: 30
  expected_actual_fps: approximately_23.3
  rgb_format: bgr8
  depth_format: uint16_le
  depth_unit: millimeter
  invalid_depth: 0
  registration: depth_registered_to_rgb_grid
  synchronization: sdk_paired_shared_timestamp
  hardware_confidence: unavailable
```

投影语义：

```yaml
projection_profiles:
  sdk_compatible:
    intrinsics: sdk_rgb
    distortion: zero
    purpose: reproduce_sdk_pointcloud

  metric_calibrated:
    intrinsics: independent_rgb_calibration
    distortion: calibrated
    purpose: metric_reconstruction
```

上传层只保存原始 RGB、注册 Depth、时间戳、标定快照和采集元数据。

上传端不得执行：

* 点云生成；
* 深度置信度最终计算；
* 深度去畸变；
* SLAM；
* TSDF；
* 原始 Depth 的破坏性过滤。

---

# 2. 总体架构

```text
HP60C SDK callback
        │
        ▼
Bounded Record Queue
        │
        ▼
Chunk Builder
        │
        ▼
Local Spool Commit
.part → fsync → .ready
        │
        ├─────────────── 采集线程到此即与网络解耦
        ▼
NetworkSink Sender
        │
        ▼
Ingest Server
.part → verify → fsync → atomic rename → journal commit
        │
        ▼
CHUNK_COMMIT_ACK
        │
        ▼
Client ACK Journal
.ready → .acked → delete
```

系统实现的是：

```text
at-least-once network delivery
+
idempotent server commit
=
exactly-once durable chunk storage
```

不宣称网络传输本身 exactly-once。

---

# 3. NetworkSink 子类规格

## 3.1 定位

`NetworkSink` 是现有 `Sink` 抽象的异步子类，但不得直接把“网络发送成功”作为 `Sink::submit()` 的成功语义。

`submit(record)` 成功只表示：

> NetworkSink 已取得记录所有权，记录已进入有界队列，后续将被写入本地 spool。

需要区分三个完成等级：

```text
ACCEPTED
记录已进入 Sink 有界队列

LOCAL_DURABLE
记录所属 chunk 已原子提交为 .ready

REMOTE_COMMITTED
服务器已持久化 chunk，并返回可验证 ACK
```

## 3.2 建议接口

方法名可以适配现有基类，但语义必须保持：

```cpp
class NetworkSink final : public Sink {
public:
    SinkResult open_session(const SessionDescriptor& session) override;

    // 不执行网络 I/O，不等待 ACK。
    SinkResult submit(Record&& record) override;

    // 将已接受的记录全部写入 .ready。
    SinkResult flush_local(std::chrono::milliseconds timeout);

    // 等待所有 .ready chunk 收到持久化 ACK。
    SinkResult flush_remote(std::chrono::milliseconds timeout);

    // 结束采集并生成本地 session seal。
    SinkResult seal_session();

    // 发送 SESSION_SEAL，等待最终会话 ACK。
    SinkResult commit_session(std::chrono::milliseconds timeout);

    SinkStatus status() const;
    void close() override;
};
```

## 3.3 强制约束

* 相机回调线程不得连接网络。
* `submit()` 不得等待服务器。
* `submit()` 不得引用 SDK 回调结束后可能失效的缓冲区。
* Record 必须复制到自有内存，或显式转移稳定缓冲区所有权。
* 所有内部队列有固定上限。
* 网络故障不得破坏已经生成的 `.ready`。
* 未 ACK 的 chunk 永远不得因循环缓存策略被静默删除。
* spool 即将耗尽时必须降级、暂停或停止采集，而不是删除未确认数据。

## 3.4 NetworkSink 状态机

```text
STOPPED
  ↓ open_session
STARTING
  ↓
OFFLINE / CONNECTING
  ↓
HANDSHAKING
  ↓
RESUMING
  ↓
SENDING
  ├─ 网络中断 → BACKOFF → CONNECTING
  ├─ 服务端流控 → THROTTLED
  ├─ 鉴权失败 → AUTH_FAILED
  ├─ chunk 冲突 → SESSION_ERROR
  └─ seal 完成 → SESSION_COMMITTED
```

`AUTH_FAILED`、`SESSION_ERROR` 和 `CHUNK_CONFLICT` 不允许无限自动重试。

---

# 4. 本地 Spool 规格

## 4.1 目录结构

```text
spool/
└─ <session_id>/
   ├─ session.json
   ├─ calibration.json
   ├─ chunks/
   │  ├─ 000000000000.chunk.part
   │  ├─ 000000000001.chunk.ready
   │  └─ 000000000002.chunk.acked
   ├─ manifests/
   │  ├─ 000000000001.json
   │  └─ 000000000002.json
   ├─ ack.jsonl
   ├─ events.jsonl
   ├─ seal.json
   └─ receipt.json
```

## 4.2 文件状态

| 状态       | 含义                    |
| -------- | --------------------- |
| `.part`  | 正在构建，不可上传             |
| `.ready` | 内容完整且本地持久化，可上传        |
| `.acked` | 已记录有效 ACK，等待删除或崩溃恢复清理 |
| `.error` | 本地校验失败或服务端冲突，禁止删除     |

不使用 `.sent` 表示可靠状态，因为“已经发送”不代表“服务器已经持久化”。

## 4.3 Chunk 完成本地提交顺序

```text
1. 写入 *.part
2. 完成内部 Record 索引
3. 计算 payload_bytes
4. 计算 CRC32C
5. 计算 SHA-256
6. flush 文件缓冲区
7. fsync 文件
8. 原子 rename：.part → .ready
9. fsync chunks 目录
10. 写入 manifest
```

只有完成第 9 步后，状态才是 `LOCAL_DURABLE`。

## 4.4 Chunk 划分

默认：

```yaml
chunk:
  target_duration_ms: 2000
  max_payload_bytes: 67108864
  max_frame_count: 128
```

Chunk 以时间为主要切分条件，不得假设每个 chunk 固定为 30、46 或其他固定帧数。

遇到以下任一条件立即结束当前 chunk：

* 时长达到 2 秒；
* payload 达到 64 MiB；
* 会话暂停；
* 相机断线；
* 标定 ID 改变；
  -采集配置改变；
  -正常停止采集。

---

# 5. Chunk Manifest

每个 `.ready` 必须有对应 manifest：

```json
{
  "schema": "hp60c.chunk-manifest",
  "schema_version": "1.0",
  "protocol_major": 1,
  "protocol_minor": 0,

  "session_id": "uuid",
  "chunk_seq": 12,
  "chunk_id": "uuid",

  "first_frame_id": 552,
  "last_frame_id": 598,
  "frame_count": 47,

  "first_device_timestamp_ms": 1785499554000,
  "last_device_timestamp_ms": 1785499556001,

  "calibration_id": "sha256:...",
  "capture_config_hash": "sha256:...",

  "payload_bytes": 12345678,
  "payload_crc32c": "8f31a209",
  "payload_sha256": "sha256:...",

  "created_utc": "2026-07-31T18:53:00Z"
}
```

服务器幂等键为：

```text
(session_id, chunk_seq)
```

内容身份为：

```text
payload_sha256
```

处理规则：

| 服务端已有状态               | 新上传结果                  |
| --------------------- | ---------------------- |
| 相同 session、seq、SHA    | 返回 `ALREADY_COMMITTED` |
| 相同 session、seq，不同 SHA | 返回 `CHUNK_CONFLICT`    |
| 尚不存在                  | 正常接收并提交                |

`CHUNK_CONFLICT` 是不可自动恢复错误，必须保留本地文件并停止该会话继续上传。

---

# 6. 网络协议

## 6.1 传输

```yaml
transport:
  network: tailscale
  application: websocket
  control_channel: /v1/control
  stream_channel: /v1/stream
  byte_order: little_endian
  max_inflight_chunks: 4
  heartbeat_seconds: 2
```

控制通道使用 JSON。

数据通道发送现有 chunk 容器的完整字节序列。NetworkSink 将 chunk 视为不可变 opaque blob，不在传输层重新解释或重编码其中的帧。

## 6.2 协议版本

```json
{
  "protocol_major": 1,
  "protocol_minor": 0
}
```

规则：

* major 不一致：拒绝连接；
* major 相同、minor 较新：通过 capability negotiation 判断；
* 未协商的字段不得擅自启用；
* 未识别的可选字段允许忽略；
* 未识别的必需能力必须拒绝。

## 6.3 消息类型

```text
HELLO
HELLO_ACK

SESSION_OPEN
SESSION_OPEN_ACK

RESUME_QUERY
RESUME_STATE

CHUNK_OFFER
CHUNK_SEND
CHUNK_COMMIT_ACK

HEARTBEAT
FLOW_CONTROL

SESSION_SEAL
SESSION_COMMIT_ACK

ERROR
```

---

# 7. 建连与续传

## 7.1 HELLO

客户端发送：

```json
{
  "type": "HELLO",
  "protocol_major": 1,
  "protocol_minor": 0,
  "client_id": "capture-laptop",
  "client_instance_id": "uuid-per-process",
  "agent_version": "git:...",
  "capabilities": [
    "chunk_offer",
    "resume_ranges",
    "sha256",
    "crc32c",
    "session_seal"
  ]
}
```

服务器返回：

```json
{
  "type": "HELLO_ACK",
  "server_instance_id": "uuid",
  "accepted_protocol_major": 1,
  "accepted_protocol_minor": 0,
  "max_chunk_bytes": 67108864,
  "max_inflight_chunks": 4
}
```

## 7.2 RESUME_QUERY

重连后客户端不得只依赖自己的“最后已发送编号”，必须查询服务器已提交状态。

```json
{
  "type": "RESUME_QUERY",
  "session_id": "uuid"
}
```

响应：

```json
{
  "type": "RESUME_STATE",
  "session_id": "uuid",
  "contiguous_committed_through": 18,
  "committed_ranges": [
    [0, 18],
    [20, 21]
  ],
  "server_session_state": "OPEN"
}
```

客户端行为：

* 对服务器已提交且 SHA 匹配的本地 chunk，按 `ALREADY_COMMITTED` 处理；
* 对缺失 chunk 重新上传；
* 不假设 chunk 必然按顺序到达；
* 不因服务器存在 chunk 20 而跳过缺失的 chunk 19。

---

# 8. Chunk 上传流程

## 8.1 CHUNK_OFFER

客户端先发送元数据，不立即发送大块内容：

```json
{
  "type": "CHUNK_OFFER",
  "session_id": "uuid",
  "chunk_seq": 19,
  "chunk_id": "uuid",
  "payload_bytes": 12345678,
  "payload_crc32c": "8f31a209",
  "payload_sha256": "sha256:..."
}
```

服务器响应之一：

```text
CHUNK_SEND
CHUNK_COMMIT_ACK(status=ALREADY_COMMITTED)
CHUNK_COMMIT_ACK(status=COMMITTED)  # 小 chunk 可能 offer 即提交
CHUNK_COMMIT_ACK(status=ALREADY_COMMITTED)
ERROR(code=CHUNK_CONFLICT)
ERROR(code=SERVER_DISK_LOW)
```

这样可以避免断线重连后重复传输服务器已经持久化的 chunk。

## 8.2 数据发送

服务器返回 `CHUNK_SEND` 后，客户端通过 stream channel 发送：

```text
固定二进制传输头
+
完整 chunk bytes
```

传输头至少包含：

```text
magic
protocol_major
protocol_minor
session_id
chunk_seq
chunk_id
payload_bytes
payload_crc32c
payload_sha256
```

一个 chunk 可以由 WebSocket fragmentation 传输，但应用层 ACK 只能在完整 chunk 提交后发送。

---

# 9. 服务端提交与 ACK 语义

## 9.1 服务端提交顺序

服务器必须严格执行：

```text
1. 接收至 *.part
2. 边接收边计算 CRC32C 和 SHA-256
3. 验证实际字节数
4. 验证 CRC32C
5. 验证 SHA-256
6. 验证 chunk 内部格式
7. fsync *.part
8. 原子 rename 至正式 chunk 文件
9. fsync 目标目录
10. 写入 ingest journal / session index
11. fsync journal 或提交持久化事务
12. 发送 CHUNK_COMMIT_ACK
```

禁止在步骤 11 完成之前发送 ACK。

## 9.2 ACK 格式

```json
{
  "type": "CHUNK_COMMIT_ACK",
  "status": "COMMITTED",
  "session_id": "uuid",
  "chunk_seq": 19,
  "chunk_id": "uuid",
  "payload_bytes": 12345678,
  "payload_sha256": "sha256:...",
  "server_commit_id": "uuid",
  "server_instance_id": "uuid",
  "committed_utc": "2026-07-31T18:53:05Z"
}
```

允许删除本地 chunk 的 ACK 状态只有：

```text
COMMITTED
ALREADY_COMMITTED
```

且必须同时满足：

```text
session_id 匹配
chunk_seq 匹配
chunk_id 匹配
payload_bytes 匹配
payload_sha256 匹配
连接已经通过身份验证
```

允许删除本地 chunk 的 ACK 状态只有 `COMMITTED` / `ALREADY_COMMITTED`，且必须通过身份验证的连接。

---

# 10. ACK 后删除闭环

客户端收到有效 ACK 后执行：

```text
1. 验证 ACK 所有身份字段和 payload SHA-256
2. 向 ack.jsonl 追加完整 ACK
3. flush + fsync ack.jsonl
4. 原子 rename：.ready → .acked
5. fsync chunks 目录
6. 删除 .acked payload
7. 删除对应 manifest
8. fsync chunks 和 manifests 目录
9. 更新内存状态与 UI 统计
```

如果程序在任意步骤崩溃：

| 崩溃位置                     | 恢复行为                      |
| ------------------------ | ------------------------- |
| ACK 日志写入前                | `.ready` 保留，重新上传          |
| ACK 日志写入后、rename 前       | 根据 ACK journal 清理或查询服务器   |
| rename 后、delete 前        | 启动时清理 `.acked`            |
| payload 删除后、manifest 删除前 | 删除孤立 manifest             |
| ACK 丢失但服务端已提交            | 重连后获得 `ALREADY_COMMITTED` |

因此任何单点崩溃都不会造成：

* 未持久化数据被删除；
* 服务器重复保存同一 chunk；
* 客户端永远无法清理已提交 chunk。

---

# 11. 会话结束闭环

停止采集后客户端生成：

```json
{
  "type": "SESSION_SEAL",
  "session_id": "uuid",
  "expected_chunk_count": 120,
  "expected_frame_count": 5591,
  "first_frame_id": 0,
  "last_frame_id": 5590,
  "chunk_manifest_sha256": "sha256:...",
  "session_manifest_sha256": "sha256:..."
}
```

服务器只有在以下条件全部满足时返回 `SESSION_COMMIT_ACK`：

* 所有 chunk 均存在；
* seq 没有缺口；
* frame 范围与 manifest 一致；
* 所有 hash 一致；
* calibration 已提交；
* session manifest 已持久化；
* session 状态已经原子变更为 `COMMITTED`。

客户端收到最终 ACK 后：

* 写入 `receipt.json`；
* 保留 `session.json`、`receipt.json` 和小型审计日志；
* 可删除已经无 payload 的空目录；
* 不删除最终 receipt。

---

# 12. 重试与流控

## 12.1 重连退避

```text
1 s
2 s
4 s
8 s
15 s
之后每 15 s
```

增加 ±20% 随机抖动，避免多个客户端同步重连。

## 12.2 超时

```yaml
timeouts:
  connect_seconds: 10
  control_idle_seconds: 10
  stream_no_progress_seconds: 30
  commit_ack_seconds_after_upload: 15
```

只要数据发送仍有进展，不因整个大 chunk 总时长超过 30 秒而中断。

## 12.3 服务端流控

```json
{
  "type": "FLOW_CONTROL",
  "server_queue_depth": 3,
  "server_disk_free_bytes": 9876543210,
  "server_disk_free_bytes": 9876543210,
  "max_inflight_chunks": 2,
  "recommended_rgb_quality": 88,
  "recommended_capture_state": "CONTINUE"
}
```

允许状态：

```text
CONTINUE
THROTTLE
PAUSE_AFTER_CURRENT_CHUNK
STOP_SAFELY
```

服务器不得通过流控要求客户端删除未 ACK 数据。

## 12.4 本地空间策略

默认 spool 上限：

```yaml
spool:
  max_size_gb: 5
  warning_ratio: 0.80
  throttle_ratio: 0.90
  stop_ratio: 0.97
```

行为：

* 80%：黄色告警；
* 90%：允许降低 RGB JPEG 质量，限制非必要预览；
* 97%：完成当前 chunk 后安全暂停；
* 任何比例：禁止删除未 ACK chunk；
* 不得无记录丢帧以维持表面上的“继续采集”。

---

# 13. 深度置信度决策

## 13.1 数据来源

HP60C SDK 不提供硬件置信度通道。

因此字段必须明确：

```yaml
depth_confidence:
  hardware_available: false
  transmitted_in_protocol_v1: false
  derived_on_server: true
  semantic: software_reliability_estimate
```

软件置信度不是：

* 深度真值；
* 误差上界保证；
* 相机硬件提供的 confidence；
* 标定精度证明；
* 物体识别置信度。

它表示：

> 当前深度像素适合参与几何融合的相对可靠程度。

## 13.2 为什么不在 Capture Agent 计算

* 避免增加采集端 CPU 和延迟；
* 避免算法升级后无法重算旧数据；
* 保留原始 Depth；
* 保证同一原始数据和同一算法版本得到可重复输出；
* 未来可以使用标定位姿或邻帧重投影增强置信度。

上传协议 V1 不传置信度图。

协议 capability 中保留未来可选通道：

```text
AUX_DEPTH_CONFIDENCE_U8
```

但 HP60C V1 会话不得声明该能力。

---

# 14. 软件深度置信度 V1

## 14.1 输入

```text
registered depth：uint16 mm
invalid value：0
min/max depth
RGB-grid calibration identity
confidence algorithm config
```

V1 不要求 RGB，不要求相机位姿，不使用神经网络。

## 14.2 输出

```text
confidence_u8：640×480，mono8
reason_bits_u8：640×480，mono8，可选
quality_summary.json
```

置信度定义：

```text
0       无效或明确拒绝
1~63    极低
64~127  低
128~191 中
192~255 高
```

原始 Depth 永不被覆盖。

## 14.3 硬无效条件

以下像素置信度强制为 0：

```text
depth == 0
depth < configured_min_depth
depth > configured_max_depth
重映射后没有来源像素
数据损坏或超出图像边界
```

第一阶段范围：

```yaml
depth_range_mm:
  min: 250
  max: 2500
```

该范围是采集配置，不代表相机物理极限。

## 14.4 单帧置信度组成

V1 使用可解释的单帧规则：

```text
C =
valid_mask
× neighborhood_support
× local_consistency
× discontinuity_penalty
× isolated_pixel_penalty
```

### A. 邻域支持度

使用 `5×5` 邻域：

```text
support = valid_neighbor_count / 25
```

有效邻居不足时降低置信度。

该项用于识别：

* 深度孔洞边缘；
* 稀疏噪点；
* 孤立飞点；
* 大片无效区域旁的不稳定像素。

### B. 局部稳健一致性

计算邻域有效深度的：

```text
median
MAD = median(|z - median|)
```

像素残差：

```text
residual = |z_pixel - median|
```

有效噪声尺度：

```text
sigma_effective =
max(
  sigma_model(z),
  1.4826 × MAD,
  sigma_floor
)
```

局部项：

```text
local_consistency =
exp(-0.5 × (residual / sigma_effective)²)
```

`sigma_model(z)` 必须由 HP60C 平面采集实验拟合，不得长期使用拍脑袋常数。

建议模型：

```text
sigma_model(z) = a + b × z²
```

其中 z 使用米，输出为毫米。

### C. 深度不连续惩罚

计算有效邻居中的最大局部深度差：

```text
gradient_ratio =
max(|z_pixel - z_neighbor|) / z_pixel
```

深度突变区域降低置信度，因为这些区域容易出现：

* 结构光遮挡；
* 前后景混合像素；
* RGB 注册边缘误差；
* 飞点；
* TSDF 表面膨胀。

真实物体边缘仍可能具有正确深度，但不宜以满权重进入融合。

### D. 孤立像素惩罚

满足以下情况时直接降为极低置信度或 0：

* `3×3` 内仅有一个有效像素；
* 与所有有效邻居差异均超过噪声阈值；
* 小型连通区域低于配置面积。

## 14.5 V1 明确不使用朴素时间一致性

手持相机运动时，相邻帧同一像素通常观察的不是同一个空间点。

因此禁止：

```text
直接比较 frame_t[x,y] 与 frame_t-1[x,y]
```

这会把正常相机运动误判为深度不稳定。

时间一致性只能在 V2 中实现：

```text
历史 Depth
+ 相机位姿
+ 标定射线
→ 重投影到当前帧
→ 遮挡判断
→ 比较预测深度与当前深度

```

在 SLAM 位姿可用之前，V1 保持单帧确定性算法。

---

# 15. reason_bits 定义

```text
bit 0：RAW_INVALID
bit 1：OUT_OF_RANGE
bit 2：LOW_NEIGHBOR_SUPPORT
bit 3：DEPTH_DISCONTINUITY
bit 4：LOCAL_OUTLIER
bit 5：RECTIFICATION_HOLE
bit 6：POSE_AWARE_TEMPORAL_FAIL，V1 恒为 0
bit 7：RESERVED
```

同一像素可以同时设置多个原因位。

`reason_bits` 用于审计和可视化，不直接作为最终权重。

---

# 16. 置信度与 Metric Calibration

原始 Depth 位于 SDK 注册后的 RGB 像素网格。

### SDK-compatible 模式

```text
原始 registered depth
+ SDK RGB intrinsics
+ software confidence
```

### Metric-calibrated 模式

采用两种实现之一：

```text
A. RGB 和 Depth 一起重映射到无畸变网格
B. 保持原始像素网格，使用预计算校准射线 LUT
```

如果执行图像重映射：

* Depth 使用最近邻或带 z-buffer 的深度重采样；
* Confidence 使用最近邻或贡献像素最小值；
* 不对 Depth 和 Confidence 使用普通双线性插值；
* 新产生的孔洞 confidence 必须为 0；
* reason bit 设置 `RECTIFICATION_HOLE`。

---

# 17. TSDF 使用方式

初始规则：

```text
confidence < 96:
  reject

confidence >= 96:
  weight = ((confidence - 96) / 159)²
```

初始规则：

```text
confidence < 96:
  reject

confidence >= 96:
  weight = ((confidence - 96) / 159)²
```

最终融合权重：

```text
tsdf_weight =
confidence_weight
× range_weight
× view_angle_weight
```

第一阶段允许只使用 `confidence_weight`，但算法版本和参数哈希必须进入任务日志。

置信度过滤不能破坏原始数据：

```text
raw depth            永久保存
confidence map       派生产物
filtered depth       派生产物
TSDF input           可追溯派生产物
```

---

# 18. ROS 2 / MCAP 表达

建议增加：

```text
/hp60c/depth_registered/image_raw
  sensor_msgs/Image
  encoding: 16UC1

/hp60c/depth_registered/camera_info
  sensor_msgs/CameraInfo

/hp60c/depth_registered/confidence
  sensor_msgs/Image
  encoding: mono8

/hp60c/depth_registered/confidence_reason
  sensor_msgs/Image
  encoding: mono8
  optional
```

每次置信度处理任务记录：

```json
{
  "algorithm_id": "hp60c_depth_confidence_v1",
  "software_version": "git:...",
  "config_sha256": "sha256:...",
  "input_session_sha256": "sha256:...",
  "output_sha256": "sha256:..."
}
```

---

# 19. 深度置信度标定实验

在冻结 `sigma_model` 和阈值前，增加专项实验：

## 距离组

```text
0.30 m
0.50 m
0.80 m
1.20 m
1.80 m
2.50 m
```

每个距离至少采集 300 帧。

## 场景组

* 哑光平面；
* 前后景锐利边缘；
* 倾斜平面；
* 黑色低反射材料；
* 高反射材料；
* 细杆或窄边物体。

## 输出

* 平面拟合残差；
* 深度标准差随距离关系；
* 飞点率；
* 孔洞率；
* 边缘误差分布；
* confidence 分桶后的真实残差；
* `sigma_model(z)` 参数；
* 推荐 TSDF 最低置信度阈值。

不能用整帧有效像素比例作为统一质量阈值，因为有效覆盖率高度依赖场景构图和相机距离。

更合适的指标：

```text
high_confidence_ratio_among_valid
plane_rmse_by_confidence_bucket
edge_outlier_rate
confidence_calibration_curve
```

---

# 20. 验收测试

## P2-13：NetworkSink

* [ ] 网络线程不进入相机回调调用栈
* [ ] `submit()` 不等待服务器 ACK
* [ ] 所有队列容量有上限
* [ ] 2 秒 chunk 可正确生成
* [ ] 64 MiB 上限可提前切块
* [ ] `.part → .ready` 原子提交
* [ ] 进程强杀后 `.ready` 可恢复
* [ ] 重连后自动执行 `RESUME_QUERY`
* [ ] 支持最多 4 个 in-flight chunk
* [ ] 断线期间持续写入本地 spool
* [ ] spool 满时安全暂停，不删除未 ACK 数据

## P2-14：ACK 删除闭环

必须在以下故障点逐项强杀：

```text
chunk 写入中
fsync 前
rename 前
发送中
服务器 fsync 前
服务器 rename 后、journal 前
服务器 journal 后、ACK 前
客户端收到 ACK 后、写 ack journal 前
写 ack journal 后、rename 前
rename .acked 后、删除前
```

必须在以下故障点逐项强杀：

```text
chunk 写入中
fsync 前
rename 前
发送中
服务器 fsync 前
服务器 rename 后、journal 前
服务器 journal 后、ACK 前
客户端收到 ACK 后、写 ack journal 前
写 ack journal 后、rename 前
rename .acked 后、删除前
```

验收：

* [ ] 无未提交数据丢失
* [ ] 无重复服务器正式 chunk
* [ ] 重复上传返回 `ALREADY_COMMITTED`
* [ ] 同 seq 不同 SHA 返回 `CHUNK_CONFLICT`
* [ ] 错误 ACK 不会触发删除
* [ ] ACK hash 不匹配不会触发删除
* [ ] 服务端重启后仍能查询已提交状态
* [ ] 客户端重启后可清理 `.acked`
* [ ] SESSION_SEAL 缺 chunk 时拒绝完成
* [ ] 最终生成 `receipt.json`

## P2-15：深度置信度

* [ ] 原始 `depth=0` 的 confidence 必为 0
* [ ] 超出采集范围的 confidence 必为 0
* [ ] 同输入、同配置输出 hash 完全一致
* [ ] 平面中心置信度高于孔洞边缘
* [ ] 深度突变边缘被降权
* [ ] 孤立飞点被拒绝
* [ ] V1 不使用同像素时间差
* [ ] 算法参数和版本进入审计日志
* [ ] 原始 Depth 不被修改
* [ ] confidence 可写入 MCAP 并正常回放
* [ ] 置信度高分桶的平面残差显著低于低分桶
* [ ] 完成距离噪声模型拟合后再冻结 TSDF 阈值

---

# 21. 任务拆分

```text
P2-13a  NetworkSink 基类适配与有界队列
P2-13b  Chunk Builder 与本地原子 spool
P2-13c  WebSocket 双通道与协议握手
P2-13d  CHUNK_OFFER、上传和续传
P2-13e  服务端幂等接收与持久化提交

P2-14a  ACK 校验与本地 ack journal
P2-14b  .ready → .acked → delete
P2-14c  SESSION_SEAL 与最终 receipt
P2-14d  故障注入和崩溃恢复测试

P2-15a  confidence_u8 单帧算法
P2-15b  reason_bits 和质量统计
P2-15c  MCAP / ROS Topic 集成
P2-15d  距离噪声标定实验
P2-15e  TSDF 权重阈值冻结
```

推荐执行顺序：

```text
1. P2-13a / P2-13b
2. P2-13c / P2-13d / P2-13e
3. P2-14a / P2-14b
4. P2-14c / P2-14d
5. P2-15a / P2-15b / P2-15c
6. P2-15d
7. P2-15e
```

---

# 22. 冻结决策摘要

```yaml
upload:
  source_of_truth_before_ack: client_spool
  delivery_semantics: at_least_once
  server_commit: idempotent
  delete_condition: durable_matching_ack_only
  session_completion: explicit_session_commit_ack

network_sink:
  asynchronous: true
  camera_thread_network_io: forbidden
  unacked_eviction: forbidden
  crash_recovery: required

depth_confidence:
  hardware_channel: absent
  protocol_v1_payload: not_transmitted
  computation_location: server
  algorithm_v1: deterministic_single_frame_rules
  temporal_same_pixel_comparison: forbidden
  raw_depth_preservation: mandatory
```
