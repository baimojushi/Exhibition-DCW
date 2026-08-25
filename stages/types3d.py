from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import numpy as np


@dataclass
class PairRegistration:
    source_node: int
    target_node: int
    source_frame_index: int
    target_frame_index: int
    transformation: np.ndarray
    information: np.ndarray
    success: bool = True
    method: str = ""
    fitness: float = float("nan")
    rmse_m: float = float("nan")
    uncertain: bool = False
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_node": int(self.source_node),
            "target_node": int(self.target_node),
            "source_frame_index": int(self.source_frame_index),
            "target_frame_index": int(self.target_frame_index),
            "transformation": np.asarray(self.transformation).tolist(),
            "information": np.asarray(self.information).tolist(),
            "success": bool(self.success),
            "method": self.method,
            "fitness": None if not np.isfinite(self.fitness) else float(self.fitness),
            "rmse_m": None if not np.isfinite(self.rmse_m) else float(self.rmse_m),
            "uncertain": bool(self.uncertain),
            "note": self.note,
            "metadata": self.metadata,
        }


@dataclass
class RegistrationBundle:
    sequential_edges: list[PairRegistration] = field(default_factory=list)
    loop_edges: list[PairRegistration] = field(default_factory=list)
    rejected_edges: list[PairRegistration] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequential_edges": [x.to_dict() for x in self.sequential_edges],
            "loop_edges": [x.to_dict() for x in self.loop_edges],
            "rejected_edges": [x.to_dict() for x in self.rejected_edges],
        }


@dataclass
class TrajectoryResult:
    poses: list[np.ndarray]
    optimized: bool
    pose_graph: Any = None
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "poses": [np.asarray(x).tolist() for x in self.poses],
            "optimized": bool(self.optimized),
            "stats": self.stats,
        }


@dataclass
class TSDFResult:
    mesh_path: Path
    pointcloud_path: Path | None
    vertices: int
    triangles: int
    integrated_frames: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mesh_path": str(self.mesh_path),
            "pointcloud_path": str(self.pointcloud_path) if self.pointcloud_path else None,
            "vertices": int(self.vertices),
            "triangles": int(self.triangles),
            "integrated_frames": int(self.integrated_frames),
        }
