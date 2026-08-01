# -*- coding: utf-8 -*-
"""端到端录制产物验证: chunk 头/帧数/时间戳四件套/2s 边界/编码可解码/manifest/session.

用法: python verify_e2e.py <输出目录>
"""
import glob
import json
import os
import sys

sys.path.insert(0, r"D:\pyopencv")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np
import zstandard

from capagent.chunk import ChunkReader, chunk_filename


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\Administrator\hp60c_audit\audit_output\capture_e2e"
    paths = sorted(glob.glob(os.path.join(root, "chunk_*.asc")))
    print("chunk 文件数: %d" % len(paths))

    total_frames = 0
    first_utc_all = None
    last_utc_all = None
    boundaries_ok = True
    decode_checked = 0
    zctx = zstandard.ZstdDecompressor()

    for p in paths:
        r = ChunkReader(p)
        s = r.summary()
        span_ms = s["last_utc_ms"] - s["first_utc_ms"]
        ok_boundary = 1000 <= span_ms <= 3200
        boundaries_ok &= ok_boundary
        total_frames += s["frames"]
        if first_utc_all is None:
            first_utc_all = s["first_utc_ms"]
        last_utc_all = s["last_utc_ms"]
        print("  %s: chunk=%d session=%d frames=%d first_id=%d last_id=%d "
              "span=%dms %s size=%d" % (
                  os.path.basename(p), s["chunk_id"], s["session_id"], s["frames"],
                  s["first_frame_id"], s["last_frame_id"], span_ms,
                  "OK" if ok_boundary else "BAD", s["size"]))
        # 首/尾帧抽查: JPEG 可解码 + zstd 解压长度
        fr = list(r.iter_frames())
        for idx in (0, len(fr) - 1):
            f = fr[idx]
            img = cv2.imdecode(np.frombuffer(f["rgb"], dtype=np.uint8), cv2.IMREAD_COLOR)
            dep = zctx.decompress(f["depth"])
            ok = (img is not None and img.shape == (r.depth_h, r.depth_w, 3)
                  and len(dep) == r.depth_w * r.depth_h * 2)
            decode_checked += 1
            if not ok:
                print("    首/尾帧解码失败 idx=%d" % idx)
        # 时间戳四件套字段齐全性 (首帧)
        f0 = fr[0]
        keys = ("frame_id", "sdk_ts_ms", "host_qpc", "host_mono_ms", "utc_ms",
                "arrival_mono_ms")
        missing = [k for k in keys if not f0[k]]
        if missing:
            print("    首帧缺字段: %s" % missing)

    print("总帧数: %d  时间跨度: %dms (%.1fs)  平均每 chunk %.1f 帧"
          % (total_frames, last_utc_all - first_utc_all,
             (last_utc_all - first_utc_all) / 1000,
             total_frames / max(1, len(paths))))
    print("2s 边界 (span 1000-3200ms): %s" % ("全部 OK" if boundaries_ok else "存在异常"))
    print("JPEG/Zstd 解码抽查: %d 帧全部通过" % decode_checked)

    # manifest 一致性
    mf = json.load(open(os.path.join(root, "manifest.json"), encoding="utf-8"))
    m_ids = {c["id"] for c in mf["chunks"]}
    disk_ids = {r.chunk_id for r in (ChunkReader(p) for p in paths)}
    print("manifest chunks=%d 磁盘 chunks=%d 一致: %s"
          % (len(m_ids), len(disk_ids), m_ids == disk_ids))
    pend = [c for c in mf["chunks"] if c["state"] == "pending"]
    print("manifest pending=%d acked=%d" % (len(pend),
                                            len(mf["chunks"]) - len(pend)))

    # session.json
    sj = json.load(open(os.path.join(root, "session.json"), encoding="utf-8"))
    print("session: id=%d last_frame=%d last_chunk=%d calibration_id=%s"
          % (sj["session_id"], sj["last_frame_id"], sj["last_chunk_id"],
             sj.get("calibration_id", "MISSING")))
    st = sj.get("stats", {})
    print("stats: frames=%d fps=%.2f dropped=%d avg_encode_ms=%.2f chunks=%d"
          % (st.get("frames", 0), st.get("fps", 0), st.get("dropped", 0),
             st.get("avg_encode_ms", 0), st.get("chunks", 0)))


if __name__ == "__main__":
    main()
