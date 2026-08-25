from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
import json
import math
import numpy as np


def cfg_get(cfg: Any, path: str, default=None):
    cur = cfg
    for token in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            if token not in cur:
                return default
            cur = cur[token]
        else:
            if not hasattr(cur, token):
                return default
            cur = getattr(cur, token)
    return default if cur is None else cur


def write_json(path: str | Path, obj: Any):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def conv(x):
        if isinstance(x, np.ndarray):
            return x.tolist()
        if isinstance(x, (np.integer,)):
            return int(x)
        if isinstance(x, (np.floating,)):
            return None if not np.isfinite(x) else float(x)
        if isinstance(x, Path):
            return str(x)
        if hasattr(x, "to_dict"):
            return x.to_dict()
        raise TypeError(type(x).__name__)

    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=conv),
        encoding="utf-8",
    )


def get_frame_attr(frame: Any, names: Iterable[str], default=None):
    for name in names:
        if isinstance(frame, dict) and name in frame:
            return frame[name]
        if hasattr(frame, name):
            return getattr(frame, name)
    return default


def get_depth(frame: Any) -> np.ndarray:
    d = get_frame_attr(frame, ["depth", "depth_image", "depth_raw"])
    if d is None:
        raise AttributeError("FrameData must expose depth/depth_image/depth_raw")
    return np.asarray(d)


def get_rgb(frame: Any, prefer_aligned: bool = True) -> np.ndarray | None:
    names = ["aligned_rgb", "rgb_aligned", "color_aligned"] if prefer_aligned else []
    names += ["rgb", "color", "rgb_image", "color_image"]
    x = get_frame_attr(frame, names)
    return None if x is None else np.asarray(x)


def get_frame_id(frame: Any, fallback: int):
    return get_frame_attr(frame, ["frame_id", "id", "index"], fallback)


def _depth_max_for_purpose(cfg: Any, purpose: str) -> float:
    if purpose == "tracking":
        return float(cfg_get(
            cfg, "depth.tracking_max_m",
            cfg_get(cfg, "depth.max_m", 3.0)
        ))
    if purpose == "fusion":
        return float(cfg_get(
            cfg, "depth.fusion_max_m",
            cfg_get(cfg, "depth.max_m", 1.2)
        ))
    return float(cfg_get(cfg, "depth.max_m", 3.0))


def depth_meters(frame: Any, cfg: Any, purpose: str = "tracking") -> np.ndarray:
    d = get_depth(frame).astype(np.float32, copy=False)
    scale = float(
        get_frame_attr(frame, ["depth_scale"], cfg_get(cfg, "camera.depth_scale", 1000.0))
    )
    if scale != 1.0:
        d = d / scale

    min_m = float(cfg_get(cfg, "depth.min_m", 0.20))
    max_m = _depth_max_for_purpose(cfg, purpose)

    valid = np.isfinite(d) & (d >= min_m) & (d <= max_m)
    out = np.zeros_like(d, dtype=np.float32)
    out[valid] = d[valid]
    return out


def _intrinsics_candidate(frame: Any, cfg: Any):
    x = get_frame_attr(
        frame,
        ["depth_intrinsics", "intrinsics_depth", "intrinsics"],
        None,
    )
    if x is not None:
        return x
    for p in ["camera.depth_intrinsics", "depth_intrinsics", "intrinsics.depth"]:
        x = cfg_get(cfg, p, None)
        if x is not None:
            return x
    return None


def intrinsic_params(frame: Any, cfg: Any, width=None, height=None):
    x = _intrinsics_candidate(frame, cfg)
    if x is None:
        raise ValueError("Depth intrinsics are missing.")

    # Open3D intrinsic
    if hasattr(x, "intrinsic_matrix"):
        K = np.asarray(x.intrinsic_matrix, dtype=float)
        return {
            "width": int(getattr(x, "width", width or 0)),
            "height": int(getattr(x, "height", height or 0)),
            "fx": float(K[0,0]), "fy": float(K[1,1]),
            "cx": float(K[0,2]), "cy": float(K[1,2]),
        }

    # numpy/list 3x3 K
    if isinstance(x, (list, tuple, np.ndarray)):
        K = np.asarray(x, dtype=float)
        if K.shape == (3, 3):
            return {
                "width": int(width),
                "height": int(height),
                "fx": float(K[0,0]), "fy": float(K[1,1]),
                "cx": float(K[0,2]), "cy": float(K[1,2]),
            }

    if isinstance(x, dict):
        if "K" in x:
            K = np.asarray(x["K"], dtype=float)
            return {
                "width": int(x.get("width", width)),
                "height": int(x.get("height", height)),
                "fx": float(K[0,0]), "fy": float(K[1,1]),
                "cx": float(K[0,2]), "cy": float(K[1,2]),
            }
        return {
            "width": int(x.get("width", width)),
            "height": int(x.get("height", height)),
            "fx": float(x["fx"]), "fy": float(x["fy"]),
            "cx": float(x["cx"]), "cy": float(x["cy"]),
        }

    return {
        "width": int(getattr(x, "width", width)),
        "height": int(getattr(x, "height", height)),
        "fx": float(getattr(x, "fx")),
        "fy": float(getattr(x, "fy")),
        "cx": float(getattr(x, "cx")),
        "cy": float(getattr(x, "cy")),
    }


