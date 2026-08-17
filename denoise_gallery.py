# -*- coding: utf-8 -*-
"""Gallery 多视角深度降噪 — camera-frame 深度中值滤波.

策略:
  - 不依赖位姿, 直接利用: 相邻帧在 camera frame 有大量冗余观测
  - 对每帧每个像素, 取时间窗口内多帧同像素深度的中值/均值
  - 评估: 降噪后帧的 best-fit plane RMS
  - 对照: 降噪前 vs 降噪后
"""
import os, sys, io, json, gc
import numpy as np
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT   = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit"
SPOOL  = os.path.join(ROOT, "audit_output", "spool", "35ac160e-8447-4a2b-ab4d-d1887c57ecae")
OUT    = os.path.join(SPOOL, "alg_wall_02")
os.makedirs(OUT, exist_ok=True)

FX = FY = 361.376; CX, CY = 318.517, 179.502
ZMIN, ZMAX = 200, 4000
STRIDE_DEPTH = 1     # 相邻帧 (不 stride, 保留时间连续性)
WINDOW = 5          # 中值窗口大小


def load_gallery_depths():
    """流式加载 gallery 帧, 返回 [(utc, depth_HW), ...]"""
    from capagent.chunk import ChunkReader
    import glob, zstandard as zstd

    zd = zstd.ZstdDecompressor()
    files = sorted(glob.glob(os.path.join(SPOOL, "chunks", "*.chunk.ready")))

    print("Loading gallery depths (stride=%d, window=%d) ..." % (STRIDE_DEPTH, WINDOW))
    frames = []
    for fi, fp in enumerate(files):
        try:
            cr = ChunkReader(fp)
        except Exception:
            continue
        dw, dh = cr.depth_w, cr.depth_h
        for fd in cr.iter_frames():
            if fd["utc_ms"] is None:
                continue
            zr = 0.0
            try:
                dep = np.frombuffer(zd.decompress(fd["depth"]), dtype=np.uint16).reshape((dh, dw))
                zr = float((dep == 0).mean())
            except Exception:
                continue
            if zr >= 0.40:
                continue
            frames.append({"utc": fd["utc_ms"], "depth": dep, "zr": zr})
        if fi % 50 == 0:
            print("  chunk %d: %d frames loaded" % (fi, len(frames)))
        if len(frames) >= 1500:
            break
    print("  Total: %d frames" % len(frames))
    return frames


def apply_temporal_median(frames, window=5):
    """对每帧做时间窗口中值滤波.

    相机慢速行走: 窗口内像素通常对应同一物理点.
    策略: 对每帧每个有效像素, 取窗口内深度的中值.
    """
    print("Temporal median filter (window=%d) ..." % window)
    n = len(frames)
    half = window // 2
    filtered = []

    for i in range(n):
        utc = frames[i]["utc"]
        dep = frames[i]["depth"].astype(np.float32)
        # 收集窗口内帧的深度
        deps = [dep]
        for j in range(max(0, i - half), min(n, i + half + 1)):
            if j != i:
                deps.append(frames[j]["depth"].astype(np.float32))
        # 中值 (逐像素)
        dep_stack = np.stack(deps, axis=0)  # (window, H, W)
        dep_med = np.median(dep_stack, axis=0)
        dep_med_uint = np.clip(dep_med, 0, 65535).astype(np.uint16)
        filtered.append({"utc": utc, "depth": dep_med, "depth_raw": frames[i]["depth"]})
    return filtered


def fit_plane_rms(depth):
    """深度图 best-fit plane RMS (camera frame)."""
    mask = (depth > ZMIN) & (depth < ZMAX)
    ys, xs = np.nonzero(mask)
    if len(ys) < 200:
        return None
    z = depth[ys, xs].astype(np.float64)
    x = (xs - CX) / FX * z
    y = (ys - CY) / FY * z
    pts = np.stack([x, y, z], axis=1)
    c = pts.mean(axis=0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    n = vt[-1]
    if n[2] > 0:
        n = -n
    d = float(n @ c)
    res = pts @ n - d
    sigma = np.median(np.abs(res)) / 0.6745
    inl = np.abs(res) < 3.0 * sigma
    if inl.sum() < 50:
        return None
    rms_sq = res[inl] ** 2
    rms_val = np.sqrt(np.mean(rms_sq))
    rms = float(rms_val)
    return rms


def main():
    print("=" * 60)
    print("Gallery Multi-View Depth Denoising")
    print("=" * 60)

    frames = load_gallery_depths()
    if not frames:
        print("No frames"); return

    # 降噪前 baseline
    print("\nBaseline (raw) ...")
    rms_before = []
    for i in range(0, len(frames), 10):
        r = fit_plane_rms(frames[i]["depth"])
        if r:
            rms_before.append(r)
    r_before = np.array(rms_before)
    print("  p50=%.1f  p75=%.1f  p95=%.1f mm  (n=%d)" % (
        np.median(r_before), np.percentile(r_before, 75), np.percentile(r_before, 95), len(r_before)))

    # 降噪后
    print("\nTemporal median filter ...")
    filtered = apply_temporal_median(frames, window=WINDOW)

    print("\nAfter denoising ...")
    rms_after = []
    for i in range(0, len(filtered), 10):
        r = fit_plane_rms(filtered[i]["depth"])
        if r:
            rms_after.append(r)
    r_after = np.array(rms_after)
    print("  p50=%.1f  p75=%.1f  p95=%.1f mm  (n=%d)" % (
        np.median(r_after), np.percentile(r_after, 75), np.percentile(r_after, 95), len(r_after)))

    reduction = (np.median(r_before) - np.median(r_after)) / max(np.median(r_before), 0.1) * 100
    print("\n" + "=" * 60)
    print("RESULT")
    print("=" * 60)
    print("Raw RMS:    %.1f mm (p50)" % np.median(r_before))
    print("Denoised:   %.1f mm (p50)" % np.median(r_after))
    print("Reduction:  %.0f%%" % reduction)

    if np.median(r_after) < 6.0:
        verdict = "PASS: %.1fmm < 6.0mm ALG-WALL-02 threshold" % np.median(r_after)
    elif np.median(r_after) < 10.0:
        verdict = "PASS: %.1fmm < 10.0mm gallery target" % np.median(r_after)
    elif np.median(r_after) < 20.0:
        verdict = "WARN: %.1fmm borderline (10-20mm)" % np.median(r_after)
    else:
        verdict = "FAIL: %.1fmm exceeds 20mm limit" % np.median(r_after)
    print(verdict)
    print("=" * 60)

    result = {
        "window": WINDOW,
        "frames": len(frames),
        "raw_rms_p50": float(np.median(r_before)),
        "raw_rms_p75": float(np.percentile(r_before, 75)),
        "raw_rms_p95": float(np.percentile(r_before, 95)),
        "denoised_rms_p50": float(np.median(r_after)),
        "denoised_rms_p75": float(np.percentile(r_after, 75)),
        "denoised_rms_p95": float(np.percentile(r_after, 95)),
        "reduction_pct": float(reduction),
        "verdict": verdict,
    }
    out_json = os.path.join(OUT, "denoise_result.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print("\nSaved: %s" % out_json)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
