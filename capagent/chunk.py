# -*- coding: utf-8 -*-
"""2 秒 chunk 容器: 自描述二进制格式, 服务器端可直接解析.

布局 (小端, 与网络字节序无关 — 跨端解析请按字段转换):
  Header (96 B):
    magic           4s   b"ASCC"
    version         u32  1
    flags           u32  0
    chunk_id        u64  全局递增
    session_id      u64  会话号 (agent 每次启动递增)
    frame_count     u32
    first_frame_id  u64
    last_frame_id   u64
    first_utc_ms    u64
    last_utc_ms     u64
    rgb_w / rgb_h   u32/u32
    depth_w / depth_h u32/u32
    jpeg_quality    u32
    depth_codec     u32  1=zstd
    reserved        16 B
  Frame record (重复 frame_count 次):
    frame_id        u64
    sdk_ts_ms       u64
    host_qpc        u64
    host_mono_ms    u64
    utc_ms          u64
    arrival_mono_ms u64
    rgb_size        u32
    depth_size      u32
    flags           u32  bit0=深度全零
    rgb_data        rgb_size B (JPEG)
    depth_data      depth_size B (zstd)

设计: 帧记录顺序写入即 chunk 内帧序; 时间戳四件套 + 到达时刻齐备 (规格 §4).
"""
import json
import os
import struct
import uuid

MAGIC = b"ASCC"
VERSION = 1
HEADER_FMT = "<4sIIQQIQQQQIIIIII16s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
FRAME_FMT = "<QQQQQQIII"
FRAME_SIZE = struct.calcsize(FRAME_FMT)
DEPTH_CODEC_ZSTD = 1
DEPTH_CODEC_RAW = 2

CHUNK_SUFFIX = ".asc"
MANIFEST = "manifest.json"


def _session_tag(session_id):
    """header 的 session_id 字段为 u64 (旧容器格式); 协议会话 id 是 uuid 字符串
    (P2 §5). 取 uuid 低 64 位作诊断回显 —— 规范身份以 manifest/session.json 为准."""
    if isinstance(session_id, str):
        try:
            return int(uuid.UUID(session_id)) & 0xFFFFFFFFFFFFFFFF
        except ValueError:
            return int.from_bytes(session_id.encode("utf-8")[:8].ljust(8, b"\x00"),
                                  "little")
    return session_id


def build_header(chunk_id, session_id, frame_count, first_frame_id, last_frame_id,
                 first_utc_ms, last_utc_ms, rgb_wh, depth_wh,
                 jpeg_quality, depth_codec=DEPTH_CODEC_ZSTD):
    return struct.pack(HEADER_FMT, MAGIC, VERSION, 0, chunk_id,
                       _session_tag(session_id), frame_count,
                       first_frame_id, last_frame_id, first_utc_ms, last_utc_ms,
                       rgb_wh[0], rgb_wh[1], depth_wh[0], depth_wh[1],
                       jpeg_quality, depth_codec, b"\x00" * 16)


