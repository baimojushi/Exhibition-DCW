# P2-15a / P2-15b 单帧深度置信度参考实现

这是一个独立、确定性、可审计的 NumPy/SciPy 实现，面向 HP60C 项目，同时保持对其他 `uint16` 毫米深度相机的复用能力。

## 主接口

```python
from depth_confidence import ConfidenceConfig, compute_confidence

confidence_u8, reason_bits_u8 = compute_confidence(
    depth_u16,
    ConfidenceConfig(),
    source_mask=optional_bool_mask,
)
```

接口：

```python
compute_confidence(
    depth,
    config=None,
    *,
    source_mask=None,
) -> tuple[np.ndarray, np.ndarray]
```

输出：

```text
confidence_u8   shape 与 depth 相同，dtype uint8，0..255
reason_bits_u8  shape 与 depth 相同，dtype uint8，可多 bit 同置
```

带审计信息的接口：

```python
from depth_confidence import compute_confidence_result

result = compute_confidence_result(depth_u16, config)

result.confidence_u8
result.reason_bits_u8
result.algorithm_id
result.algorithm_version
result.config_sha256
result.depth_sha256
result.source_mask_sha256
result.input_sha256
result.confidence_sha256
result.reason_bits_sha256
result.summary
```

批量流式接口：

```python
from depth_confidence.batch import DepthFrame, compute_confidence_batch

for frame, result in compute_confidence_batch(
    frames,
    config,
    audit_jsonl=Path("confidence_audit.jsonl"),
):
    ...
```

它一次只保留一帧结果，适合接入现有 `ChunkReader`。

## 算法版本

```text
algorithm_id       single_frame_depth_confidence
algorithm_version  1.0.0
```

V1 没有任何时间输入，不执行同像素跨帧比较。

## 计算结构

```text
C =
valid_mask
× neighborhood_support
× local_consistency
× discontinuity_penalty
× isolated_pixel_penalty
```

最终：

```text
confidence_u8 = round(255 × clamp(C, 0, 1))
```

### 1. 硬无效

强制置信度为 0：

```text
depth == 0
depth < 250 mm
depth > 2500 mm
source_mask == False
NaN / Inf 浮点输入
```

`source_mask=False` 表示重映射后无来源，或调用方已经判定源坐标越界。原始未重映射深度可省略该参数。

### 2. 5×5 邻域支持

```text
support = 5×5 内硬有效像素数 / 25
```

分母固定为 25。图像外邻域按无效处理，因此最外边界会自然降权。

默认：

```text
support < 0.80 → bit2 LOW_NEIGHBOR_SUPPORT
```

### 3. 局部一致性

在 5×5 硬有效邻域计算：

```text
median
MAD = median(abs(z_neighbor - median))
sigma_model(z) = a + b × z_m²
sigma_effective =
  max(
    sigma_model(z),
    1.4826 × MAD,
    sigma_floor
  )

residual = abs(z_pixel - median)

local_consistency =
  exp(-0.5 × (residual / sigma_effective)²)
```

默认噪声参数是临时值：

```text
a = 2.0 mm
b = 1.0 mm/m²
sigma_floor = 2.0 mm
```

这些值必须在 P2-15d 平面距离实验后替换，不能视为 HP60C 已标定噪声模型。

默认：

```text
residual / sigma_effective > 3.0
→ bit4 LOCAL_OUTLIER
```

### 4. 深度不连续惩罚

```text
gradient_ratio =
  max(abs(z_pixel - z_neighbor)) / z_pixel
```

参考实现补全了规格中未指定的软惩罚函数：

```text
discontinuity_penalty =
  exp(-0.5 × (gradient_ratio / 0.03)²)
```

默认：

```text
gradient_ratio > 0.05
→ bit3 DEPTH_DISCONTINUITY
```

这会显著降低前后景交界、混合像素和注册边缘的融合权重，但不会修改原始深度。

