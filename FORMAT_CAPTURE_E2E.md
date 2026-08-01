# Capture E2E 产物格式说明

本文档定义 Capture Agent 端到端录制产物的**二进制/JSON 格式**，以及两个验证工具
（`verify_e2e.py` 旧布局、`verify_spool_e2e.py` P2-13 新布局）的校验规则。
对接方（服务端 / 分析脚本）以本文档为准。

- 字节序：**小端**（所有多字节字段）
- 时间基：所有 ms 时间戳均为 epoch UTC 毫秒（`SYSTEM_CLOCK`，规格 §1）
- 帧规格：RGB 640×480×3 (BGR8)；Depth 640×480×2 (uint16 LE, 单位 mm, **0=无效**)
- 请求帧率 30 FPS（实测稳定约 21.9 FPS 对流，规格 §7A）

---

## 1. 两套布局对照

| 项 | 旧布局 (P0, `verify_e2e.py`) | 新布局 (P2-13a/b, `verify_spool_e2e.py`) |
|---|---|---|
| 根目录 | 扁平：`chunk_*.asc` + `manifest.json` + `session.json` | `root/<session_id>/` 分会话目录 |
| 会话 id | 数字递增（1, 2, ...） | **uuid 字符串**（协议 §5 幂等键） |
| chunk 文件 | `chunk_000000000001.asc` | `chunks/000000000001.chunk.ready` |
| manifest | 单文件集中式 | 每 chunk 一个 `manifests/000000000001.json` |
| 指纹 | 无 | `payload_crc32c` + `payload_sha256`（§4.3/§5） |
| 状态机 | pending / acked（磁盘文件名不变） | part→ready→acked（文件后缀即状态） |
| 校验工具 | `verify_e2e.py <dir>` | `verify_spool_e2e.py <root>` |

chunk 二进制容器（`.asc`）两种布局**完全相同**，服务器端解析无需区分。

---

## 2. Chunk 二进制容器（`.asc`）

### 2.1 Header（96 字节）

| 偏移 | 大小 | 类型 | 字段 | 说明 |
|---|---|---|---|---|
| 0 | 4 | bytes | `magic` | 固定 `"ASCC"` |
| 4 | 4 | u32 | `version` | 固定 1 |
| 8 | 4 | u32 | `flags` | 固定 0 |
| 12 | 8 | u64 | `chunk_id` | 全局递增（本会话内亦唯一） |
| 20 | 8 | u64 | `session_id` | 会话号。P2-13 起为 uuid 低 64 位**诊断回显**；规范身份以 manifest/session.json 字符串为准 |
| 28 | 4 | u32 | `frame_count` | 本 chunk 帧数 |
| 32 | 8 | u64 | `first_frame_id` | 首帧全局帧号 |
| 40 | 8 | u64 | `last_frame_id` | 末帧全局帧号 |
| 48 | 8 | u64 | `first_utc_ms` | 首帧 UTC 毫秒 |
| 56 | 8 | u64 | `last_utc_ms` | 末帧 UTC 毫秒 |
| 64 | 4 | u32 | `rgb_w` | 640 |
| 68 | 4 | u32 | `rgb_h` | 480 |
| 72 | 4 | u32 | `depth_w` | 640 |
| 76 | 4 | u32 | `depth_h` | 480 |
| 80 | 4 | u32 | `jpeg_quality` | 85 |
| 84 | 4 | u32 | `depth_codec` | 1 = zstd |
| 88 | 16 | bytes | `reserved` | 全 0 |

### 2.2 帧记录（60 字节 + 数据，重复 frame_count 次）

