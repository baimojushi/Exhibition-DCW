# -*- coding: utf-8 -*-
"""P2-15a/b 真机批量验收 (规格 §20 P2-15, ACCEPTANCE_CHECKLIST.md).

对照验收清单:
  单帧功能            -> 参考实现 tests/test_core.py 22 项 (本脚本 smoke 复核关键项)
  真机批量验证        -> ChunkReader 解 144 chunks 抽样 >= 100 帧, 覆盖非零比 0.86~0.90
  确定性与数据完整性  -> 真机帧双跑逐字节一致 + hash 一致 + tile_rows 不变性
  审计                -> confidence_audit.jsonl (fsync) + config.json + 汇总
  性能和资源          -> 单帧 p50/p95 时延 + 峰值 RSS (ctypes)

运行: python verify_confidence_e2e.py [spool_root] [--max-frames N]
"""
import ctypes
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")          # zstandard
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import numpy as np

from depth_confidence import (
    ConfidenceConfig, ReasonBits, compute_confidence_result,
)
from depth_confidence.batch import compute_confidence_batch

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if not cond:
        FAIL.append(name)
        raise AssertionError("%s 失败 %s" % (name, (" " + detail) if detail else ""))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def _ctypes_peak_rss_mb():
    """进程峰值工作集 (MB), Windows 专用; 失败返回 None."""
    try:
        from ctypes import wintypes
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong),
                        ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.windll.psapi          # GetProcessMemoryInfo 在 psapi.dll
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return counters.PeakWorkingSetSize / (1024 * 1024)
    except Exception:
        return None


