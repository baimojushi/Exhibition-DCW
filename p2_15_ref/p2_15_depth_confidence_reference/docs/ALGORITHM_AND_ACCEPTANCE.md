# P2-15 V1 算法与验收映射

## 规格映射

| 规格要求 | 实现位置 |
|---|---|
| `depth==0 → 0` | `core.py: raw_invalid` |
| `<250 / >2500 → 0` | `core.py: out_of_range` |
| 无来源或越界 → 0 | `source_mask=False` 与 bit5 |
| 5×5 支持度 | `_window_valid_count()` |
| median / MAD | `_local_statistics_tiled()` |
| `sigma_effective=max(...)` | `compute_confidence_result()` |
| 指数局部一致性 | `local_consistency` |
| 最大相对深度差 | `gradient_ratio` |
| 3×3 单有效 | `single_valid` |
| 与所有邻居差异超阈值 | `differs_from_all_neighbors` |
| 小连通区 | `_small_components()` |
| bit0..bit5 | `ReasonBits` |
| bit6 / bit7 恒 0 | 运行时断言 + 单元测试 |
| 禁止朴素时间一致性 | API 无时间输入、无历史状态 |
| 原始 Depth 不修改 | 私有 `float32 copy` + 单元测试 |
| 参数哈希和算法版本 | `ConfidenceResult` / `audit.py` |

## 输出语义

`confidence_u8` 是几何融合相对权重，不是：

- 真实误差上界；
- 硬件 confidence；
- 深度真值；
- 每像素概率校准结果；
- 标定准确性的证明。

在 P2-15d 完成前，不能把某个数值阈值解释为固定毫米误差保证。

## source_mask

输入图本身没有“越界数组元素”。因此“无来源/越界像素”通过与 depth 同形状的 `source_mask` 表达：

```text
True   当前输出像素有合法来源
False  重映射没有来源，或源坐标在输入图外
```

当 `source_mask=False`：

- confidence 强制 0；
- bit5 设置；
- 如果 depth 同时为 0，bit0 和 bit5 可同时设置。

## 连通域定义

小连通区使用 8 邻接，在硬有效 mask 上计算。面积阈值默认 `<4`。

这一步与图像内容相关，是真正的单帧空间规则，不包含任何时间信息。

## 参数冻结建议

从 `audit_output/spool_real` 先输出以下统计：

```text
每帧 hard_valid_ratio
confidence p05/p25/p50/p75/p95
bit2/bit3/bit4 占比
isolated_rejected_count
不同深度区间的 sigma/MAD 分布
```

随后对平面实验按距离拟合：

```text
sigma_model(z) = a + b × z²
```

冻结时保存：

```text
algorithm_id
algorithm_version
config JSON
config SHA-256
拟合数据 session IDs
拟合脚本 Git commit
验证报告 hash
```
