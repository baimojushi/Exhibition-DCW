# -*- coding: utf-8 -*-
"""HP60C chunk 容器验证器 (P2-13e 服务端注入钩子).

IngestServer(chunk_validator=...) 在服务端提交前校验收到的 .part 文件内部
格式 (规格 §9.1 step 6: 验证 chunk 内部格式). 用现有 capagent.chunk.ChunkReader
完整解析并核对 manifest 声明:

- magic "ASCC" / 容器版本 / codec 标签
- 帧记录边界: 逐帧迭代无截断、无越界
- frame_count / first/last frame_id 与 CHUNK_OFFER metadata 一致
- RGB 段为 JPEG (SOI 0xFFD8), Depth 段为 zstd (magic 28 B5 2F FD)
- Depth 解压后尺寸与图像规格一致 (640x480x2) —— 不依赖解码器重编码

metadata 即 CHUNK_OFFER 的协议字段 (payload_bytes/frame_count/
first_frame_id/last_frame_id/rgb 宽高等, 可缺省; 缺省字段跳过比对).

规格 §9.1: 服务端"验证 chunk 内部格式"是提交顺序 step 6, 校验失败
(PAYLOAD_REJECTED) 的 chunk 不得进入 .part fsync 后的提交链.
"""
import os
import sys

sys.path.insert(0, r"D:\pyopencv")          # 与 encoder.py 相同的 zstandard 位置
import zstandard as zstd

from capagent.chunk import ChunkReader, DEPTH_CODEC_ZSTD

JPEG_SOI = b"\xff\xd8"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# depth 帧原始尺寸: 640x480x2 字节 (uint16 LE); 由 header rgb/depth 宽高推导
_DEPTH_DEFAULT_WH = (640, 480)


def validate_chunk(path, metadata):
    """校验服务端收到的 chunk .part 文件. 失败抛 ValueError (PAYLOAD_REJECTED)."""
    try:
        reader = ChunkReader(str(path))
    except Exception as e:
        raise ValueError("chunk 容器解析失败: %r" % (e,)) from e

    # 帧数与 manifest 一致
    if metadata.get("frame_count") is not None \
            and reader.frame_count != metadata["frame_count"]:
        raise ValueError("frame_count 不一致: 容器=%d manifest=%d"
                         % (reader.frame_count, metadata["frame_count"]))
    if metadata.get("first_frame_id") is not None \
            and reader.first_frame_id != metadata["first_frame_id"]:
        raise ValueError("first_frame_id 不一致: 容器=%d manifest=%d"
                         % (reader.first_frame_id, metadata["first_frame_id"]))
    if metadata.get("last_frame_id") is not None \
            and reader.last_frame_id != metadata["last_frame_id"]:
        raise ValueError("last_frame_id 不一致: 容器=%d manifest=%d"
                         % (reader.last_frame_id, metadata["last_frame_id"]))
    if metadata.get("payload_bytes") is not None \
            and reader.file_size != metadata["payload_bytes"]:
        raise ValueError("payload_bytes 不一致: 文件=%d manifest=%d"
                         % (reader.file_size, metadata["payload_bytes"]))

    # codec 标签 (规格 chunk 头: depth_codec 1=zstd)
    if reader.depth_codec != DEPTH_CODEC_ZSTD:
        raise ValueError("不支持的 depth_codec=%d (期望 zstd=1)" % reader.depth_codec)

    # 逐帧解析: 无截断 + 载荷识别
    depth_raw = reader.depth_w * reader.depth_h * 2
    try:
        decomp = zstd.ZstdDecompressor()
        for frame in reader.iter_frames():
            rgb = frame["rgb"]
            depth = frame["depth"]
            if not rgb.startswith(JPEG_SOI):
                raise ValueError("帧 %d RGB 非 JPEG (SOI 缺失)" % frame["frame_id"])
            if not depth.startswith(ZSTD_MAGIC):
                raise ValueError("帧 %d Depth 非 zstd (magic 缺失)" % frame["frame_id"])
            try:
                raw = decomp.decompress(depth, max_output_size=depth_raw + 1)
            except zstd.ZstdError as e:
                raise ValueError("帧 %d Depth zstd 解压失败: %s"
                                 % (frame["frame_id"], e)) from e
            if len(raw) != depth_raw:
                raise ValueError("帧 %d Depth 尺寸异常: %d != %d"
                                 % (frame["frame_id"], len(raw), depth_raw))
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("帧解析失败: %r" % (e,)) from e
