# -*- coding: utf-8 -*-
"""实验 A 正式统计: 读 audit_output/longrun/ 输出完整指标"""
import json
import numpy as np

BASE = r"C:\Users\Administrator\hp60c_audit\audit_output\longrun"
s = json.load(open(BASE + r"\stats.json", encoding="utf-8"))
sess = json.load(open(BASE + r"\session.json", encoding="utf-8"))
sam = json.load(open(BASE + r"\samples.json", encoding="utf-8"))
cal = json.load(open(BASE + r"\calibration_snapshot.json", encoding="utf-8"))

print("=== 会话 ===")
print("started:", sess["started_utc"], "| minutes_target:", sess["minutes_target"])
print("init:", {k: sess[k] for k in ("open_ret", "set_stream_param_ret", "set_timestamp_type_ret",
                                     "register_callback_ret", "start_stream_ret", "get_cam_parameter_ret")})
print("first_frame_ok:", sess["first_frame_ok"])

print("\n=== 帧率 ===")
print("frames_total:", s["frames_total"], "| elapsed_s:", s["elapsed_s"],
      "| actual_fps: %.4f" % s["actual_fps"], "| verdict:", s["frame_rate_verdict"])
h = s["host_interval_ms"]
print("host interval ms: mean=%.2f p50=%.2f p95=%.2f p99=%.2f max=%.2f min=%.2f"
      % (h["mean"], h["p50"], h["p95"], h["p99"], h["max"], h["min"]))
print("sdk ts interval ms: mean=%.2f p95=%.2f max=%.2f"
      % (s["sdk_ts_interval_ms"]["mean"], s["sdk_ts_interval_ms"]["p95"], s["sdk_ts_interval_ms"]["max"]))
print("gap counts >100/200/500/1000ms:", s["gap_counts"]["gt_100ms"], s["gap_counts"]["gt_200ms"],
      s["gap_counts"]["gt_500ms"], s["gap_counts"]["gt_1000ms"])
print("max consecutive missing frames:", s["max_consecutive_missing_frames"])

print("\n=== 帧质量 ===")
print("zero_frames:", s["zero_frames"], "| miss_rgb:", s["miss_rgb_frames"],
      "| miss_depth:", s["miss_depth_frames"], "| ts_non_monotonic:", s["ts_non_monotonic"])
print("rgb-depth ts delta ms:", s["rgb_depth_ts_delta_ms"])
print("ts units_per_sec: %.2f" % s["ts_units_per_sec"], "| ts_range:", s["ts_range"])
print("first_frame_utc:", s["first_frame_utc"], "| stop_reason:", s["stop_reason"])

print("\n=== 资源 ===")
mem = s["mem"]
thr = s["threads"]
print("mem private_kb min/max/last:", mem["private_kb"]["min"], mem["private_kb"]["max"],
      mem["private_kb"]["last"], "| samples:", mem["samples"])
print("mem working_kb min/max/last:", min(m["working_kb"] for m in sam["mem"]),
      max(m["working_kb"] for m in sam["mem"]), sam["mem"][-1]["working_kb"])
print("threads min/max/last:", thr["min"], thr["max"], thr["last"], "| samples:", thr["samples"])
# 内存/线程随时间变化趋势 (每 30s)
print("\nmem/threads timeline (every 10th sample):")
for i in range(0, len(sam["mem"]), 10):
    m = sam["mem"][i]
    t = sam["threads"][i]
    print("  t=%6.0fs private=%6dKB working=%6dKB threads=%3d" % (m["t_s"], m["private_kb"], m["working_kb"], t["threads"]))
print("  t=%6.0fs private=%6dKB working=%6dKB threads=%3d (last)"
      % (sam["mem"][-1]["t_s"], sam["mem"][-1]["private_kb"], sam["mem"][-1]["working_kb"],
         sam["threads"][-1]["threads"]))

print("\n=== 标定快照 ===")
print("calibration_id:", cal["calibration_id"])
print("serial:", cal["camera_serial"], "| config_version:", cal["config_version"])
print("fxir=%.4f fxrgb=%.4f T=(%.3f,%.3f,%.3f)"
      % (cal["values"]["fxir"], cal["values"]["fxrgb"],
         cal["values"]["T1"], cal["values"]["T2"], cal["values"]["T3"]))

# 间隔分布直方图
rows = [json.loads(l) for l in open(BASE + r"\frames.jsonl", encoding="utf-8") if l.strip()]
iv = np.diff(np.array([r[4] for r in rows], dtype=np.int64)) / 1e6
buckets = np.arange(0, 200, 5)
hist, _ = np.histogram(iv, bins=buckets)
print("\n=== host interval histogram (ms) ===")
for b, c in zip(buckets[:-1], hist):
    if c:
        print("  %3d-%3d ms: %6d (%.2f%%)" % (b, b + 5, c, 100.0 * c / len(iv)))
print("  total intervals:", len(iv))