| 偏移 | 大小 | 类型 | 字段 | 说明 |
|---|---|---|---|---|
| 0 | 8 | u64 | `frame_id` | 全局帧号 |
| 8 | 8 | u64 | `sdk_ts_ms` | SDK 时间戳（SYSTEM_CLOCK，epoch ms） |
| 16 | 8 | u64 | `host_qpc` | 回调入口 QPC 计数（诊断延迟用） |
| 24 | 8 | u64 | `host_mono_ms` | 回调入口主机单调 ms |
| 32 | 8 | u64 | `utc_ms` | 回调入口 UTC ms（= 帧到达主机时刻） |
| 40 | 8 | u64 | `arrival_mono_ms` | 帧进入编码队列时刻（单调 ms） |
| 48 | 4 | u32 | `rgb_size` | rgb_data 字节数（JPEG） |
| 52 | 4 | u32 | `depth_size` | depth_data 字节数（zstd） |
| 56 | 4 | u32 | `flags` | bit0=1 表示整帧深度全零 |
| 60 | — | bytes | `rgb_data` | JPEG（BGR8 编码） |
| 60+rgb_size | — | bytes | `depth_data` | zstd 压缩（uint16 LE，mm） |

时间戳四件套 = `sdk_ts_ms` / `host_qpc` / `host_mono_ms` / `utc_ms`，外加 `arrival_mono_ms`
用于定位 SDK/USB/编码各阶段延迟（规格 §4）。帧顺序即采集顺序。

---

## 3. 旧布局（P0）JSON 文件

### 3.1 manifest.json（集中式，根目录单文件）

```jsonc
{
  "quota_bytes": 5368709120,        // 循环缓存配额 (5 GiB)
  "delete_on_ack": true,
  "chunks": [
    {
      "id": 1,                      // chunk_id
      "file": "chunk_000000000001.asc",
      "size": 6627361,              // 字节
      "frame_count": 44,
      "first_utc_ms": 1785515785010,
      "last_utc_ms": 1785515787023,
      "session_id": 1,              // 数字会话号
      "state": "pending"            // pending | acked
    }
    // ... 每条一个 chunk
  ]
}
```

### 3.2 session.json（根目录单文件）

```jsonc
{
  "session_id": 1,                  // 数字会话号
  "start_utc_ms": 1785516085052,
  "last_frame_id": 6692,
  "last_chunk_id": 146,
  "config": { "camera": {...}, "capture": {...}, "watchdog": {...} },
  "stats": {                        // 运行统计摘要
    "elapsed_s": 305.2,
    "frames": 6692, "fps": 21.93, "dropped": 0,
    "encoded": 6692, "avg_encode_ms": 3.54,
    "chunks": 146, "store_bytes": 931888267,
    "quota_evictions": 0, "acks": 0,
    "stall_events": 0, "recovered": 0, "state": "stopping"
  },
  "calibration_id": "sha256:c9bdc1..."   // 标定指纹
}
```

---

## 4. 新布局（P2-13a/b，规格 §4/§5/§10）JSON 文件

根目录：`root/<session_id(uuid)>/`

### 4.1 session.json

```jsonc
{
  "schema": "hp60c.session",
  "protocol_major": 1, "protocol_minor": 0,
  "session_id": "753ab4ea-122f-41da-b2b0-6d0a32643f7b",  // uuid
  "start_utc_ms": 1785516085052,
  "calibration_id": "smoke-cal-1",        // 或 sha256 指纹
  "capture_config_hash": "sha256:..."     // §4.1 采集配置指纹 (camera+capture+chunk)
}
```

### 4.2 calibration.json

`adapter.read_calibration()` 原文（标定快照，json 可序列化）。

### 4.3 manifests/<seq:012d>.json（每 chunk 一个，幂等键 + 指纹）

```jsonc
{
  "schema": "hp60c.chunk-manifest",
  "schema_version": "1.0",
  "protocol_major": 1, "protocol_minor": 0,
  "session_id": "753ab4ea-...",         // 幂等键组件 1
  "chunk_seq": 1,                        // 幂等键组件 2
  "chunk_id": 1,
  "first_frame_id": 100, "last_frame_id": 168, "frame_count": 68,
  "first_device_timestamp_ms": 2000000, "last_device_timestamp_ms": 2002040,
  "calibration_id": "smoke-cal-1",
  "capture_config_hash": "sha256:...",
  "payload_bytes": 44984,
  "payload_crc32c": "8d3276eb",          // CRC-32C (Castagnoli, 非 IEEE!)
  "payload_sha256": "sha256:...",        // 内容身份 (§5)
  "created_utc": "2026-08-01T12:00:00Z"
}
```

