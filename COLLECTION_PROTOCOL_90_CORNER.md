# 90° 墙角采集规范 (Corner Collection Protocol)

> **目的**：获取真实 90° 墙角数据，用于 Audit 03 held-out 验证角度/角线准确性
> **前置条件**：DCW 生产 profile 已冻结 (production_profile.json)，t7920 qwen daemon 运行中

---

## 1. 环境要求

| 项目 | 要求 |
|------|------|
| **墙面** | 两面相互垂直的平整墙面 (90° ± 1°)，无挂画/装饰/插座干扰 |
| **墙面尺寸** | 单墙宽 ≥ 3m，高 ≥ 2.5m |
| **光照** | 均匀漫射光，避免直射阳光/强反光，墙面亮度 200-500 lux |
| **地面** | 平整，无高差，标记相机轨迹线 |
| **相机高度** | 1.2-1.5m (三脚架/云台固定) |

---

## 2. 相机配置 (DCW 生产 Profile)

```json
{
  "camera": {
    "dcw_backend": true,
    "dcw": {
      "color": {"width": 640, "height": 480, "fps": 30, "format": "RGB"},
      "depth": {"width": 640, "height": 360, "fps": 30, "format": "Y16"},
      "d2c": {"enabled": false, "align_mode": "OFF"}
    }
  },
  "capture": {"denoise": {"enabled": false, "window_size": 5}},
  "run": {"visualize": true, "seconds": 60}
}
```

**关键**：
- D2C **必须关闭** (manual reprojection)
- 去噪 **关闭** (保留原始噪声分布)
- 可视化 **开启** (实时监控 Pitch/Yaw)

---

## 3. 采集动作规范 (SOP)

### 3.1 相机放置
```
墙角线 (垂直线)
    │
    │  ← 相机光轴正对墙角线 (yaw ≈ 0°)
    │
    ▼ 相机高度 1.2-1.5m
```

### 3.2 采集序列 (每个序列 60s)

| 序列 | 动作 | 速度 | 视线角 | 距离范围 | 目的 |
|------|------|------|--------|----------|------|
| **C01** | 静止正对 | — | 0° | 1.5m | 基线：零运动模糊 |
| **C02** | 慢速平移 | 0.05 m/s | 0° | 1.5m → 3m → 1.5m | 正对平移扫描 |
| **C03** | 慢速平移 | 0.05 m/s | 0° | 3m → 1.5m | 反向闭环 |
| **C04** | 静止偏航 | — | +10° | 2m | 视线角影响 |
| **C05** | 静止偏航 | — | -10° | 2m | 对称性验证 |
| **C06** | 慢速平移 | 0.05 m/s | +10° | 2m → 3m | 组合运动 |
| **C07** | 快速平移 | 0.2 m/s | 0° | 1.5m → 3m | 运动模糊上界 |

### 3.3 关键约束
- **Pitch/Yaw 实时监控**：可视化窗口保持绿色 (<5°)
- **连续采集**：序列间不停相机，仅暂停移动
- **距离标记**：地面贴胶带标记 1.5m/2m/2.5m/3m 位置
- **时间同步**：每序列开始/结束口头报时 (UTC)

---

## 4. 数据产物

```
audit_output/
└── corner_90deg_YYYYMMDD/
    ├── C01_static_facing/
    │   ├── chunks/           # *.chunk.ready
    │   ├── identity.json     # 设备身份
    │   └── meta.json         # 采集元数据
    ├── C02_slow_facing_fwd/
    ├── C03_slow_facing_rev/
    ├── C04_static_yaw_p10/
    ├── C05_static_yaw_n10/
    ├── C06_slow_yaw_p10/
    └── C07_fast_facing/
```

**meta.json 必填字段**：
```json
{
  "sequence": "C02",
  "description": "慢速正对平移 1.5→3→1.5m",
  "start_utc": "2026-08-17T10:30:00Z",
  "end_utc": "2026-08-17T10:31:00Z",
  "camera_height_m": 1.35,
  "wall_corner_verified_deg": 90.0,
  "distance_marks_m": [1.5, 2.0, 2.5, 3.0],
  "operator": "xxx"
}
```

---

## 5. 验收标准 (Gate)

| 指标 | 门槛 | 备注 |
|------|------|------|
| **好帧率** | ≥ 60% | 零深度 < 40% 的帧 |
| **Pitch/Yaw** | < 5° (全程) | 可视化窗口全绿 |
| **覆盖连续性** | 无断层 | 角线区域每帧有效点 > 500 |
| **反向闭环** | C02 vs C03 角线位置差 < 20mm | 验证无系统漂移 |

---

## 6. 运行命令

### t7920 (远程机) 启动 qwen daemon
```cmd
cd D:\ai\qwen-code\bin
qwen.exe serve --port 4171 --token sk-ENLFFTIfY5X6cmjN5PVhHXzKnfzzswsR
```

### 本地启动采集 (每序列)
```bash
# 静止正对
python qwen_remote.py "cd /home/baimojushi/ai/hp60c_audit && python cap_viz.py --config capagent/config_denoise.json --seconds 60" "D:\ai\qwen-code\bin"

# 修改配置文件中的 run.seconds 即可复用
```

---

## 7. 事后分析

```bash
# 1. 将 t7920 spool 数据同步回本地
# 2. 运行 Audit 03
python audit_03_corner_holdout.py --data-dir audit_output/corner_90deg_YYYYMMDD

# 3. 关键输出
# - 角度误差分布 (目标: median < 1°)
# - 角线位置误差 (目标: median < 20mm)
# - C02 vs C03 闭环差异
```

---

## 8. 风险与应对

| 风险 | 症状 | 应对 |
|------|------|------|
| 墙面非 90° | 角度误差系统性偏移 | 事前用激光经纬仪验证墙角 |
| 反光/玻璃 | 深度空洞/飞点 | 避开或贴哑光纸 |
| 相机高度变化 | Pitch 漂移 | 三脚架锁死，每序列前检查 |
| t7920 断连 | 采集中断 | 启动前 `tailscale ping t7920` 确认 |

---

**版本**: v1.0 (2026-08-17)
**负责人**: HP60C 审计组