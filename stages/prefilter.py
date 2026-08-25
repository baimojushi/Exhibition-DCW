"""Depth 预滤波管线.

提供深度图预处理滤波器，用于 TSDF 融合前的质量提升：
- Discontinuity / flying-pixel filter（深度不连续/飞行像素滤波）
- Spatial bilateral filter（空间双边滤波）
- Invalid boundary erosion（无效边界腐蚀）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .common3d import cfg_get


@dataclass
class PrefilterConfig:
    """预滤波配置参数."""
    # Discontinuity filter
    enable_discontinuity: bool = True
    disc_max_depth_diff: float = 0.05  # 最大深度差 (m)
    disc_kernel_size: int = 5

    # Bilateral filter
    enable_bilateral: bool = True
    bilateral_d: int = 5  # 邻域直径
    bilateral_sigma_color: float = 0.03  # 颜色空间 sigma (m)
    bilateral_sigma_space: float = 2.0  # 坐标空间 sigma (像素)

    # Boundary erosion
    enable_erosion: bool = True
    erosion_kernel_size: int = 3
    erosion_iterations: int = 1

    # General
    min_depth: float = 0.2
    max_depth: float = 3.0

    @classmethod
    def from_cfg(cls, cfg: Any) -> "PrefilterConfig":
        return cls(
            enable_discontinuity=bool(cfg_get(cfg, "prefilter.enable_discontinuity", True)),
            disc_max_depth_diff=float(cfg_get(cfg, "prefilter.disc_max_depth_diff", 0.05)),
            disc_kernel_size=int(cfg_get(cfg, "prefilter.disc_kernel_size", 5)),
            enable_bilateral=bool(cfg_get(cfg, "prefilter.enable_bilateral", True)),
            bilateral_d=int(cfg_get(cfg, "prefilter.bilateral_d", 5)),
            bilateral_sigma_color=float(cfg_get(cfg, "prefilter.bilateral_sigma_color", 0.03)),
            bilateral_sigma_space=float(cfg_get(cfg, "prefilter.bilateral_sigma_space", 2.0)),
            enable_erosion=bool(cfg_get(cfg, "prefilter.enable_erosion", True)),
            erosion_kernel_size=int(cfg_get(cfg, "prefilter.erosion_kernel_size", 3)),
            erosion_iterations=int(cfg_get(cfg, "prefilter.erosion_iterations", 1)),
            min_depth=float(cfg_get(cfg, "prefilter.min_depth", 0.2)),
            max_depth=float(cfg_get(cfg, "prefilter.max_depth", 3.0)),
        )


def discontinuity_filter(
    depth: np.ndarray,
    max_diff: float = 0.05,
    kernel_size: int = 5,
) -> np.ndarray:
    """
    深度不连续/飞行像素滤波.

    使用中值滤波后的深度图作为参考，移除与局部邻域差异过大的像素.

    Args:
        depth: 输入深度图 (H, W), 单位米
        max_diff: 允许的最大深度差 (米)
        kernel_size: 中值滤波核大小

    Returns:
        滤波后的深度图，异常值设为 0
    """
    if depth.size == 0:
        return depth

    # 中值滤波作为局部参考
    k = max(1, kernel_size | 1)  # 确保奇数
    depth_median = cv2.medianBlur(depth.astype(np.float32), k)

    # 计算差异
    diff = np.abs(depth - depth_median)

    # 有效掩码
    valid = (depth > 0) & np.isfinite(depth)

    # 标记异常值
    outlier = valid & (diff > max_diff)

    # 结果：异常值设为 0
    result = depth.copy()
    result[outlier] = 0

    return result


def bilateral_filter(
    depth: np.ndarray,
    d: int = 5,
    sigma_color: float = 0.03,
    sigma_space: float = 2.0,
) -> np.ndarray:
    """
    空间双边滤波 - 保边平滑.

    Args:
        depth: 输入深度图 (H, W), 单位米
        d: 邻域直径
        sigma_color: 颜色空间标准差 (米)
        sigma_space: 坐标空间标准差 (像素)

    Returns:
        滤波后的深度图
    """
    if depth.size == 0:
        return depth

    # OpenCV bilateralFilter 需要 8-bit 或 float32
    # 将深度归一化到 0-1 范围用于 sigma_color 计算
    valid_mask = (depth > 0) & np.isfinite(depth)
    if not np.any(valid_mask):
        return depth

    depth_valid = depth[valid_mask]
    d_min, d_max = depth_valid.min(), depth_valid.max()
    depth_range = d_max - d_min if d_max > d_min else 1.0

    # 归一化
    depth_norm = np.zeros_like(depth, dtype=np.float32)
    depth_norm[valid_mask] = (depth[valid_mask] - d_min) / depth_range

    # 双边滤波
    filtered_norm = cv2.bilateralFilter(
        depth_norm, d, sigma_color / depth_range, sigma_space
    )

    # 反归一化
    result = depth.copy()
    result[valid_mask] = filtered_norm[valid_mask] * depth_range + d_min

    return result


def boundary_erosion(
    depth: np.ndarray,
    kernel_size: int = 3,
    iterations: int = 1,
) -> np.ndarray:
    """
    无效边界腐蚀 - 移除图像边缘的无效深度区域.

    Args:
        depth: 输入深度图 (H, W)
        kernel_size: 腐蚀核大小
        iterations: 腐蚀迭代次数

    Returns:
        腐蚀后的深度图
    """
    if depth.size == 0:
        return depth

    # 有效掩码
    valid = ((depth > 0) & np.isfinite(depth)).astype(np.uint8)

    # 腐蚀核
    k = max(1, kernel_size | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))

    # 腐蚀
    valid_eroded = cv2.erode(valid, kernel, iterations=iterations)

    # 应用掩码
    result = depth.copy()
    result[valid_eroded == 0] = 0

    return result


def prefilter_depth(
    depth: np.ndarray,
    cfg: Any,
    config: PrefilterConfig | None = None,
) -> np.ndarray:
    """
    完整的深度预滤波管线.

    处理顺序：
    1. 范围裁剪
    2. Discontinuity filter
    3. Bilateral filter
    4. Boundary erosion

    Args:
        depth: 输入深度图 (H, W), 单位米
        cfg: 配置对象
        config: 可选的 PrefilterConfig，优先于 cfg

    Returns:
        预滤波后的深度图
    """
    if config is None:
        config = PrefilterConfig.from_cfg(cfg)

    result = depth.astype(np.float32, copy=True)

    # 1. 范围裁剪
    result[(result < config.min_depth) | (result > config.max_depth) | ~np.isfinite(result)] = 0

    # 2. Discontinuity filter
    if config.enable_discontinuity:
        result = discontinuity_filter(
            result,
            max_diff=config.disc_max_depth_diff,
            kernel_size=config.disc_kernel_size,
        )

    # 3. Bilateral filter
    if config.enable_bilateral:
        result = bilateral_filter(
            result,
            d=config.bilateral_d,
            sigma_color=config.bilateral_sigma_color,
            sigma_space=config.bilateral_sigma_space,
        )

    # 4. Boundary erosion
    if config.enable_erosion:
        result = boundary_erosion(
            result,
            kernel_size=config.erosion_kernel_size,
            iterations=config.erosion_iterations,
        )

    return result


def prefilter_frame(
    frame: Any,
    cfg: Any,
    config: PrefilterConfig | None = None,
) -> np.ndarray:
    """
    对单帧应用预滤波.

    Args:
        frame: 帧数据对象
        cfg: 配置对象
        config: 可选的 PrefilterConfig

    Returns:
        预滤波后的深度图
    """
    from .common3d import depth_meters
    depth = depth_meters(frame, cfg, purpose="fusion")
    return prefilter_depth(depth, cfg, config)