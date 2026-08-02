# HP60C Capture Agent（Windows 端）交付说明

> 阶段：第二阶段审计完成后的产品化实现。规格依据 `SPEC_phase2_fixed.md`（§1 固定配置 / §2 状态机 / §3 标定快照 / §4 时间戳协议）。
> 日期：2026-08-01。相机：HP60C SN `ASC60CF08000539`，SDK v1.2.14.20240129。

## 1. 组件总览

```
hp60c_audit/
├── sdk.py                     # ABI 冻结层: 厂商 DLL 唯一 ctypes 绑定 (版本/契约见文件头)
├── capagent/
│   ├── errors.py              # 统一错误码 (ErrorCode + CameraError)
│   ├── frames.py              # 帧模型 + 时间戳工具 (QPC/单调/UTC)
│   ├── state.py               # 相机状态机 (规格 §2)
│   ├── adapter.py             # SDK Adapter: 生命周期/标定快照/Watchdog/断线恢复
│   ├── encoder.py             # RGB->JPEG (逐帧), Depth->Zstd (无损)
│   ├── chunk.py               # 2 秒 chunk 容器格式 (服务器可直接解析)
│   ├── store.py               # 5GB 磁盘循环缓存 + ACK 状态 + 断线续传候选
│   ├── agent.py               # Capture Agent 主流程 (CLI)
│   └── config.py              # 配置加载 (默认值=规格基线)
├── test_unit.py               # 单元测试 (无需相机, 29 项断言)
└── test_restart.py            # 100 次真机启停测试
```

## 2. 线程模型（禁止网络线程阻塞相机线程）

```
SDK 回调线程 (厂商内部)
  └─ on_frame: 深拷贝 rgb/depth + 时间戳四件套 -> queue.put_nowait (有界 64, 满则丢帧计数)
                                                      │ 非阻塞
消费者线程
  ├─ JPEG 编码 RGB / Zstd 压缩 Depth
  ├─ ChunkBuilder 累积 -> 每 2 秒 flush -> chunk_XXXX.asc
  └─ store.add_chunk -> NetworkSink.upload (预留; 默认 Null 保留 pending)
Watchdog 线程 (adapter 内部): 500ms 无帧 / ts 不增长 / 空数据 / 全零 -> STALLED
主线程: 事件循环 (STALLED -> recover), 统计, 超时退出
```

约束：
- SDK 回调只做**深拷贝 + 入队**，绝不做编码/写盘/网络 —— 相机线程永不因下游阻塞
- 有界队列满时丢帧（`dropped` 计数），不阻塞相机
- 消费者线程阻塞只影响编码/落盘，不影响采集

## 3. 冻结 C ABI 契约（sdk.py 文件头，勿改）

1. 厂商 DLL 导出为 C++ mangled 符号，逻辑名↔mangled 映射见 `sdk.py MANGLED`（冻结）
2. 进程启动须先 `WSAStartup`（规避 libzmq 静态析构 assertion）
3. **进程退出必须 `kernel32.TerminateProcess`**（否则 exit code 1073741845）
4. 帧回调内 `AS_Frame_s.data` 指向 SDK 内部缓冲，**回调返回即失效** —— 所有权归 Adapter，必须深拷贝
5. `GetCamParameter` 仅 StartStream 后可读（规格 §3 顺序）
6. 640x480 下仅 `fps=30` 输出 RGB+Depth 成对完整流（§7A：15 档 depth 隔帧缺失、10/25 被拒）
7. `TS_SYSTEM_CLOCK` = epoch 毫秒；`rgb.ts == depth.ts`（同帧同戳）
8. SDK 返回 0=成功、-91=checkFpsCfg 拒绝；其余负值原样透传进 `CameraError.rc`

## 4. 相机状态机（规格 §2）

```
Disconnected -> Enumerating -> Opening -> Configuring -> Streaming
Streaming -> Stalled -> Reconnecting -> Opening -> Configuring -> Streaming
Reconnecting -> Disconnected   (连续 3 次恢复失败, 终止会话)
```

Stalled 触发条件（任一，Watchdog 每 100ms 检查）：
- 超过 500ms 无新帧（实验 A：p99=74ms，余量 6.7×）
- 连续 3 帧 SDK 时间戳不增长
- 连续 3 帧空数据/尺寸异常
- 连续 3 帧深度全零

