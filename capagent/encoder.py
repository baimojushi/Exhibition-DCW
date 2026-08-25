# -*- coding: utf-8 -*-
"""帧编码: RGB -> JPEG (逐帧), Depth -> Zstd 无损压缩.

规格 P0: RGB 编码为逐帧 JPEG; Depth 使用 Zstd 无损压缩.
依赖: opencv-python-headless (D:\\pyopencv), zstandard (D:\\pyopencv).
"""
import sys

import numpy as np

sys.path.insert(0, r"D:\pyopencv")
import cv2
import zstandard as zstd

DEFAULT_JPEG_QUALITY = 85
DEFAULT_ZSTD_LEVEL = 3
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"          # zstd frame magic, 用于校验/识别


class FrameEncoder:
    def __init__(self, jpeg_quality=DEFAULT_JPEG_QUALITY, zstd_level=DEFAULT_ZSTD_LEVEL):
        self.jpeg_quality = jpeg_quality
        self.zstd_level = zstd_level
        self._jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
        self._zc = zstd.ZstdCompressor(level=zstd_level)
        self._zd = zstd.ZstdDecompressor()

    # -- RGB (BGR8 bytes -> JPEG bytes) --
    def encode_rgb(self, rgb_bytes, h=480, w=640):
        if not rgb_bytes:
            raise ValueError("RGB 数据为空")
        arr = np.frombuffer(rgb_bytes, np.uint8).reshape(h, w, 3)
        if arr.size == 0:
            raise ValueError("RGB 数组为空")
        ok, buf = cv2.imencode(".jpg", arr, self._jpeg_params)
        if not ok:
            raise ValueError("JPEG 编码失败")
        return buf.tobytes()

    def decode_rgb(self, jpeg_bytes):
        arr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise ValueError("JPEG 解码失败")
        return arr

    # -- Depth (uint16 LE bytes -> zstd bytes / raw passthrough) --
    def encode_depth(self, depth_bytes, raw=False):
        if raw:
            return depth_bytes
        return self._zc.compress(depth_bytes)

    def decode_depth(self, z_bytes, out_len=None):
        return self._zd.decompress(z_bytes, out_len) if out_len else self._zd.decompress(z_bytes)

    def compress_ratio(self, raw_size, enc_size):
        return raw_size / enc_size if enc_size else 0.0
