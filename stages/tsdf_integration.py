"""TSDF 多段产物结构.

处理 TSDF 融合的多段产物，解决 segment_00 vs segment_01 问题：
- 多段产物结构化输出
- 每段独立 identity 起始
- Component Cleanup (小组件/退化面过滤)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import numpy as np
import open3d as o3d

from .common3d import cfg_get, write_json


@dataclass
class TSDFSegment:
    """单个 TSDF 产物段."""
    id: int
    mesh_path: Path
    pointcloud_path: Optional[Path]
    vertices: int
    triangles: int
    integrated_frames: int
    pose_origin: np.ndarray  # 该段的起始位姿 (4x4)
    volume_origin: np.ndarray  # TSDF 体素网格原点
    voxel_size: float
    sdf_trunc: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "mesh_path": str(self.mesh_path),
            "pointcloud_path": str(self.pointcloud_path) if self.pointcloud_path else None,
            "vertices": self.vertices,
            "triangles": self.triangles,
            "integrated_frames": self.integrated_frames,
            "pose_origin": self.pose_origin.tolist(),
            "volume_origin": self.volume_origin.tolist(),
            "voxel_size": self.voxel_size,
            "sdf_trunc": self.sdf_trunc,
        }


@dataclass
class TSDFResult:
    """完整的 TSDF 融合结果."""
    segments: List[TSDFSegment]
    mesh_path: Optional[Path]  # 向后兼容：第一段的 mesh
    pointcloud_path: Optional[Path]  # 向后兼容：第一段的 pointcloud
    vertices: int  # 向后兼容：总顶点数
    triangles: int  # 向后兼容：总三角形数
    integrated_frames: int  # 向后兼容：总帧数

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "mesh_path": str(self.mesh_path) if self.mesh_path else None,
            "pointcloud_path": str(self.pointcloud_path) if self.pointcloud_path else None,
            "vertices": self.vertices,
            "triangles": self.triangles,
            "integrated_frames": self.integrated_frames,
        }


def save_tsdf_result(
    result: TSDFResult,
    output_dir: Path,
    prefix: str = "tsdf",
) -> Dict[str, Path]:
    """
    保存 TSDF 融合结果到磁盘.

    Args:
        result: TSDFResult 对象
        output_dir: 输出目录
        prefix: 文件名前缀

    Returns:
        保存的文件路径字典
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存每段的 mesh 和 pointcloud
    saved_files = {}

    for i, segment in enumerate(result.segments):
        seg_prefix = f"{prefix}_segment_{segment.id:02d}"

        # 保存 mesh
        if segment.mesh_path and segment.mesh_path.exists():
            mesh_dst = output_dir / f"{seg_prefix}.ply"
            # 这里假设 mesh_path 已经是 .ply 文件，直接复制或移动
            # 实际使用中可能需要从 TSDF 重新提取
            saved_files[f"segment_{segment.id}_mesh"] = mesh_dst

        # 保存 pointcloud
        if segment.pointcloud_path and segment.pointcloud_path.exists():
            pcd_dst = output_dir / f"{seg_prefix}_pcd.ply"
            saved_files[f"segment_{segment.id}_pcd"] = pcd_dst

    # 保存元数据
    meta_path = output_dir / f"{prefix}_result.json"
    write_json(meta_path, result.to_dict())
    saved_files["metadata"] = meta_path

    return saved_files


