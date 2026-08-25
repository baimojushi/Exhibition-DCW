# HP60C 重建管线 - 工作计划 (2026-08-25)

> 基于 dae46582 会话复核结论制定，三层问题并行推进，Loop Closure 优先级最高。

---

## 🎯 核心目标
- 消除大尺度轨迹漂移/扭曲（Loop Closure）
- 验证 Recovery 边的运动学合理性
- 修复 TSDF 多段产物结构与碎片问题
- 建立可复现的 QC 验证流程

---

## 1️⃣ Loop Closure - 最高优先级
*解决主段 118 KF / 6.58m / 0 loop edges 的根本漂移源*

- [x] **候选生成**：`stages/loop_closure.py` 新建
  - [x] 轨迹空间近邻搜索（节点间隔 >10, 位置 <0.3m, 方向 <30°）
  - [x] 输出候选对列表 `(i, j, dist, angle_diff)`
- [x] **ICP 验证管线**
  - [x] Coarse→Fine ICP 两阶段验证（复用 `_multiscale_icp`）
  - [x] 阈值：fitness ≥ 0.65, RMSE ≤ 8mm, 双向一致性 PASS
  - [x] 标记 `uncertain=True` 送入 PGO
- [x] **PGO 集成**
  - [x] `stages/icp.py` `refine_registration` 集成 `find_loop_edges`
  - [x] 目标：5–20 条高可靠回环约束
- [ ] **验证指标**
  - [ ] 主段 orientation drift < 1°/m
  - [ ] 重投影误差中位数 < 5mm
  - [ ] TSDF 单层无双层/撕裂

---

## 2️⃣ TSDF 多段产物结构与碎片清理 - 立即修复
*当前 QC 只看 segment_00，真正主体 segment_01 被忽略；3,145 components 需清理*

- [x] **产物结构重构** `stages/tsdf_integration.py` / `stages/qc.py`
  - [x] `tsdf_result.json` → `segments: [{id, mesh_path, integrated_frames, pose_origin}, ...]`
  - [x] `qc_report.json` → `per_segment_mesh_metrics[]` + `aggregate_mesh_metrics`
  - [x] 禁止直接叠加显示（各段各自 identity 起始）
- [x] **Depth 预滤波管线** `stages/prefilter.py` 新建
  - [x] Discontinuity / flying-pixel filter
  - [x] Spatial bilateral filter
  - [x] Invalid boundary erosion
- [x] **TSDF 后 Component Cleanup**
  - [x] 删除同时满足：faces < 100 **AND** bbox diagonal < 30mm 的小组件
  - [x] Degenerate / skinny face cleanup（面积 < 1mm², aspect ratio 极值）
- [ ] **验证阶段降分辨策略**
  - [ ] 先用 8–10mm voxel + truncation 3–4 voxels 验证轨迹
  - [ ] 轨迹稳定后恢复高分辨率

---

## 3️⃣ Recovery Continuity Gate - 并行推进
*6 条 recovery 已接上，但 23→24 等边运动学异常（0.95 m/s, 91° 瞬时转向）*

- [x] **保存 refine 前后 delta** `stages/icp.py` `_recover_rejected_edge`
  - [x] `metadata` 新增：`T_composed`, `T_endpoint_refined`, `delta = inv(T_composed) @ T_endpoint_refined`
  - [x] 仅当 `||delta||_F < ε` 且 residual 改善时允许替换
- [x] **运动学连续性检查** `stages/continuity_gate.py` 新建
  - [x] 真实 Δt 计算：linear velocity, angular velocity
  - [x] Linear / angular acceleration
  - [x] Direction change（相对前一运动向量夹角）
  - [x] 阈值参考：v < 1.5 m/s, ω < 120°/s, direction_change < 60°（可调）
- [x] **优先复核区段**：22→23→24→25（本 session 第一优先级）
- [x] **拒绝策略**：不通过 gate 的 recovery 标记 `rejected_continuity`，保持轨迹分段

---

## 4️⃣ 验证与回归测试
- [x] `test_final_verify3.py`：集成 Loop Closure + Continuity Gate + 多段 TSDF (已创建，需 open3d 环境运行)
- [ ] 回归 dae46582：Validation FAIL → PASS，轨迹分段正确，QC 检测跳变
- [ ] 新增合成数据集：已知 GT 轨迹 + 合成 depth，端到端验证
- [ ] 性能基线：Loop Closure 候选生成 < 2s，ICP 验证 < 5s/会话

---

## 📋 执行顺序建议
```
Week 1: Loop Closure 候选生成 + ICP 验证 + PGO 集成
Week 2: TSDF 多段结构 + Depth 预滤波 + Component Cleanup
Week 3: Recovery Continuity Gate + 运动学阈值调优
Week 4: 集成测试 + 回归 + 文档化
```

---

## 🔗 关键文件映射
| 模块 | 文件 | 状态 |
|------|------|------|
| Sequential ICP | `stages/icp.py` | ✅ 完成 |
| Loop Closure | `stages/loop_closure.py` | ✅ 完成 |
| Continuity Gate | `stages/continuity_gate.py` | ✅ 完成 |
| Depth Prefilter | `stages/prefilter.py` | ✅ 完成 |
| TSDF Integration | `stages/tsdf_integration.py` | ✅ 完成 |
| QC | `stages/qc.py` | ✅ 完成 |
| 验证脚本 | `test_final_verify3.py` | **新建** |

---

## ✅ 已完成（历史参考）
- [x] 核心修复：fallback 不再接受 Fine ICP 拒绝边（2026-08-24 验证通过）
- [x] 轨迹正确分段：4 段 [0-39], [40-42], [43-57], [58-67]
- [x] 3 处断裂边被隔离，PGO 不再硬接
- [x] QC 检测到轨迹跳变：620mm / 28.6°
- [x] Intermediate Recovery 基础框架：6 条成功接上

---

> **更新规则**：完成项打勾 ✅，新增项追加，优先级变更标注原因。每日立会前同步此文件。