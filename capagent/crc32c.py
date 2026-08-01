# -*- coding: utf-8 -*-
"""CRC-32C (Castagnoli) 实现: 反射查表 (纯 Python, 无外部依赖).

规格 §4.3: chunk 本地提交需计算 payload CRC32C; 服务器 (P2-13e) 边收边算同一
校验, 必须一致 —— 不能使用 zlib.crc32 (IEEE 多项式不同). 已实测:
- ntdll.RtlComputeCrc32 是 IEEE (cbf43926), 非 CRC32C, 无系统级 CRC32C 可借
- hashlib/OpenSSL 3.x 构建未暴露 crc32c
- 标准测试向量 crc32c(b"123456789") == 0xE3069283 锁定正确性

性能: 本机 ~5.6 MB/s (纯 Python 逐字节). 实际 chunk ~6.5MB 需 ~1.2s, 由 spool
提交线程在读取 payload 时边读边算, 与采集/编码线程解耦, 不阻塞相机 (架构 §2).
若上传阶段需要更高吞吐, 可替换为 C 扩展 (slicing-by-8 表驱动) —— 接口不变.
"""
POLY_REFLECTED = 0x82F63B78   # CRC-32C 反射多项式 (0x1EDC6F41 bit-reversed)
TEST_VECTOR = b"123456789"
TEST_CRC = 0xE3069283


def _build_table():
    table = [0] * 256
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ POLY_REFLECTED if c & 1 else c >> 1
        table[i] = c
    return table


_TABLE = _build_table()


def crc32c(data, crc=0):
    """计算 CRC-32C (Castagnoli), 返回 uint32.

    data: bytes/bytearray/memoryview. crc: 前段结果 (分段串行组合用).
    """
    c = (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
    for b in data:
        c = _TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return (c ^ 0xFFFFFFFF) & 0xFFFFFFFF


if __name__ == "__main__":
    import time
    t = crc32c(TEST_VECTOR)
    print("crc32c(b'123456789') = %08x 期望 %08x  %s" % (t, TEST_CRC, "OK" if t == TEST_CRC else "FAIL"))
    data = bytes(range(256)) * 1000
    print("分段 == 整段:", crc32c(data[:500000], crc32c(data[500000:])) == crc32c(data))
    big = b"\x5a" * (64 * 1024 * 1024)
    t0 = time.perf_counter()
    crc32c(big)
    dt = time.perf_counter() - t0
    print("64MiB: %.2fs (%.0f MB/s)" % (dt, 64 * 1024 * 1024 / dt / 1e6))
    raise SystemExit(0 if t == TEST_CRC else 1)