恢复（`recover()`）按规格执行完整"重开设备"：StopStream → CloseCamera → 销毁句柄 → 重新枚举 → OpenCamera → SetStreamParam → StartStream → 等待首帧 → **重读标定并校验**（快照 sha256 变化发 `CALIB_CHANGED` 事件）。

## 5. 标定快照 + SHA-256（规格 §3）

首帧后 `GetCamParameter`，对结构体原始字节做 sha256：

```json
{
  "calibration_id": "sha256:c9bdc12bd398f71c...",
  "params_sha256": "c9bdc12bd398f71c...",
  "camera_serial": "ASC60CF08000539",
  "sdk_version": "1.2.14.20240129",
  "firmware_version": "DLS_VERc20865=:...",
  "config_version": "hp60c_v2_00_20230704_configEncrypt",
  "registration_enabled": true,
  "intrinsics": { "fxrgb": 566.16, "...": "..." },
  "read_utc_ms": 1722420000000
}
```

会话启动即生成，存 `session.json`；每次重连重新生成（100 次启停实测 sha256 恒定）。

## 6. 帧记录（规格 §4 时间戳协议）

每帧保存：`frame_id`（跨会话递增）、`sdk_ts_ms`（epoch ms）、`host_qpc`（QueryPerformanceCounter）、`host_mono_ms`、`utc_ms`、`arrival_mono_ms`（入队时刻）——可定位 SDK/USB/编码/网络各阶段延迟。

## 7. 2 秒 chunk 容器（`*.asc`，服务器端解析参考）

```
Header 96B: magic "ASCC" | version=1 | chunk_id(u64) | session_id(u64)
            | frame_count(u32) | first/last_frame_id(u64) | first/last_utc_ms(u64)
            | rgb/depth 宽高(u32x4) | jpeg_quality(u32) | depth_codec(u32=1 zstd)
Frame:      frame_id(u64) sdk_ts_ms(u64) host_qpc(u64) host_mono_ms(u64)
            utc_ms(u64) arrival_mono_ms(u64) rgb_size(u32) depth_size(u32)
            flags(u32, bit0=深度全零) | rgb JPEG | depth zstd
```

小端序，Python 解析用 `capagent.chunk.ChunkReader`；其他语言按上述偏移解析即可。

## 8. 磁盘循环缓存（~5GB）+ ACK（已废弃，见 §13）

> P2-13a/b 起 Capture Agent 改用 `capagent.spool.SessionSpool`（§13），本节的
> `ChunkStore` 循环缓存保留仅兼容旧测试/旧产物，不再被新 agent 使用。

- `store.root/chunk_*.asc` + `manifest.json`（幂等索引，崩溃后扫描重建）
- chunk 状态：`pending`（未确认）→ `acked`（服务器确认）
- `delete_on_ack=true`（默认）：ACK 即删文件；配额 5GB 是硬上限，超限删最旧（优先 acked）
- 断线续传：`pending_chunks()` 按时间升序返回未确认 chunk，重连后重传
- **网络接口预留**：`capagent.agent.NetworkSink` 抽象（`upload(chunk_path, meta) -> bool`），实现子类即接入上传；本轮 `NullNetworkSink` 仅本地落盘保留 pending

## 9. 配置与运行

```bash
# 单元测试 (无相机)
python test_unit.py

# P2-13a/b spool/sink 测试 (无相机)
python test_spool.py

# 离线端到端冒烟 (合成帧, 无相机) + 产物校验
python smoke_p2_13.py
python verify_spool_e2e.py audit_output\smoke_p2_13

# 100 次启停测试 (真机)
python test_restart.py --iters 100

# Capture Agent 运行 (默认 640x480/30fps/2s chunk/5GB spool)
python -m capagent.agent --seconds 3600
python -m capagent.agent --config capagent_config.json --root D:\capture_data

# 配置文件 (JSON, 覆盖默认值)
#   {"camera": {"serial": "ASC60CF08000539"}, "spool": {"root": "D:\\spool"},
#    "capture": {"jpeg_quality": 90}, "chunk": {"target_duration_ms": 2000},
#    "network": {"enabled": true, "url": "..."}}
```

固定参数（规格 §1 修正）：640×480、**请求 30 FPS**（§7A）、SYSTEM_CLOCK、RGB|DEPTH。

