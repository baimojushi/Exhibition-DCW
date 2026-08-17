# HP60C RGB-D 相机审计项目

> **项目负责人**：Angstrong HP60C 审计组
> **工作目录**：`D:\项目\交互艺术\展厅孪生\ai\hp60c_audit`
> **远程仓库**：https://github.com/baimojushi/Exhibition-DCW
> **远程机**：t7920 (Tailscale: `t7920.tailb2b872.ts.net:4171`, qwen daemon)

---

## 项目概述

验证 Angstrong HP60C RGB-D 相机的 SDK/网络协议行为，制定并落盘规格基线（`SPEC_phase2_fixed.md`），为下游服务器端 RTAB-Map/TSDF 方案做前置验证。

**核心交付物**：
- **Capture Agent** (Windows 端)：SDK 回调 → 有界队列 → JPEG/Zstd 编码 → 2s chunk → 本地 Spool → 网络上传
- **上传协议** (P2-13/14/15)：双通道 WebSocket + CRC32C/SHA-256 幂等 + ACK 删除闭环 + 故障注入验收
- **深度置信度** (P2-15a/b/c)：参考实现接入 + 生产接线 + MCAP/ROS Topic 集成 + 真机批量验收
- **DCW Backend** (P2-DCW-01)：OrbbecSDK v1 拉取式后端 + 身份 JSON/模式枚举/D2C 配置
- **ALG-WALL 系列**：连续墙面重建算法验证（plane+pose 优化拉直整墙 ↓78%）

---

## 目录结构

```
hp60c_audit/
├── SPEC_phase2_fixed.md          # 规格基线（冻结配置/状态机/标定/时间戳/上传协议）
├── README.md                      # 本文件
├── README_CAPTURE_AGENT.md        # Capture Agent 详细交付说明
├── capagent/                      # Capture Agent 核心代码
│   ├── agent.py                   # 主流程 (CLI)
│   ├── config.py                  # 配置加载
│   ├── denoise.py                 # 时序中值去噪 (P2-15d)
│   ├── visualizer.py              # 实时点云+XYZ倾斜角可视化
│   ├── adapter.py                 # SDK Adapter (生命周期/标定/Watchdog/恢复)
│   ├── dcw_backend.py             # DaBai DCW Backend (OrbbecSDK v1)
│   ├── encoder.py                 # JPEG/Zstd 编码
│   ├── chunk.py                   # 2s chunk 容器格式
│   ├── spool.py                   # SessionSpool (原子提交/ACK/崩溃恢复)
│   ├── network_worker.py          # 网络上传双通道
│   ├── confidence.py              # 深度置信度生产接线
│   ├── mcap_writer.py             # MCAP/ROS Topic 写入
│   └── ... (errors, frames, state, store, sink, crc32c, chunk_validator)
├── sdk.py                         # ABI 冻结层 (厂商 DLL ctypes 绑定)
├── cap_viz.py                     # 一键启动采集+可视化
├── denoise_gallery.py             # 离线 gallery 数据去噪分析
├── run_ingest_server.py           # 服务端 IngestServer (t7920 部署)
├── test_*.py                      # 单元/集成/故障注入/真机验收测试
├── verify_*.py                    # 产物验证脚本
├── audit_output/                  # 所有采集/验收产物 (gitignored)
│   ├── spool/                     # SessionSpool 目录 (按 session_id 分组)
│   ├── cap_lr_wall_v3b/           # 静态墙面标定数据 (4 序列, 498 帧)
│   ├── confidence_v1/             # 深度置信度验收产物
│   ├── mcap_v1/                   # MCAP 文件
│   └── alg_wall_02/               # ALG-WALL-02 优化结果
└── plane_*.py                     # Plane MCP 工作项同步脚本
```

---

## 阶段进展总览

| 阶段 | 状态 | 关键交付 | 验收结果 |
|------|------|----------|----------|
| **P2-13a/b** | ✅ 完成 | Spool 原子提交/ACK/崩溃恢复/使用率分级 | 115 项断言全过 + 真机 300s 录制 0 丢帧 |
| **P2-13c/d/e** | ✅ 完成 | 双通道上传 + 服务端 IngestServer + 跨机联调 | 28 项检查全过 + t7920 跨机 4 场景全过 |
| **P2-14** | ✅ 完成 | ACK 删除闭环 + 40 项故障注入 (进程强杀/ACK 三窗口/服务端强杀) | 40 项全过 |
| **P2-15a/b** | ✅ 完成 | 深度置信度参考实现 + 生产接线 + 真机批量验收 | 6569 帧抽样 143 帧全过，p50≈2.2s/帧 |
| **P2-15c** | ✅ 完成 | MCAP/ROS Topic 写入 + 回放验收 | 12 项验收全过，逐字节一致 |
| **P2-15d** | 🔄 进行中 | 距离噪声标定 (平面距离实验，替换 sigma 临时参数) | 分析脚本修复 + 冒烟验证 + 采集脚本就绪 |
| **DCW-01** | ✅ 完成 | DaBaiDCWBackend + 身份 JSON/模式枚举/D2C 配置 + 无设备冒烟 47 项 | 全过 |
| **ALG-WALL-02** | ✅ 完成 | Plane+Pose 优化拉直整墙 29.5→6.5mm (↓78%) | v3b 数据验证通过 |
| **ALG-WALL-M3D / WALL-QA** | ⏳ 待推进 | Metric3D 墙面先验评估 / 8/15 前连续墙面生产验收 Gate | 依赖 8/15 采集数据 |