class ChunkBuilder:
    """累积编码后帧 -> finish() 写出 chunk 文件 (P2-13b: 调用方写 .part 路径).

    切块条件 (规格 §4.4): 时长达到 target_duration_ms / payload 达到
    max_payload_bytes / 帧数达到 max_frame_count / force. 时间为主,
    不假设固定帧数.
    """

    def __init__(self, chunk_id, session_id, rgb_wh, depth_wh, jpeg_quality,
                 target_duration_ms=2000, max_payload_bytes=64 * 1024 * 1024,
                 max_frame_count=128, depth_codec=DEPTH_CODEC_ZSTD):
        self.chunk_id = chunk_id
        self.session_id = session_id
        self.rgb_wh = rgb_wh
        self.depth_wh = depth_wh
        self.jpeg_quality = jpeg_quality
        self.depth_codec = depth_codec
        self.target_duration_ms = target_duration_ms
        self.max_payload_bytes = max_payload_bytes
        self.max_frame_count = max_frame_count
        self._frames = []          # (record_bytes, rgb, depth)
        self.payload_bytes = 0     # 含帧记录头 + rgb + depth
        self.first_frame_id = None
        self.last_frame_id = None
        self.first_utc_ms = 0
        self.last_utc_ms = 0
        self.first_sdk_ts_ms = 0
        self.last_sdk_ts_ms = 0

    def add(self, frame_id, sdk_ts_ms, host_qpc, host_mono_ms, utc_ms,
            arrival_mono_ms, rgb_jpeg, depth_zstd, depth_zeros):
        rec = struct.pack(FRAME_FMT, frame_id, sdk_ts_ms, host_qpc, host_mono_ms,
                          utc_ms, arrival_mono_ms, len(rgb_jpeg), len(depth_zstd),
                          1 if depth_zeros else 0)
        if self.first_frame_id is None:
            self.first_frame_id = frame_id
            self.first_utc_ms = utc_ms
            self.first_sdk_ts_ms = sdk_ts_ms
        self.last_frame_id = frame_id
        self.last_utc_ms = utc_ms
        self.last_sdk_ts_ms = sdk_ts_ms
        self._frames.append((rec, rgb_jpeg, depth_zstd))
        self.payload_bytes += FRAME_SIZE + len(rgb_jpeg) + len(depth_zstd)

    @property
    def frame_count(self):
        return len(self._frames)

    def is_empty(self):
        return not self._frames

    def should_close(self, now_utc_ms=None, force=False):
        """§4.4 切块判定: 时间/容量/帧数已达上限或 force."""
        if self.is_empty():
            return False
        if force:
            return True
        if (self.last_utc_ms - self.first_utc_ms) >= self.target_duration_ms:
            return True
        if self.max_payload_bytes and self.payload_bytes >= self.max_payload_bytes:
            return True
        if self.max_frame_count and self.frame_count >= self.max_frame_count:
            return True
        return False

    def finish(self, path):
        """写出 chunk 文件到 path (调用方负责 .part/.ready 生命周期), 返回路径."""
        if self.is_empty():
            raise ValueError("空 chunk")
        header = build_header(self.chunk_id, self.session_id, len(self._frames),
                              self.first_frame_id, self.last_frame_id,
                              self.first_utc_ms, self.last_utc_ms,
                              self.rgb_wh, self.depth_wh, self.jpeg_quality,
                              self.depth_codec)
        with open(path, "wb") as f:
            f.write(header)
            for rec, rgb, depth in self._frames:
                f.write(rec)
                f.write(rgb)
                f.write(depth)
        return path


class ChunkReader:
    """读取 chunk 文件, 逐帧 yield 原始编码数据."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            raw = f.read(HEADER_SIZE)
        if len(raw) != HEADER_SIZE:
            raise ValueError("chunk 头不完整: %s" % path)
        (magic, self.version, self.flags, self.chunk_id, self.session_id,
         self.frame_count, self.first_frame_id, self.last_frame_id,
         self.first_utc_ms, self.last_utc_ms,
         self.rgb_w, self.rgb_h, self.depth_w, self.depth_h,
         self.jpeg_quality, self.depth_codec, _) = struct.unpack(HEADER_FMT, raw)
        if magic != MAGIC:
            raise ValueError("非法 chunk magic: %r (%s)" % (magic, path))
        if self.version != VERSION:
            raise ValueError("不支持的 chunk 版本: %d" % self.version)
        self.file_size = os.path.getsize(path)

    def iter_frames(self):
        with open(self.path, "rb") as f:
            f.seek(HEADER_SIZE)
            for _ in range(self.frame_count):
                rec = f.read(FRAME_SIZE)
                if len(rec) != FRAME_SIZE:
                    raise ValueError("帧记录截断: %s" % self.path)
                (frame_id, sdk_ts_ms, host_qpc, host_mono_ms, utc_ms,
                 arrival_mono_ms, rgb_size, depth_size, flags) = struct.unpack(FRAME_FMT, rec)
                rgb = f.read(rgb_size)
                depth = f.read(depth_size)
                if len(rgb) != rgb_size or len(depth) != depth_size:
                    raise ValueError("帧数据截断: %s" % self.path)
                yield {"frame_id": frame_id, "sdk_ts_ms": sdk_ts_ms, "host_qpc": host_qpc,
                       "host_mono_ms": host_mono_ms, "utc_ms": utc_ms,
                       "arrival_mono_ms": arrival_mono_ms,
                       "rgb": rgb, "depth": depth, "depth_zeros": bool(flags & 1),
                       "depth_codec": self.depth_codec}

    def summary(self):
        return {"chunk_id": self.chunk_id, "session_id": self.session_id,
                "frames": self.frame_count, "first_frame_id": self.first_frame_id,
                "last_frame_id": self.last_frame_id,
                "first_utc_ms": self.first_utc_ms, "last_utc_ms": self.last_utc_ms,
                "size": self.file_size}


def chunk_filename(chunk_id):
    return "chunk_%012d%s" % (chunk_id, CHUNK_SUFFIX)