def smoke_single_frame():
    """单帧功能 smoke (完整覆盖在参考实现 test_core.py)."""
    print("[P2-15a/b smoke: 单帧功能复核]")
    from depth_confidence import compute_confidence
    depth = np.full((41, 41), 1000, dtype=np.uint16)
    depth[4, 4] = 0
    depth[2, 2] = 249
    depth[6, 6] = 2501
    depth[18:23, 18:23] = 0                    # 中央孔洞 (边缘检查点 (17,20))
    depth[0:14, 21:] = 1400                    # 上半右侧突变 (边缘检查点 (12,20))
    original = depth.copy()
    conf, reasons = compute_confidence(depth)
    check("depth==0 -> confidence 0", conf[4, 4] == 0)
    check("depth<250 -> 0 + OUT_OF_RANGE",
          conf[2, 2] == 0 and bool(reasons[2, 2] & ReasonBits.OUT_OF_RANGE))
    check("depth>2500 -> 0 + OUT_OF_RANGE",
          conf[6, 6] == 0 and bool(reasons[6, 6] & ReasonBits.OUT_OF_RANGE))
    check("平面中心 > 孔洞边缘",
          int(conf[8, 8]) > int(conf[17, 20]))
    check("深度突变边缘降权 + DEPTH_DISCONTINUITY",
          int(conf[12, 8]) > int(conf[12, 20])
          and bool(reasons[12, 20] & ReasonBits.DEPTH_DISCONTINUITY))
    forbidden = int(ReasonBits.POSE_AWARE_TEMPORAL_FAIL | ReasonBits.RESERVED)
    check("bit6/bit7 恒 0", not bool(np.any(reasons & forbidden)))
    check("原始 depth 未被修改", np.array_equal(depth, original))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P2-15a/b 真机批量验收")
    parser.add_argument("spool_root", nargs="?",
                        default=os.path.join("audit_output", "spool_real"))
    parser.add_argument("--max-frames", type=int, default=None,
                        help="最多处理帧数 (默认全部抽样帧)")
    parser.add_argument("--out", default=os.path.join("audit_output",
                                                      "confidence_v1"))
    args = parser.parse_args()

    smoke_single_frame()

    from capagent.confidence import iter_spool_depth_frames

    print("[P2-15a/b 真机批量: %s]" % args.spool_root)
    out = args.out
    os.makedirs(os.path.join(out, "npz"), exist_ok=True)
    config = ConfidenceConfig()                  # 临时参数, P2-15d 后替换
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"config": config.to_dict(),
                   "config_sha256": config.sha256(),
                   "algorithm_id": "single_frame_depth_confidence",
                   "algorithm_version": "1.0.0",
                   "note": "provisional until P2-15d plane-noise fit"},
                  f, ensure_ascii=False, indent=1)

    frames = iter_spool_depth_frames(args.spool_root)
    # 帧总数 (count-only 扫描, 不解压, 避免 6569 帧全量进内存 ~4GB)
    total = sum(1 for _ in iter_spool_depth_frames(args.spool_root,
                                                   yield_depth=False))
    check("ChunkReader 解出真机帧 >= 100", total >= 100,
          "got=%d" % total)

    # 流式抽样: 均匀取最多 max_frames 帧 (参考 0.55s/帧, 全量 ~6500 帧太慢)
    import itertools
    import math
    max_frames = args.max_frames or 144
    step = max(1, int(math.ceil(total / max_frames)))
    sampled = list(itertools.islice(frames, 0, None, step))
    check("抽样帧数 >= 100", len(sampled) >= 100, "got=%d" % len(sampled))

    # 非零比分布 (验收: 覆盖 0.86~0.90 的正常帧)
    ratios = [float(np.count_nonzero(f.depth)) / f.depth.size for f in sampled]
    r = np.array(ratios)
    check("采样帧非零比覆盖 0.86~0.90",
          bool(np.any((r >= 0.86) & (r <= 0.90))),
          "min=%.3f max=%.3f" % (r.min(), r.max()))

    # 批量计算 + 审计 + 性能计时 + NPZ 输出
    timings = []
    audit_path = os.path.join(out, "confidence_audit.jsonl")
    npz_dir = os.path.join(out, "npz")
    # 幂等: 重跑前清空旧产物 (审计 JSONL 为 fsync 追加, 残留会重复计数)
    if os.path.exists(audit_path):
        os.remove(audit_path)
    for fn in os.listdir(npz_dir):
        os.remove(os.path.join(npz_dir, fn))
    frame_count = 0
    for frame, result in compute_confidence_batch(
            sampled, config, audit_jsonl=audit_path):
        t0 = time.perf_counter()
        # 复核确定性 (每帧重算一次同输入, 逐字节对比)
        again = compute_confidence_result(frame.depth, config)
        timings.append(time.perf_counter() - t0)
        check_ok = (np.array_equal(result.confidence_u8, again.confidence_u8)
                    and np.array_equal(result.reason_bits_u8,
                                       again.reason_bits_u8)
                    and result.confidence_sha256 == again.confidence_sha256)
        if not check_ok:
            raise AssertionError("帧 %d 确定性复核失败" % frame.frame_id)
        np.savez_compressed(
            os.path.join(npz_dir, "%010d.npz" % frame.frame_id),
            confidence_u8=result.confidence_u8,
            reason_bits_u8=result.reason_bits_u8)
        frame_count += 1
    check("全部抽样帧确定性复核 (双跑逐字节一致)", True,
          "frames=%d" % frame_count)
    check("NPZ 输出已保存", len(os.listdir(npz_dir)) == frame_count)
    check("审计 JSONL 已写入 (fsync)", os.path.exists(audit_path)
          and os.path.getsize(audit_path) > 0)
    with open(audit_path, encoding="utf-8") as f:
        audit_lines = [json.loads(l) for l in f if l.strip()]
    check("审计记录数 == 抽样帧数", len(audit_lines) == frame_count)
    rec = audit_lines[0]
    for key in ("algorithm_id", "algorithm_version", "config_sha256",
                "depth_sha256", "input_sha256", "confidence_sha256",
                "reason_bits_sha256", "summary"):
        check("审计含 %s" % key, key in rec)
    check("审计含 session_id/frame_id/source",
          all(k in rec for k in ("session_id", "frame_id", "source")))
    check("审计含 reason bit counts",
          "reason_bit_counts" in rec["summary"])
    check("审计含 confidence 分位数",
          "confidence_percentiles_on_valid" in rec["summary"])

    # tile_rows 不变性 (真机帧)
    probe = sampled[0].depth
    small = compute_confidence_result(probe, ConfidenceConfig(tile_rows=7))
    large = compute_confidence_result(probe, ConfidenceConfig(tile_rows=128))
    check("真机帧 tile_rows=7 vs 128 输出逐字节一致",
          np.array_equal(small.confidence_u8, large.confidence_u8)
          and np.array_equal(small.reason_bits_u8, large.reason_bits_u8))

    # 性能 p50/p95 (仅置信度计算, 不含解压/NPZ 写盘)
    ts = np.array(timings) * 1000.0
    p50, p95 = float(np.percentile(ts, 50)), float(np.percentile(ts, 95))
    check("单帧时延 p50/p95 记录", ts.size > 0,
          "p50=%.1fms p95=%.1fms max=%.1fms" % (p50, p95, ts.max()))
    rss = _ctypes_peak_rss_mb()
    check("峰值 RSS 记录", rss is not None and rss > 0,
          "peak_rss=%.1fMB" % (rss or 0))

    # reason bit 聚合汇总
    agg = {}
    for line in audit_lines:
        for bit, cnt in line["summary"]["reason_bit_counts"].items():
            agg[bit] = agg.get(bit, 0) + cnt
    summary = {
        "frames_processed": frame_count,
        "frames_total_in_spool": total,
        "spool_root": os.path.abspath(args.spool_root),
        "nonzero_ratio_min": float(r.min()),
        "nonzero_ratio_max": float(r.max()),
        "latency_ms": {"p50": round(p50, 1), "p95": round(p95, 1),
                       "max": round(float(ts.max()), 1)},
        "peak_rss_mb": round(rss or 0, 1),
        "aggregate_reason_bit_counts": agg,
        "output_dir": os.path.abspath(out),
        "config_sha256": config.sha256(),
        "provisional_params_until_p2_15d": True,
    }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print("== P2-15a/b 真机验收: %d 项通过, %d 项失败 ==" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