### 4.4 状态与审计文件

| 文件 | 内容 |
|---|---|
| `chunks/<seq>.chunk.part` | 提交中（崩溃后启动清理，不可上传） |
| `chunks/<seq>.chunk.ready` | LOCAL_DURABLE，可上传候选（`pending_chunks()` 按 first 时间升序） |
| `chunks/<seq>.chunk.acked` | ACK 后 rename 残留（启动清理） |
| `chunks/<seq>.chunk.error` | 校验失败/冲突（保留，禁止删除） |
| `ack.jsonl` | ACK 审计日志（每行一个 ack json：`chunk_seq`/`chunk_id`/`payload_sha256`） |
| `events.jsonl` | 状态/恢复事件（CHUNK_LOCAL_DURABLE / SPOOL_RECOVER / ...） |
| `seal.json` | 会话正常结束 seal；不变量 `seal.pending = 已 ACK + 剩余 .ready` |
| `receipt.json` | 最终会话确认（P2-14c 预留） |

---

## 5. verify_e2e.py 校验规则（旧布局）

输入：目录（默认 `audit_output/capture_e2e`）。对每个 `chunk_*.asc`：

1. **ChunkReader 读头**：magic=`ASCC`、version=1，非法即失败
2. **2s 边界**：`last_utc_ms - first_utc_ms` 须在 **[1000, 3200] ms**（宽容窗口，实测约 2013ms）
3. **首/尾帧解码抽查**：`rgb` 用 OpenCV `imdecode` 可解码且形状 `(480, 640, 3)`；`depth` 用 zstd 解压长度 `== 640*480*2`
4. **时间戳四件套字段齐全**：`frame_id/sdk_ts_ms/host_qpc/host_mono_ms/utc_ms/arrival_mono_ms` 均非 0
5. **manifest 一致性**：`manifest.json` 的 chunk id 集合 == 磁盘文件集合；统计 pending/acked
6. **session.json**：会话号/末帧/末 chunk/calibration_id + stats 摘要输出

输出：逐 chunk 摘要行 + 汇总（总帧数/时间跨度/平均每 chunk 帧数/边界判定/解码抽查数）。

## 6. verify_spool_e2e.py 校验规则（新布局）

输入：spool root（默认 `audit_output/spool`）。对每个会话目录：

1. `session.json` 的 `session_id` 是 **uuid** 且与目录名一致；`capture_config_hash` 以 `sha256:` 开头
2. `calibration.json` 可解析且 `calibration_id` 与 session.json 一致
3. `chunks/*.chunk.ready` 与 `manifests/*.json` **一一对应**（数量相等）
4. 每个 ready 文件重算：`payload_bytes`/`payload_crc32c`（CRC-32C）/`payload_sha256` 与 manifest 完全一致；`frame_count` 与 chunk 头一致
5. seal 存在时校验不变量 `seal.pending == ack.jsonl 行数 + 剩余 ready 数`（无数据丢失）
6. 输出：每会话校验行 + 汇总（会话/chunk/帧总数）

---

## 7. 服务器端对接要点（P2-13e 前置）

- **CRC-32C ≠ zlib.crc32**：标准向量 `crc32c(b"123456789") == 0xE3069283`（IEEE 多项式结果为 `0xCBF43926`）。服务器端必须用 Castagnoli 多项式（反射 0x82F63B78），Python 可 `google-crc32c` 或 C 扩展
- 校验顺序建议（§5/§10）：幂等键 `(session_id, chunk_seq)` 查重 → `payload_sha256` 内容校验 → ACK 回 `chunk_id + payload_sha256`，客户端据此闭环删除
- 未 ACK 的 chunk 客户端**永不删除**（无配额驱逐），服务器可随时重拉 `pending_chunks()`
- chunk 容器格式新旧布局一致，服务器解析仅需 §2 定义
