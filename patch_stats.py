# -*- coding: utf-8 -*-
"""修正 stats.json 的 sdk_ts_interval_ms (ts 已是 ms, 不应 /1e6), 其余字段不动."""
import json
import numpy as np

BASE = r"C:\Users\Administrator\hp60c_audit\audit_output\longrun"
p = BASE + r"\stats.json"
s = json.load(open(p, encoding="utf-8"))

rows = [json.loads(l) for l in open(BASE + r"\frames.jsonl", encoding="utf-8") if l.strip()]
ts_d = np.array([r[3] for r in rows], dtype=np.int64)
g = np.diff(ts_d)


def pct(a, pv):
    return float(np.percentile(a, pv)) if len(a) else None


s["sdk_ts_interval_ms"] = {
    "mean": float(np.mean(g)) if len(g) else None,
    "p50": pct(g, 50), "p95": pct(g, 95), "p99": pct(g, 99), "max": pct(g, 100),
}
s["sdk_ts_interval_ms"]["note"] = "sdk ts 单位为 ms; 与 host 间隔一致 (100% 帧 rgb.ts==depth.ts, 每帧更新)"

json.dump(s, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("patched sdk_ts_interval_ms:", json.dumps(s["sdk_ts_interval_ms"], ensure_ascii=False))