## 10. 与审计结论的衔接

| 审计结论 | 本实现 |
|---|---|
| 实际输出 ~23.3fps（请求 30） | 接受为事实，不依赖 15/25 档位 |
| RGB 内参反投影 = SDK 点云（自洽） | chunk 只存 Depth，服务器用标定模型反投影 |
| SDK 内参偏差 +3.5% 焦距 / +4.5% 尺度（实验 C） | 如需精确点云用实验 C 标定模型（fx=586.11 等），配置外置 |
| 帧停滞恢复以重开设备为单位 | §4 状态机 + recover() 完整流程 |
| 500ms 停滞阈值余量充分（p99=74ms） | watchdog 默认 500ms，可配 |

## 11. 测试结果（2026-08-01）

**单元测试**（`test_unit.py`，无需相机）：40 项断言全部通过 —— 状态机合法/非法转换与重连上限、JPEG/Zstd 编码往返、chunk 容器字段级往返、§4.4 切块（时间/容量/帧数/force）、循环缓存配额驱逐、ACK 删除、崩溃后索引重建、配置校验（fps=15 拒绝、spool 分级校验、capture_config_hash）。

**P2-13a/b 测试**（`test_spool.py`，无需相机）：115 项断言全部通过 —— crc32c 测试向量（e3069283）+ 分段组合、§4.3 十步原子提交、manifest 校验和一致、§10 ACK 闭环（无效 ACK 拒绝且不删数据）、崩溃恢复（孤儿 .part 清理 / .ready 无 manifest 重建 / .acked 残留清理 / 孤立 manifest 删除）、§12.4 使用率分级（normal/warning/throttle/stop 且**无驱逐删除**）、sink 三级完成语义（ACCEPTED→LOCAL_DURABLE→seal）、flush 屏障无帧丢失且按 §4.4 切块、有界队列满拒绝。

**100 次真机启停测试**（`test_restart.py --iters 100`，结果 `audit_output/test_restart_100.json`）：

| 指标 | 结果 |
|---|---|
| 完成 | **100/100**（耗时 569s，5.69s/次） |
| 成功到达 STREAMING + 收到首帧 | **100/100** |
| 单次启停耗时 | mean=4.91s p50=4.91s p95=4.95s max=5.08s |
| 标定快照 sha256 | **100 次唯一**：`c9bdc12bd398f71c718bceafa1b1ba5685a864322578e43071d1970cfe2ff17d` |
| 进程内存（WorkingSet） | 22.5 MB → 30.1 MB（+7.6 MB，无句柄/缓冲泄漏） |
| 失败 | 0 |

每次迭代执行完整 §2 恢复单位：枚举→打开→配置→启动→首帧→标定快照→STREAMING→停止流→关相机→销毁句柄。验证了 SDK 句柄生命周期在同一进程内 100 次往复无泄漏、标定读取稳定、状态机全程合法。

（SDK 内部日志 "RegisterClass fail" 为第二次及以后 listener 注册时的厂商内部非致命警告，不影响枚举与开流，实测 100 次均正常。）

## 12. 端到端录制验证（2026-08-01）

运行：`python capagent\agent.py --seconds 300 --root audit_output\capture_e2e`
产物验证：`python verify_e2e.py audit_output\capture_e2e`（chunk 头/帧数/时间戳/边界/JPEG+Zstd 解码/manifest/session）

| 指标 | 20s 冒烟 | 300s 完整录制 |
|---|---|---|
| 会话 | session=1（标定 sha256 与 100 次启停一致） | session=1（同一哈希） |
| 帧数 / 实际帧率 | 451 / 17.9 fps | 6692 / 21.9 fps |
| 丢帧 dropped | 0 | 0 |
| chunk 数 | 10 | 146（145×2s + 收尾 1×344ms partial） |
| chunk 边界 | 全部 ~2000ms（±3%） | 145 个 ~2010ms（±3%），末个为 stop force-flush partial |
| 平均编码耗时 | 3.5 ms/帧 | 3.5 ms/帧（帧间隔 ~45ms，消费者余量 >12×） |
| 存储 | 61 MB | 0.87 GB（5GB 配额未触发） |
| 停滞/恢复事件 | 0 | 0 |
| 时间跨度 | 20.1s | 300.0s（utc_ms 精确对齐） |
| 帧时间戳四件套 | 齐全（sdk_ts/host_qpc/host_mono/utc + arrival） | 抽查 292 帧全部通过 |
| JPEG 解码 / Zstd 解压 | 20 帧通过 | 292 帧全部通过（640×480 尺寸正确） |
| manifest ↔ 磁盘 | 一致，10 pending | 一致，146 pending |
| 退出 | exit 0（TerminateProcess 契约） | exit 0 |

