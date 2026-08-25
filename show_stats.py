# -*- coding: utf-8 -*-
import json

s = json.load(open(r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\longrun_smoke2\stats.json", encoding="utf-8"))
print("frames_total:", s["frames_total"], "actual_fps:", s["actual_fps"], "verdict:", s["frame_rate_verdict"])
print("miss_rgb:", s["miss_rgb_frames"], "miss_depth:", s["miss_depth_frames"], "zero:", s["zero_frames"])
print("ts_non_monotonic:", s["ts_non_monotonic"])
print("host_interval p50/p95/p99/max:", s["host_interval_ms"]["p50"], s["host_interval_ms"]["p95"],
      s["host_interval_ms"]["p99"], s["host_interval_ms"]["max"])
print("gap>500ms:", s["gap_counts"]["gt_500ms"], "max_missing:", s["max_consecutive_missing_frames"])
print("rgb-depth ts delta:", s["rgb_depth_ts_delta_ms"])
print("mem private last:", s["mem"]["private_kb"]["last"], "threads last:", s["threads"]["last"])