### 5. 孤立像素

默认满足任一条件即拒绝，`isolated_penalty=0`：

```text
3×3 内只有中心一个硬有效像素

存在有效邻居，但中心与每个有效邻居的差都超过：
max(20 mm, 4 × sigma_effective)

8 邻接连通区面积 < 4 像素
```

§15 没有单独的孤立像素 reason bit，因此映射为：

```text
结构性孤立、微小连通区 → bit2 LOW_NEIGHBOR_SUPPORT
数值上与所有邻居分离   → bit4 LOCAL_OUTLIER
```

这些 bit 可以与 `DEPTH_DISCONTINUITY` 同时出现。

## reason_bits

```text
bit0  RAW_INVALID
bit1  OUT_OF_RANGE
bit2  LOW_NEIGHBOR_SUPPORT
bit3  DEPTH_DISCONTINUITY
bit4  LOCAL_OUTLIER
bit5  RECTIFICATION_HOLE
bit6  POSE_AWARE_TEMPORAL_FAIL，V1 强制为 0
bit7  RESERVED，强制为 0
```

调用：

```python
from depth_confidence import ReasonBits

bad_edge = (reason_bits_u8 & ReasonBits.DEPTH_DISCONTINUITY) != 0
```

## 确定性和输入保护

实现保证：

- 输入数组不被写入；
- 同一 depth、同一逻辑 source mask、同一配置得到相同输出；
- 配置使用 canonical JSON 计算 SHA-256；
- depth 与归一化 source mask 均进入组合输入 hash；
- 没有随机数；
- 没有当前时间参与计算；
- 没有跨帧状态；
- bit6、bit7 在 V1 中被运行时断言为 0。

审计记录中的 UTC 只用于日志时间，不进入输出计算。

## 依赖

```text
Python >= 3.10
NumPy >= 1.24
SciPy >= 1.10
```

SciPy 仅用于确定性的卷积计数和 8 邻接连通域标记。

## 安装和测试

```bash
python -m pip install -e .
PYTHONPATH=src pytest -q
```

## CLI

单帧 `.npy`：

```bash
depth-confidence \
  depth.npy \
  --output confidence.npz \
  --config examples/config_provisional.json \
  --audit-jsonl confidence_audit.jsonl \
  --session-id SESSION_ID \
  --frame-id 123
```

NPZ：

```bash
depth-confidence \
  frame.npz \
  --key depth \
  --source-mask-key source_mask \
  --output confidence.npz
```

输出 NPZ 包含：

```text
confidence_u8
reason_bits_u8
```

## ChunkReader 对接

参考：

```text
examples/chunkreader_batch.py
```

只需替换其中 `iter_project_frames()`，把现有记录映射为：

```python
DepthFrame(
    session_id=record.session_id,
    frame_id=record.frame_id,
    depth=record.depth_u16,
    source_mask=None,
    source=f"chunk:{record.chunk_seq}",
)
```

## 性能定位

当前是高可靠参考实现，不是实时 GPU 内核。5×5 median/MAD 是主要成本，使用 `tile_rows` 控制临时内存。

本交付环境对一张 `480×640`、约 88% 非零、平面加噪深度图的三次结果：

```text
0.585 s
0.554 s
0.551 s
median 0.554 s/frame
```

该结果仅作为参考环境记录，不是目标机器性能承诺。批量验收建议先抽样，再决定是否实现 Numba、C++、OpenCV SIMD 或 GPU 等价内核。任何优化版本都应对本实现做逐像素 golden comparison。

## 生产冻结前仍需 P2-15d

以下参数尚未由真机距离实验确定：

```text
sigma_a_mm
sigma_b_mm_per_m2
sigma_floor_mm
discontinuity_scale_ratio
isolation_absolute_mm
TSDF 最低置信度阈值
```

因此当前交付应标记为：

```text
algorithm implementation: complete
default parameter calibration: provisional
```
