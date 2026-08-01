# -*- coding: utf-8 -*-
import json
import numpy as np

rows = [json.loads(l) for l in open(r"C:\Users\Administrator\hp60c_audit\audit_output\longrun\frames.jsonl",
                                    encoding="utf-8") if l.strip()]
# row = (i, frameId, rgb_ts, depth_ts, host_ns, host_epoch_ms, zero, miss_rgb, miss_d, interval_ns)
rgb = np.array([r[2] for r in rows], dtype=np.int64)
dep = np.array([r[3] for r in rows], dtype=np.int64)

drgb = np.diff(rgb)
ddep = np.diff(dep)

print("rgb ts diff: nonzero=%d/%d mean=%.2f p50=%.1f p95=%.1f max=%d"
      % (int(np.count_nonzero(drgb)), len(drgb), drgb.mean(), np.percentile(drgb, 50),
         np.percentile(drgb, 95), int(drgb.max())))
print("depth ts diff: nonzero=%d/%d mean=%.2f p50=%.1f p95=%.1f max=%d"
      % (int(np.count_nonzero(ddep)), len(ddep), ddep.mean(), np.percentile(ddep, 50),
         np.percentile(ddep, 95), int(ddep.max())))

same = int(np.sum(rgb == dep))
print("frames with rgb.ts == depth.ts:", same, "/", len(rows))

# 同帧 delta 分布
delta = rgb - dep
print("rgb-depth delta: min=%d max=%d nonzero=%d" % (delta.min(), delta.max(), int(np.count_nonzero(delta))))
if np.count_nonzero(delta):
    # 看 delta 非零的模式
    idx = np.nonzero(delta)[0][:10]
    for i in idx:
        print("  frame", i + 1, "rgb", rgb[i], "depth", dep[i], "delta", delta[i])

# depth ts 重复模式: 连续相同值的段长
runs = []
cur = dep[0]
ln = 1
for v in dep[1:]:
    if v == cur:
        ln += 1
    else:
        runs.append(ln)
        cur = v
        ln = 1
runs.append(ln)
print("depth ts run-length: distinct values=%d, max run=%d, runs histogram:" % (len(runs), max(runs)))
import collections
print(sorted(collections.Counter(runs).items())[:10])

# 有效 depth ts 间隔 (run 首帧之间)
dep_first = dep[[0] + [i for i in range(1, len(dep)) if dep[i] != dep[i - 1]]]
gd = np.diff(dep_first)
print("effective depth ts interval: mean=%.2f p50=%.1f p95=%.1f" % (gd.mean(), np.percentile(gd, 50), np.percentile(gd, 95)))

# 前 12 帧展示模式
print("\nfirst 12 frames (i, rgb_ts, depth_ts, host_interval_ms):")
for r in rows[:12]:
    print("  %6d %15d %15d %9.1f" % (r[0], r[2], r[3], r[9] / 1e6))
