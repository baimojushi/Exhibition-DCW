# -*- coding: utf-8 -*-
import json
import numpy as np

path = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\longrun_smoke\frames.jsonl"
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
print("first 5 rows:")
for r in rows[:5]:
    print(r)
print("last 3 rows:")
for r in rows[-3:]:
    print(r)
g = np.diff(np.array([r[3] for r in rows], dtype=np.int64))
print("ts diff nonzero count:", int(np.count_nonzero(g)), "of", len(g))
print("unique ts values:", len(set(r[3] for r in rows)))
h = np.diff(np.array([r[4] for r in rows], dtype=np.int64)) / 1e6
import collections
hist = collections.Counter(int(x // 10) * 10 for x in h)
print("host interval histogram (ms buckets):", sorted(hist.items()))