def make_o3d_intrinsic(frame: Any, cfg: Any, depth_shape=None):
    import open3d as o3d
    if depth_shape is None:
        depth_shape = get_depth(frame).shape[:2]
    h, w = int(depth_shape[0]), int(depth_shape[1])
    p = intrinsic_params(frame, cfg, width=w, height=h)

    cw, ch = int(p["width"]), int(p["height"])
    sx = w / cw if cw else 1.0
    sy = h / ch if ch else 1.0

    return o3d.camera.PinholeCameraIntrinsic(
        w, h,
        p["fx"] * sx, p["fy"] * sy,
        p["cx"] * sx, p["cy"] * sy,
    )


def make_depth_pointcloud(
    frame: Any,
    cfg: Any,
    voxel_m: float = 0.0,
    estimate_normals: bool = True,
    purpose: str = "tracking",
):
    import open3d as o3d

    depth = depth_meters(frame, cfg, purpose=purpose)
    intrinsic = make_o3d_intrinsic(frame, cfg, depth.shape)
    depth_img = o3d.geometry.Image(depth.astype(np.float32))

    pcd = o3d.geometry.PointCloud.create_from_depth_image(
        depth_img,
        intrinsic,
        depth_scale=1.0,
        depth_trunc=_depth_max_for_purpose(cfg, purpose),
        stride=1,
    )

    if voxel_m and voxel_m > 0:
        pcd = pcd.voxel_down_sample(float(voxel_m))

    if estimate_normals and len(pcd.points) >= 10:
        radius = max(float(voxel_m) * 3.0, 0.02)
        pcd.estimate_normals(
            o3d.geometry.KDTreeSearchParamHybrid(radius=radius, max_nn=30)
        )
        try:
            pcd.orient_normals_towards_camera_location(np.zeros(3))
        except Exception:
            pass

    return pcd


def rgbd_is_aligned(frame: Any, cfg: Any) -> bool:
    rgb = get_rgb(frame, prefer_aligned=True)
    if rgb is None:
        return False
    d = get_depth(frame)
    if rgb.shape[:2] != d.shape[:2]:
        return False
    flag = get_frame_attr(frame, ["rgb_depth_aligned", "d2c_aligned"], None)
    return flag is not False


def make_rgbd(frame: Any, cfg: Any):
    import open3d as o3d
    if not rgbd_is_aligned(frame, cfg):
        raise ValueError("RGB and depth are not D2C-aligned/same resolution.")

    rgb = get_rgb(frame, prefer_aligned=True)
    depth = depth_meters(frame, cfg, purpose="tracking")

    if rgb.ndim == 2:
        rgb = np.repeat(rgb[..., None], 3, axis=2)
    rgb = rgb[..., :3]
    if rgb.dtype != np.uint8:
        if np.issubdtype(rgb.dtype, np.floating) and rgb.max() <= 1.5:
            rgb = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        else:
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    return o3d.geometry.RGBDImage.create_from_color_and_depth(
        o3d.geometry.Image(np.ascontiguousarray(rgb)),
        o3d.geometry.Image(np.ascontiguousarray(depth.astype(np.float32))),
        depth_scale=1.0,
        depth_trunc=_depth_max_for_purpose(cfg, "tracking"),
        convert_rgb_to_intensity=False,
    )


def sequential_poses_from_edges(edges, n_nodes: int):
    """
    从 sequential edges 计算初始 poses。
    如果有 rejected edges 导致链断裂，抛出 RuntimeError。
    调用方应使用 PGO 的 segment-based 优化来处理断链。
    """
    poses = [np.eye(4, dtype=float)]
    edge_map = {(e.source_node, e.target_node): e for e in edges if e.success}
    
    for i in range(n_nodes - 1):
        e = edge_map.get((i, i + 1))
        if e is None:
            raise RuntimeError(f"Pose chain broken between nodes {i} and {i+1}")
        poses.append(poses[-1] @ np.linalg.inv(np.asarray(e.transformation)))
    return poses


def predicted_relative_transform(source_pose, target_pose):
    return np.linalg.inv(np.asarray(target_pose)) @ np.asarray(source_pose)


def rotation_angle_deg(T: np.ndarray) -> float:
    R = np.asarray(T)[:3, :3]
    x = max(-1.0, min(1.0, (np.trace(R) - 1.0) / 2.0))
    return math.degrees(math.acos(x))