结论：全链路（SDK 回调 → 有界队列 → JPEG/Zstd 编码 → 2s chunk → 磁盘循环缓存 → session 元数据）5 分钟连续录制无丢帧、无停滞、chunk 边界与时间戳正确、编码余量充足。预留网络接口（`NetworkSink.upload` → ACK 删除）未启用，chunk 全部保留 pending 待接入上传模块后闭环。

## 13. P2-13a/b 上传层本地实现（2026-08-01，规格《HP60C 上传协议与深度置信度规格 draft-1.0》§3/§4/§5/§10/§12.4）

本轮按"客户端 P2-13a/b 起步 + P2-13c/d/e 本地闭环"范围交付，**网络上传已在本地闭环验证**（真 capagent spool + NetworkDeliveryWorker → 本地 IngestServer，28 项检查）；t7920 真机联调待 tailscale 域名激活（见 §14）。核心原则（规格 §1）：上传层只保存原始 RGB/注册 Depth/时间戳/标定快照/采集元数据，不做点云生成、深度置信度最终计算、深度去畸变、SLAM/TSDF、原始 Depth 破坏性过滤。

### 13.1 组件

| 组件 | 职责 |
|---|---|
| `capagent.crc32c` | CRC-32C (Castagnoli) 纯 Python 查表实现。实测系统无 CRC32C 快速路径（ntdll.RtlComputeCrc32 为 IEEE 多项式 cbf43926，hashlib 构建无 crc32c），以标准向量 `crc32c(b"123456789")==0xE3069283` 锁定。~5.6MB/s，由独立写线程承担。 |
| `capagent.spool.SessionSpool` | 本地原子 Spool：`session.json`/`calibration.json`/`chunks/`/`manifests/`/`ack.jsonl`/`events.jsonl`/`seal.json`/`receipt.json`。§4.3 十步提交、§10 ACK 闭环、崩溃恢复扫描、§12.4 使用率分级。**无配额驱逐**——未 ACK 数据永不被删除。 |
| `capagent.sink.NetworkSink` / `LocalSpoolSink` | 上传通道抽象（§3.1/§3.2 三级完成语义 + §3.4 状态机）与本地实现：编码后帧 → 有界队列 → 写线程切块（§4.4）→ .part → finalize_chunk → .ready。 |
| `capagent.agent` | uuid 会话（协议要求）、submit→sink 流程、spool 使用率 stop 级暂停（§12.4）、session/calibration 分离写入（§4.1）。 |

### 13.2 目录布局与生命周期（root/<session_id>/）

```
session.json       会话元数据: session_id(uuid) / start_utc_ms / calibration_id / capture_config_hash
calibration.json   标定快照原文 (adapter.read_calibration())
chunks/            <seq:012d>.chunk.{part,ready,acked,error}
manifests/         <seq:012d>.json  幂等键 (session_id, chunk_seq) + payload 指纹
ack.jsonl          ACK 审计 (fsync 后 rename .acked → 删 payload/manifest)
seal.json          会话结束 seal (seal.pending = 已 ACK + 剩余 .ready, 不变量)
```

**本地提交顺序（§4.3）**：写 .part → 内部索引 → payload_bytes → CRC32C → SHA-256 → flush → fsync → rename .part→.ready → fsync 目录（Windows 尽力）→ 写 manifest。第 9 步后状态即 LOCAL_DURABLE，崩溃可恢复。

**崩溃恢复（幂等）**：孤儿 .part 清理；.ready 无 manifest 重建（ChunkReader 回读 + 重算指纹）；.acked 残留启动清理；孤立 manifest 删除。

**切块（§4.4）**：时间为主（2s，不假设固定帧数），同时受 64MiB payload 与 128 帧上限约束，force 收尾。实测 2s chunk 约 6.5MB，远低于 64MiB 上限。

### 13.3 约束落实