def load_tsdf_result(
    input_dir: Path,
    prefix: str = "tsdf",
) -> TSDFResult:
    """
    从磁盘加载 TSDF 融合结果.

    Args:
        input_dir: 输入目录
        prefix: 文件名前缀

    Returns:
        TSDFResult 对象
    """
    meta_path = input_dir / f"{prefix}_result.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"TSDF result not found: {meta_path}")

    with open(meta_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    segments = []
    for seg_data in data.get("segments", []):
        segment = TSDFSegment(
            id=seg_data["id"],
            mesh_path=Path(seg_data["mesh_path"]) if seg_data["mesh_path"] else None,
            pointcloud_path=Path(seg_data["pointcloud_path"]) if seg_data["pointcloud_path"] else None,
            vertices=seg_data["vertices"],
            triangles=seg_data["triangles"],
            integrated_frames=seg_data["integrated_frames"],
            pose_origin=np.array(seg_data["pose_origin"], dtype=np.float64),
            volume_origin=np.array(seg_data["volume_origin"], dtype=np.float64),
            voxel_size=seg_data["voxel_size"],
            sdf_trunc=seg_data["sdf_trunc"],
        )
        segments.append(segment)

    # 向后兼容字段
    mesh_path = Path(data["mesh_path"]) if data.get("mesh_path") else None
    pointcloud_path = Path(data["pointcloud_path"]) if data.get("pointcloud_path") else None
    vertices = data.get("vertices", 0)
    triangles = data.get("triangles", 0)
    integrated_frames = data.get("integrated_frames", 0)

    return TSDFResult(
        segments=segments,
        mesh_path=mesh_path,
        pointcloud_path=pointcloud_path,
        vertices=vertices,
        triangles=triangles,
        integrated_frames=integrated_frames,
    )


def cleanup_small_components(
    mesh: o3d.geometry.TriangleMesh,
    min_faces: int = 100,
    min_bbox_diagonal: float = 0.03,
) -> o3d.geometry.TriangleMesh:
    """
    删除小组件 Component Cleanup.

    删除同时满足：
    - faces < min_faces
    - bbox diagonal < min_bbox_diagonal (米)

    Args:
        mesh: 输入三角网格
        min_faces: 最小面数阈值
        min_bbox_diagonal: 最小包围盒对角线阈值 (米)

    Returns:
        清理后的网格
    """
    if mesh is None or not hasattr(mesh, 'triangles') or len(mesh.triangles) == 0:
        return o3d.geometry.TriangleMesh()

    try:
        # 连通组件分析
        triangle_clusters, cluster_n_triangles, cluster_area = (
            mesh.cluster_connected_triangles()
        )
        triangle_clusters = np.asarray(triangle_clusters)
        cluster_n_triangles = np.asarray(cluster_n_triangles)

        # 保留足够大的组件
        triangles_to_remove = []
        for i, cluster_id in enumerate(np.unique(triangle_clusters)):
            if cluster_id < 0:  # 无效簇
                continue
            n_tri = cluster_n_triangles[cluster_id]
            if n_tri < min_faces:
                # 检查该簇的 bbox 对角线
                cluster_mask = triangle_clusters == cluster_id
                if np.any(cluster_mask):
                    cluster_mesh = mesh.select_by_index(
                        np.where(cluster_mask)[0]
                    )
                    bbox = cluster_mesh.get_axis_aligned_bounding_box()
                    extent = bbox.get_extent()
                    diagonal = np.linalg.norm(extent)
                    if diagonal < min_bbox_diagonal:
                        triangles_to_remove.extend(
                            np.where(cluster_mask)[0].tolist()
                        )

        if triangles_to_remove:
            mesh = mesh.remove_triangles_by_index(triangles_to_remove)
            if mesh is not None and hasattr(mesh, 'triangles') and len(mesh.triangles) > 0:
                mesh.remove_unreferenced_vertices()
                mesh.remove_duplicate_triangles()
                mesh.remove_duplicate_vertices()
                mesh.remove_non_manifold_edges()
            else:
                print(f"[DEBUG] cleanup_small_components: mesh became empty after removing {len(triangles_to_remove)} triangles")
                return o3d.geometry.TriangleMesh()
    except Exception as e:
        print(f"[DEBUG] cleanup_small_components error: {e}")
        return o3d.geometry.TriangleMesh()

    return mesh if mesh is not None else o3d.geometry.TriangleMesh()


def cleanup_degenerate_faces(
    mesh: o3d.geometry.TriangleMesh,
    min_area: float = 1e-6,
    max_aspect_ratio: float = 100.0,
) -> o3d.geometry.TriangleMesh:
    """
    删除退化/皮ny面 Component Cleanup.

    删除同时满足：
    - 面积 < min_area (m²)
    - 长宽比 > max_aspect_ratio (极值)

    Args:
        mesh: 输入三角网格
        min_area: 最小面积阈值 (平方米)
        max_aspect_ratio: 最大长宽比阈值

    Returns:
        清理后的网格
    """
    if mesh is None or not hasattr(mesh, 'triangles') or len(mesh.triangles) == 0 or len(mesh.vertices) < 3:
        return o3d.geometry.TriangleMesh()

    try:
        triangles = np.asarray(mesh.triangles)
        vertices = np.asarray(mesh.vertices)

        # 计算每个三角形的面积
        v0 = vertices[triangles[:, 0]]
        v1 = vertices[triangles[:, 1]]
        v2 = vertices[triangles[:, 2]]

        # 使用叉积计算面积: 0.5 * ||(v1-v0) x (v2-v0)||
        edge1 = v1 - v0
        edge2 = v2 - v0
        cross = np.cross(edge1, edge2)
        areas = 0.5 * np.linalg.norm(cross, axis=1)

        # 计算每条边的长度
        len_edge1 = np.linalg.norm(edge1, axis=1)
        len_edge2 = np.linalg.norm(edge2, axis=1)
        len_base = np.linalg.norm(v2 - v1, axis=1)

        # 避免除零 - 只计算有效三角形的长宽比
        sides = np.column_stack([len_edge1, len_edge2, len_base])
        sides_sorted = np.sort(sides, axis=1)
        # 长宽比 = 最长边 / 最短边，只对最短边 > 0 的三角形计算
        valid_tri = sides_sorted[:, 0] > 1e-10
        aspect_ratios = np.full(len(triangles), 0.0)
        aspect_ratios[valid_tri] = sides_sorted[valid_tri, 2] / sides_sorted[valid_tri, 0]
        aspect_ratios[~valid_tri] = 0.0  # 退化三角形设为 0，不触发长宽比删除

        # 标记需要删除的三角形：面积太小 OR 长宽比太大
        to_remove = (areas < min_area) | (aspect_ratios > max_aspect_ratio)

        if np.any(to_remove):
            n_remove = np.sum(to_remove)
            print(f"[DEBUG] cleanup_degenerate_faces: removing {n_remove} triangles (area<{min_area} or aspect>{max_aspect_ratio})")
            mesh = mesh.remove_triangles_by_index(np.where(to_remove)[0])
            if mesh is not None and hasattr(mesh, 'triangles') and len(mesh.triangles) > 0:
                mesh.remove_unreferenced_vertices()
                mesh.remove_duplicate_triangles()
                mesh.remove_duplicate_vertices()
                mesh.remove_non_manifold_edges()
            else:
                print(f"[DEBUG] cleanup_degenerate_faces: mesh became empty after removing {n_remove} triangles")
                return o3d.geometry.TriangleMesh()
    except Exception as e:
        print(f"[DEBUG] cleanup_degenerate_faces error: {e}")
        return o3d.geometry.TriangleMesh()

    return mesh if mesh is not None else o3d.geometry.TriangleMesh()


def integrate_tsdf_multisegment(
    frames: List[Any],
    poses: List[np.ndarray],
    cfg: Any,
    voxel_size: float = 0.02,
    sdf_trunc: float = 0.1,
    depth_scale: float = 1000.0,
    depth_min: float = 0.1,
    depth_max: float = 3.0,
) -> TSDFResult:
    """
    多段 TSDF 融合 - 为每个连续轨迹段创建独立的 TSDF.

    Args:
        frames: 帧数据列表
        poses: 位姿列表 (4x4 矩阵)
        cfg: 配置对象
        voxel_size: TSDF 体素大小 (米)
        sdf_trunc: TSDF 截断距离 (米)
        depth_scale: 深度比例因子
        depth_min: 最小有效深度 (米)
        depth_max: 最大有效深度 (米)

    Returns:
        TSDFResult 包含多个段
    """
    from .common3d import depth_meters, make_rgbd, intrinsic_params, make_o3d_intrinsic
    from .prefilter import prefilter_depth

    # 检测轨迹段断裂（基于位姿间隔大于阈值）
    segment_breaks = [0]  # 第一段从 0 开始
    max_pose_gap = float(cfg_get(cfg, "tsdf.segment_break_threshold", 0.5))  # 米
    print(f"[DEBUG] max_pose_gap: {max_pose_gap}")

    for i in range(1, len(poses)):
        if i < len(poses):
            trans_diff = np.linalg.norm(
                poses[i][:3, 3] - poses[i-1][:3, 3]
            )
            if trans_diff > max_pose_gap:
                segment_breaks.append(i)
                print(f"[DEBUG] Break detected at index {i}, trans_diff: {trans_diff}")

    # 添加结束点
    segment_breaks.append(len(poses))
    print(f"[DEBUG] segment_breaks: {segment_breaks}")

    segments = []
    all_vertices = 0
    all_triangles = 0
    all_frames = 0

    for seg_idx in range(len(segment_breaks) - 1):
        start_idx = segment_breaks[seg_idx]
        end_idx = segment_breaks[seg_idx + 1]

        if end_idx <= start_idx:
            continue

        # 提取该段的帧和位姿
        seg_frames = frames[start_idx:end_idx]
        seg_poses = poses[start_idx:end_idx]

        if len(seg_frames) == 0:
            continue

        print(f"Processing segment {seg_idx}: {len(seg_frames)} frames")

        # 创建该段的 TSDF 体积
        volume = o3d.pipelines.integration.ScalableTSDFVolume(
            voxel_length=voxel_size,
            sdf_trunc=sdf_trunc,
            color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8
        )

        # 该段的起始位姿（用于 identity）
        pose_origin = np.array(seg_poses[0], dtype=np.float64)
        volume_origin = np.zeros(3, dtype=np.float64)  # 相对原点

        # 融合该段的所有帧
        for frame_idx, (frame, pose) in enumerate(zip(seg_frames, seg_poses)):
            try:
                # 获取深度和 RGB
                depth = depth_meters(frame, cfg, purpose="fusion")
                print(f"[DEBUG] Frame {start_idx + frame_idx}: depth range [{depth.min():.3f}, {depth.max():.3f}], valid: {np.sum((depth > 0) & np.isfinite(depth))}")
                # 应用预滤波
                depth = prefilter_depth(depth, cfg)
                print(f"[DEBUG] Frame {start_idx + frame_idx}: after prefilter, depth range [{depth.min():.3f}, {depth.max():.3f}], valid: {np.sum((depth > 0) & np.isfinite(depth))}")
                # 确保深度在有效范围内
                depth[(depth < depth_min) | (depth > depth_max) | ~np.isfinite(depth)] = 0
                print(f"[DEBUG] Frame {start_idx + frame_idx}: after clamp, depth range [{depth.min():.3f}, {depth.max():.3f}], valid: {np.sum((depth > 0) & np.isfinite(depth))}")

                # 获取内参
                intrin = intrinsic_params(frame, cfg)
                intrinsic = o3d.camera.PinholeCameraIntrinsic(
                    intrin["width"], intrin["height"],
                    intrin["fx"], intrin["fy"],
                    intrin["cx"], intrin["cy"]
                )

                # 创建 RGBD 图像
                if getattr(frame, 'rgb', None) is not None:
                    rgb = np.asarray(frame.rgb)
                    if rgb.ndim == 2:
                        rgb = np.stack([rgb]*3, axis=-1)
                    rgb = rgb[..., :3]
                    if rgb.dtype != np.uint8:
                        if np.issubdtype(rgb.dtype, np.floating) and rgb.max() <= 1.5:
                            rgb = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
                        else:
                            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
                else:
                    # 无 RGB，使用深度图的灰度
                    rgb = np.stack([depth]*3, axis=-1)
                    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

                rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                    o3d.geometry.Image(rgb),
                    o3d.geometry.Image(depth.astype(np.float32)),
                    depth_scale=depth_scale,
                    depth_trunc=depth_max,
                    convert_rgb_to_intensity=False
                )

                # 融合到 TSDF
                volume.integrate(
                    rgbd,
                    intrinsic,
                    pose  # pose 是相机在世界坐标系下的位姿 (camera-to-world)
                )
                print(f"[DEBUG] Frame {start_idx + frame_idx}: integrated successfully")

            except Exception as e:
                # 记录错误但继续处理
                print(f"Warning: Failed to integrate frame {start_idx + frame_idx}: {e}")
                continue

        # 提取该段的网格和点云
        try:
            mesh = volume.extract_triangle_mesh()
            pointcloud = volume.extract_point_cloud()
            print(f"[DEBUG] Segment {seg_idx}: extracted mesh with {len(mesh.triangles)} triangles, {len(mesh.vertices)} vertices")
            print(f"[DEBUG] Segment {seg_idx}: pointcloud has {len(pointcloud.points)} points")

            # 应用 Component Cleanup
            mesh = cleanup_small_components(
                mesh,
                min_faces=int(cfg_get(cfg, "tsdf.cleanup.min_faces", 100)),
                min_bbox_diagonal=float(cfg_get(cfg, "tsdf.cleanup.min_bbox_diagonal", 0.03))
            )
            if mesh is None:
                print(f"[DEBUG] Segment {seg_idx}: cleanup_small_components returned None")
                mesh = o3d.geometry.TriangleMesh()
            
            mesh = cleanup_degenerate_faces(
                mesh,
                min_area=float(cfg_get(cfg, "tsdf.cleanup.min_area", 1e-6)),
                max_aspect_ratio=float(cfg_get(cfg, "tsdf.cleanup.max_aspect_ratio", 100.0))
            )
            if mesh is None:
                print(f"[DEBUG] Segment {seg_idx}: cleanup_degenerate_faces returned None")
                mesh = o3d.geometry.TriangleMesh()
            
            print(f"[DEBUG] Segment {seg_idx}: after cleanup, {len(mesh.triangles)} triangles")

            # 只保留足够大的网格
            if len(mesh.triangles) > 0:
                segment = TSDFSegment(
                    id=seg_idx,
                    mesh_path=None,  # 将由调用者保存
                    pointcloud_path=None,  # 将由调用者保存
                    vertices=len(mesh.vertices),
                    triangles=len(mesh.triangles),
                    integrated_frames=len(seg_frames),
                    pose_origin=pose_origin,
                    volume_origin=volume_origin,
                    voxel_size=voxel_size,
                    sdf_trunc=sdf_trunc,
                )
                segments.append(segment)
                all_vertices += len(mesh.vertices)
                all_triangles += len(mesh.triangles)
                all_frames += len(seg_frames)
                print(f"[DEBUG] Segment {seg_idx}: added to segments list, total segments now: {len(segments)}")
            else:
                print(f"[DEBUG] Segment {seg_idx}: mesh has 0 triangles, skipped")

        except Exception as e:
            print(f"Warning: Failed to extract mesh for segment {seg_idx}: {e}")
            continue

    # 向后兼容：第一段的数据
    mesh_path = segments[0].mesh_path if segments else None
    pointcloud_path = segments[0].pointcloud_path if segments else None
    vertices = segments[0].vertices if segments else 0
    triangles = segments[0].triangles if segments else 0
    integrated_frames = segments[0].integrated_frames if segments else 0

    return TSDFResult(
        segments=segments,
        mesh_path=mesh_path,
        pointcloud_path=pointcloud_path,
        vertices=all_vertices,
        triangles=all_triangles,
        integrated_frames=all_frames,
    )