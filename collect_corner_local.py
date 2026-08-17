# -*- coding: utf-8 -*-
"""本机 90° 墙角采集脚本 (无 t7920 依赖)。

按 COLLECTION_PROTOCOL_90_CORNER.md 规范运行 7 个序列。
每个序列 60s，序列间暂停等待用户调整相机。
"""
import os
import sys
import time
import json
import subprocess

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

CONFIG = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\capagent\config_corner_local.json"
OUT_DIR = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\corner_90deg"

SEQUENCES = [
    {"id": "C01", "name": "static_facing", "desc": "静止正对墙角 (基线)", "seconds": 60},
    {"id": "C02", "name": "slow_facing_fwd", "desc": "慢速正对平移 1.5→3→1.5m", "seconds": 60},
    {"id": "C03", "name": "slow_facing_rev", "desc": "慢速正对平移 3→1.5m (反向闭环)", "seconds": 60},
    {"id": "C04", "name": "static_yaw_p10", "desc": "静止偏航 +10°", "seconds": 60},
    {"id": "C05", "name": "static_yaw_n10", "desc": "静止偏航 -10°", "seconds": 60},
    {"id": "C06", "name": "slow_yaw_p10", "desc": "慢速平移 +10° 偏航", "seconds": 60},
    {"id": "C07", "name": "fast_facing", "desc": "快速正对平移 (运动模糊上界)", "seconds": 60},
]


def run_sequence(seq):
    """运行单个采集序列。"""
    print(f"\n{'='*60}")
    print(f"[{seq['id']}] {seq['name']} - {seq['desc']}")
    print(f"时长: {seq['seconds']}s")
    print(f"{'='*60}")

    # 等待用户确认相机就位
    input(f"\n请将相机置于 [{seq['desc']}] 状态，按回车开始采集...")

    # 启动采集
    cmd = [
        sys.executable, "cap_viz.py",
        "--config", CONFIG,
        "--seconds", str(seq["seconds"])
    ]

    print(f"执行: {' '.join(cmd)}")
    start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        result = subprocess.run(cmd, timeout=seq["seconds"] + 30)
        end_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {
            "sequence": seq["id"],
            "name": seq["name"],
            "description": seq["desc"],
            "start_utc": start_utc,
            "end_utc": end_utc,
            "duration_s": seq["seconds"],
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "sequence": seq["id"],
            "name": seq["name"],
            "description": seq["desc"],
            "start_utc": start_utc,
            "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": seq["seconds"],
            "exit_code": -1,
            "error": "timeout"
        }


def main():
    print("=" * 60)
    print("本机 90° 墙角采集 (7 序列)")
    print("=" * 60)
    print(f"配置: {CONFIG}")
    print(f"输出: {OUT_DIR}")
    print(f"序列数: {len(SEQUENCES)}")
    print(f"总时长: {sum(s['seconds'] for s in SEQUENCES)}s")

    os.makedirs(OUT_DIR, exist_ok=True)

    results = []
    for seq in SEQUENCES:
        result = run_sequence(seq)
        results.append(result)

        # 序列间暂停
        if seq != SEQUENCES[-1]:
            print("\n等待 5 秒进入下一序列...")
            time.sleep(5)

    # 保存元数据
    meta_path = os.path.join(OUT_DIR, "collection_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "config": CONFIG,
            "out_dir": OUT_DIR,
            "sequences": results,
            "protocol": "COLLECTION_PROTOCOL_90_CORNER.md"
        }, f, indent=2, ensure_ascii=False)
    print(f"\n元数据已保存: {meta_path}")

    # 摘要
    print("\n" + "=" * 60)
    print("采集摘要")
    print("=" * 60)
    for r in results:
        status = "OK" if r.get("exit_code") == 0 else f"FAIL({r.get('exit_code')})"
        print(f"  [{r['sequence']}] {r['name']}: {status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())