- 相机回调线程只做深拷贝 + 有界队列入队，**永不碰网络/磁盘**；编码在消费者线程；crc32c/sha256/落盘在 sink 写线程（§2 解耦）
- submit() 只入队（ACCEPTED），不等待服务器；网络故障不影响已生成的 .ready
- 未 ACK chunk 永不被循环缓存策略删除；spool 使用率只上报（normal 0.80 / throttle 0.90 / stop 0.97），stop 级由 Agent 暂停 submit（丢帧计数）
- flush_local 为屏障：排空队列 + 按 §4.4 提交，无帧丢失；seal 后 submit 拒绝
- 幂等键 (session_id, chunk_seq) + payload_sha256；ACK 校验身份字段，不匹配抛 ValueError 且不删数据

### 13.4 验证

- `python test_spool.py`：115 项断言（§4.3/§5/§10/§12.4/sink 三级语义/有界队列/crc32c 向量）
- `python smoke_p2_13.py` + `python verify_spool_e2e.py audit_output\smoke_p2_13`：离线端到端（uuid 会话 → 3×2s chunk → seal → 重启续传 → ACK 闭环 → 崩溃恢复），产物指纹重算一致
- 真机端到端（相机录制，2026-08-01）：
  - `python -m capagent.agent --seconds 300 --root audit_output\spool_real`：HP60C 640x480@30 连续录制 300s，**6593 帧 / 144 chunks / zero_depth=0 / stall=0 / dropped=0**，seal 后干净退出（exit 0），spool 使用率全程 normal
  - `python verify_spool_e2e.py audit_output\spool_real`：全部校验通过（uuid 会话、manifest 指纹重算一致、ChunkReader 回读、seal pending 不变量 144 = 0 ack + 144 ready）
  - 深度诊断工具 `python diag_depth.py`：绕过 watchdog 直接采样原始字节统计（depth uint16 非零比/min/max/均值 + RGB 非零比），用于区分物理遮挡 / SDK 深度流状态 / 管线问题。注：曾出现"深度全零 → 反复 STALLED"，根因是深度模组窗口被物理遮挡（镜头盖），移除后深度恢复（非零比 0.86-0.90），管线本身无需修改；完整诊断报告见 `audit_output/diag_depth_report.md`
- P2-13c/d/e 网络上传与服务端见 §14；P2-14/15 留待下轮（需规格原文落盘）

## 14. P2-13c/d/e 网络上传与服务端（2026-08-01，规格《HP60C 上传协议 draft-1.0》§8-§11）

按用户交付的参考实现（`hp_upload_ref`，`p2_13_ref` fc /b 零差异比对）完成生产接线：客户端双通道 + 服务端 IngestServer，且补齐参考实现明确要求"对接时必须补齐"的两项——**P2 chunk 验证器注入**（`run_ingest_server.py` → `chunk_validator=validate_chunk`）与 **seal 嵌入 session_manifest**（sink `_seal_protocol_fields`）。

### 14.1 客户端（capagent.network_worker.NetworkDeliveryWorker）

- 独立网络线程轮询 spool 新 `.ready` 上传（`notify_ready_chunk` 唤醒）；`flush_remote`/`commit_session` 持 `_op_lock` 与后台循环互斥（NetworkUploader 非线程安全）
- 双通道：`/v1/control`（JSON，单接收者线程，request_id/chunk_seq 路由）+ `/v1/stream`（二进制，128B 小端传输头 `struct("<4sHH36sI36sQI32s")`，magic `HPC1`，全 chunk 分片，单 send lock）
- 协议幂等（§8.1）：**0 基连续 chunk_seq**；幂等键 `(session_id, chunk_seq)`；内容身份 `payload_sha256`；manifest `chunk_id` 为 attempt 级 **uuid**（容器头 u64 仅诊断，`_chunk_id`/`_chunk_uuid` 解耦）
- 错误分类：FATAL（AUTH_FAILED/CHUNK_CONFLICT/SESSION_ERROR/PROTOCOL_ERROR/PAYLOAD_REJECTED）不再重试、暴露 `last_error` 并 `mark_error`；RETRYABLE 退避 1/2/4/8/15s ±20% 抖动
- 提交闭环全权在 `spool.ack_chunk`（§10）：服务端 ACK → rename `.acked` → 删 payload/manifest；未 ACK chunk 永不被删除

