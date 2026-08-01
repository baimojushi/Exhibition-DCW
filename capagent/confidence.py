# -*- coding: utf-8 -*-
"""P2-15 深度置信度生产接线 (规格 §14/§15, 参考实现 depth_confidence).

参考实现 (p2_15_ref, 已拷贝为项目根包 depth_confidence/) 是独立确定性算法;
本模块只做 capagent 侧适配: ChunkReader 解真机 chunk -> 原始 uint16 mm Depth
-> DepthFrame -> compute_confidence_batch. 原始 Depth 永不被修改.

运行: python verify_confidence_e2e.py [spool_root]
"""
import glob
import os

import numpy as np

from depth_confidence.batch import DepthFrame


def iter_spool_depth_frames(spool_root, max_chunks=None, yield_depth=True):
    """遍历 spool_root/<session>/chunks/*.chunk.ready, 逐帧解出 depth_u16.

    depth 容器: zstd 压缩的 uint16 LE, shape (depth_h, depth_w) mm.
    yield DepthFrame(session_id, frame_id, depth, source=chunk:<seq>).
    yield_depth=False 时 depth 为 None (仅计数扫描, 不解压, 用于流式抽样).
    解压失败/尺寸不符抛 ValueError (数据完整性由调用方决定是否容忍).
    """
    import zstandard as zstd
    from capagent.chunk import ChunkReader

    zd = zstd.ZstdDecompressor() if yield_depth else None
    pattern = os.path.join(os.path.abspath(spool_root), "*", "chunks",
                           "*.chunk.ready")
    paths = sorted(glob.glob(pattern))
    if max_chunks:
        paths = paths[:max_chunks]
    if not paths:
        raise FileNotFoundError("无 .chunk.ready 文件: %s" % pattern)
    for chunk_path in paths:
        reader = ChunkReader(chunk_path)
        expected = reader.depth_w * reader.depth_h * 2
        shape = (reader.depth_h, reader.depth_w)
        session_id = os.path.basename(
            os.path.dirname(os.path.dirname(chunk_path)))
        seq = int(os.path.basename(chunk_path).split(".")[0])
        for fr in reader.iter_frames():
            if not yield_depth:
                yield DepthFrame(session_id=session_id,
                                 frame_id=fr["frame_id"], depth=None,
                                 source="chunk:%d" % seq)
                continue
            raw = zd.decompress(fr["depth"],
                                max_output_size=expected + 1) if fr["depth"] else b""
            if len(raw) != expected:
                raise ValueError("chunk %d 帧 %d depth 解压尺寸异常 (%d != %d)"
                                 % (seq, fr["frame_id"], len(raw), expected))
            depth = np.frombuffer(raw, dtype="<u2").reshape(shape)
            yield DepthFrame(
                session_id=session_id,
                frame_id=fr["frame_id"],
                depth=depth,
                source="chunk:%d" % seq,
            )