---

## 关键技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| **SDK 版本** | OrbbecSDK v1.10.35 (DCW) / v1.2.14 (HP60C) | DCW 为 OpenNI 协议必须走 v1；HP60C 厂商 DLL 仅 v1 稳定 |
| **进程退出** | `kernel32.TerminateProcess` | 避免 libzmq 静态析构 assertion (exit code 1073741845) |
| **相机线程** | 仅深拷贝+入队，绝不编码/写盘/网络 | 保证相机线程永不阻塞，Watchdog 语义纯净 |
| **Chunk 格式** | 自描述二进制 `*.asc` (magic ASCC) | 服务器端无需 Python 即可解析，跨语言友好 |
| **上传幂等键** | `(session_id, chunk_seq)` + `payload_sha256` | 协议 §8.1 要求，支持重传去重 |
| **ACK 删除** | 仅 ACK 后 rename `.acked` → 删 payload/manifest | 未 ACK 数据永不被循环缓存策略删除 |
| **深度置信度** | 原始 Depth 永不修改，仅输出 confidence/reason | 规格 §14/§15 要求，下游 TSDF 自行阈值 |
| **MCAP 编码** | ROS 2 CDR little-endian + profile="ros2" | 可被 mcap/rosbag2 直接回放，无 ROS 环境依赖 |
| **时序去噪** | 滑动窗口中值 (window=5)，忽略零深度 | 静态场景 ~1.9x 降噪 (50→26mm)；漫游需相机静止生效 |

---

## 运行指南

### 环境要求
- Windows 10/11, Python 3.10+
- HP60C SDK (厂商 DLL) 放入 PATH 或 `sdk.py` 指定路径
- 依赖：`numpy`, `opencv-python`, `zstandard`, `mcap`, `requests`, `websockets`

```bash
# 安装依赖
pip install numpy opencv-python zstandard mcap requests websockets
```

### Capture Agent 运行

```bash
# 单元测试 (无相机)
python test_unit.py

# 离线端到端冒烟 (合成帧)
python smoke_p2_13.py
python verify_spool_e2e.py audit_output\smoke_p2_13

# 真机采集 (默认 640x480@30fps, 2s chunk, 5GB spool)
python -m capagent.agent --seconds 3600

# 带去噪 + 可视化采集 (需 t7920 qwen daemon 运行)
python cap_viz.py --config capagent/config_denoise.json --seconds 60

# 自定义配置
python -m capagent.agent --config capagent_config.json --root D:\capture_data
```

### 服务端部署 (t7920)

```bash
# 启动 qwen daemon (t7920 本机)
cd D:\ai\qwen-code\bin
qwen.exe serve --port 4171 --token <TOKEN>

# 启动 IngestServer (t7920)
python run_ingest_server.py --root D:\hp_upload\store --host 0.0.0.0 --port 4170 --token-file C:\hp_upload\token.txt
tailscale serve --bg http://127.0.0.1:4170
```

### 验收测试

```bash
# Spool/上传/故障注入
python test_spool.py              # 115 项
python test_p2_14_faults.py       # 40 项故障注入
python test_net_integration.py    # 28 项本地闭环

# 深度置信度
python verify_confidence_e2e.py audit_output\spool_real --max-frames 143

# MCAP
python verify_mcap.py audit_output\mcap_v1\confidence_<gitsha>.mcap

# 跨机联调 (需 t7920 可达)
python test_net_t7920.py <TOKEN> [--url ws://t7920.tailb2b872.ts.net:4170] [--idempotent] [--reject]
```

---

## 数据产物说明

| 目录 | 来源 | 规模 | 用途 |
|------|------|------|------|
| `audit_output/spool/` | Capture Agent 真机录制 | ~1.2 GB/session | 完整原始数据 (RGB JPEG + Depth zstd + 元数据) |
| `audit_output/cap_lr_wall_v3b/` | 静态墙面标定采集 | 276 MB (498 帧) | 去噪基线 / 平面拟合 RMS 评估 |
| `audit_output/confidence_v1/` | P2-15a/b 验收 | NPZ + JSONL | confidence_u8 + reason_bits_u8 + 审计日志 |
| `audit_output/mcap_v1/` | P2-15c 验收 | ~36 MB | MCAP 文件 (depth/confidence/reason/camera_info) |
| `audit_output/alg_wall_02/` | ALG-WALL-02 优化 | JSON/NPZ | plane+pose 优化后墙面网格 |

---

## 远程协作

- **t7920 访问**：`http://t7920.tailb2b872.ts.net:4171` (明文 HTTP, Bearer token)
- **Plane 项目管理**：`https://t7920.tailb2b872.ts.net/molink` (MCP 接入, workspace `molink`, project `6198137d`)
- **GitHub**：https://github.com/baimojushi/Exhibition-DCW

---

## 待办 / 下一步

1. **P2-15d 距离噪声标定** — 真机 + 标定板采集，拟合 half-normal sigma，替换临时参数
2. **P2-15e TSDF 融合阈值冻结** — 基于置信度分位数确定截断阈值
3. **WALL-QA (8/15 前)** — 连续墙面生产验收 Gate，决定生产路径+冻结阈值+写采集 checklist
4. **ALG-WALL-M3D** — Metric3D 墙面先验评估，target 08-14
5. **8/15 展厅正式采集** — 正对平移走扫 (≤10° 视线角)、连续单墙≥5m、同路径正反向各一条、好帧率≥60%

---

## 许可

内部审计项目，非开源发布。所有代码与数据仅限项目组内部使用。