### 14.2 服务端（hp_upload_ref + run_ingest_server.py，t7920 部署）

- 提交链（§9.1）：边收边验字节/CRC32C/SHA-256 → `chunk_validator`（P2 真实容器校验：magic/version/depth_codec==zstd/frame_count/first+last frame_id/RGB JPEG SOI/depth zstd 解压 640×480×2）→ fsync `.part` → 幂等 manifest → 原子 rename → fsync 目录 → append journal → fsync → 才发 ACK
- `seal_session`（§11）：`actual_sequences == list(range(expected_count))` 硬校验；`chunk_manifest_sha256`/`session_manifest_sha256` canonical JSON 双向校验；session_manifest 嵌入
- `run_ingest_server.py`：复用参考实现 `build_parser`/`_read_token`（--root/--host/--port/--token/--token-file/--max-chunk-bytes/--max-inflight/--min-free-bytes），注入 `capagent.chunk_validator.validate_chunk`，SIGINT/SIGTERM 优雅停止；已冒烟（--help 全参数 + 起停，启动打印 `chunk_validator=capagent`）

### 14.3 运行

t7920 服务端（Windows 采集端需 `ws://t7920.tailb2b872.ts.net:4170` 可达）：

```bash
python run_ingest_server.py --root D:\hp_upload\store --host 0.0.0.0 \
    --port 4170 --token-file C:\hp_upload\token.txt
tailscale serve --bg http://127.0.0.1:4170
```

Windows 采集端开启网络上传（`capagent_config.json`）：

```json
{"network": {"enabled": true,
             "url": "ws://t7920.tailb2b872.ts.net:4170",
             "token": "…"},
 "spool": {"root": "D:\\spool"}}
python -m capagent.agent --config capagent_config.json --seconds 3600
```

### 14.4 本地闭环验证（无需相机）

- `python test_net_integration.py`：**28 项检查全过** —— 真 spool（FrameEncoder 真实 JPEG+zstd）3 chunk 上传 COMMITTED + ACK 删除闭环 + SESSION_SEAL 双向 digest + `sink.commit_session` 写 receipt.json + 服务端 `contiguous_committed_through=2`/`ranges=[[0,2]]`/状态 COMMITTED；同内容不同 chunk_id 重传 → ALREADY_COMMITTED=2 且 pending 清空；同 session 同 seq 不同 SHA → CHUNK_CONFLICT → 本地 `.error`；损坏容器（非 JPEG rgb + 非 zstd depth）→ PAYLOAD_REJECTED → `.error` 且服务端零提交
- `python test_spool.py`（115 项）与参考实现 10 tests 回归全过；`run_ingest_server.py --help` + 起停冒烟通过
- **跨机真机联调（2026-08-01，t7920 已激活，全部通过）**：`python test_net_t7920.py [token] [--url ...] [--idempotent] [--reject]`
  - `ws://t7920.tailb2b872.ts.net:4170` 直连：3 chunk 上传 COMMITTED + ACK 删除闭环 + SESSION_SEAL 双向 digest + receipt.json（7 项）
  - `wss://t7920.tailb2b872.ts.net`（tailscale serve 443 生产路径）：2 chunk 全链路通过（7 项）
  - `--idempotent`：同内容不同 chunk_id 重传 → ALREADY_COMMITTED=2 / uploaded=0 / pending 清空（4 项）
  - `--reject`：损坏容器跨机被 PAYLOAD_REJECTED 拒收（detail "帧 1 RGB 非 JPEG (SOI 缺失)"）+ 本地 `.error`（2 项）——证明 t7920 部署为注入 P2 验证器的 `run_ingest_server.py`
- 状态：**P2-13c/d/e 跨机联调完成**；P2-14 ACK 闭环/崩溃注入、P2-15 depth confidence 待下轮

## 15. P2-14 ACK 闭环/故障注入 与 P2-15 深度置信度（2026-08-01，规格《HP60C 上传协议 draft-1.0》§10/§14/§15/§20）

### 15.1 P2-14 ACK 删除闭环与崩溃恢复（全部通过）

- `python test_p2_14_faults.py`：**40 项故障注入检查全过**（进程级 taskkill /F + `_fault_runner.py` monkeypatch 精确窗口）
  - A 客户端写入中强杀 → 孤儿 `.part` 清理、无 manifest 残留、session.json 完好
  - B 发送中强杀（payload 已发 ACK 未处理）→ 服务端已提交 `committed_ranges=[[0,0]]`，重启重传 → ALREADY_COMMITTED=1 闭环，`.ready` 删除
  - C ACK 三窗口（journal 前 / rename 前 / delete 前）→ 各自恢复语义（`ack.jsonl` 记录、`.ready` 保留、`.acked` 清理）
  - D 服务端强杀 + 重启 → 已提交状态可查询（验收 7）+ 续传幂等闭环（3 chunk 全量）+ SESSION_SEAL COMMITTED
  - E SESSION_SEAL 缺 chunk → 服务端拒绝完成（SessionError），无 receipt，会话保持 OPEN

### 15.2 P2-15a/b 深度置信度（参考实现 + 生产接线 + 真机批量验收）

- 参考实现：用户交付 `D:\BaiduNetdiskDownload\p2_15_depth_confidence_reference.zip`，解压至 `p2_15_ref/`（SHA256SUMS 全部校验通过，自带 22 项 pytest 全过）；生产包 `depth_confidence/` 与参考实现 **fc /b 零差异**
- 算法（规格 §14）：`C = valid_mask × neighborhood_support(5×5) × local_consistency(median/MAD) × discontinuity_penalty × isolated_pixel_penalty`；硬无效 depth==0 / <250mm / >2500mm / source_mask=False；reason_bits（§15）bit6/7 V1 恒 0；无随机/无时间/无跨帧状态，同输入同配置输出 hash 一致
- 生产接线：`capagent/confidence.py`（ChunkReader 解 zstd depth → `depth_confidence.batch.DepthFrame` → `compute_confidence_batch`），原始 Depth 永不被修改
- 真机批量验收 `python verify_confidence_e2e.py [spool_root] [--max-frames N]`：spool_real 6569 帧流式抽样 143 帧全过 —— 单帧功能 smoke、确定性双跑逐字节一致、`tile_rows=7 vs 128` 输出一致、NPZ 输出、审计 JSONL（fsync，含 algorithm/config/depth/input/output hashes + reason bit counts + confidence 分位数）、**单帧 p50≈2.2s / p95≈3.4s（本机纯 Python 参考实现，非实时内核）**、峰值 RSS 226MB（流式抽样，不整 spool 载入内存）
- 结果落盘 `audit_output/confidence_v1/`：config.json（canonical + sha256）、confidence_audit.jsonl、npz/<frame_id>.npz（confidence_u8 + reason_bits_u8）、summary.json
- 状态：**P2-15a/b 完成**；sigma 噪声模型参数为 **provisional 临时值**（a=2.0mm, b=1.0mm/m², floor=2.0mm, discontinuity_scale=0.03, isolation=20mm），**P2-15d 平面距离实验后必须替换**（见 §15.3 P2-15c MCAP 集成完成）

### 15.3 P2-15c MCAP / ROS Topic 集成（验收通过）

- `capagent/mcap_writer.py`：ROS 2 CDR 序列化（little-endian）+ MCAP 写入，topic 命名按规格 §18——`/hp60c/depth_registered/image_raw`（sensor_msgs/Image 16UC1，原始 depth 永不修改）、`/confidence`（mono8）、`/confidence_reason`（mono8）、`/camera_info`（CameraInfo，实验 C 标定内参 fx=586.11/fy=589.18/cx=330.83/cy=233.56/k1-3）；每次任务写 metadata 审计（algorithm_id / software_version git / config_sha256 / input_session_sha256）
- 依赖：`mcap` 1.4.0（纯 Python，profile="ros2" + CDR 编码，可由 mcap / rosbag2 直接回放；无 ROS 环境依赖）
- `verify_mcap.py`：**12 项验收全过**（143 帧抽样）——MCAP 写入 36MB、3 图像 channel + camera_info 消息数正确、mcap reader 回放 depth/confidence/reason **逐字节一致**、metadata 审计四字段齐全；产物 `audit_output/mcap_v1/confidence_<gitsha>.mcap`（mcap_sha256=dce7ec62...）+ summary.json
- 状态：**P2-15c 完成**；P2-15d 距离噪声标定实验（真机+标定板，替换 sigma 临时参数）、P2-15e TSDF 阈值冻结待下轮
