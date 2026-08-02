# -*- coding: utf-8 -*-
"""P2-15c MCAP 集成验收 (规格 §18, 验收 §20: confidence 可写入 MCAP 并正常回放).

流程: 流式抽样真机帧 -> 计算 confidence/reason -> 写 MCAP (profile=ros2,
CDR 编码, topic 命名按 §18) -> mcap reader 回放逐字节对比 -> metadata 审计
(algorithm_id/software_version/config_sha256/input_session_sha256) ->
MCAP 文件 sha256 (output_sha256) 落盘 summary.

运行: python verify_mcap.py [spool_root] [--max-frames N] [--out DIR]
"""
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\pyopencv")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import numpy as np

from depth_confidence import ConfidenceConfig, compute_confidence_result

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if not cond:
        FAIL.append(name)
        raise AssertionError("%s 失败 %s" % (name, (" " + detail) if detail else ""))
    PASS += 1
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def session_identity_sha256(spool_root):
    """会话 chunk 身份累积日志 (session_manifest.jsonl) 的 sha256 (input_session_sha256)."""
    import glob
    paths = sorted(glob.glob(os.path.join(
        os.path.abspath(spool_root), "*", "session_manifest.jsonl")))
    digest = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            digest.update(f.read())
    return "sha256:" + digest.hexdigest()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P2-15c MCAP 集成验收")
    parser.add_argument("spool_root", nargs="?",
                        default=os.path.join("audit_output", "spool_real"))
    parser.add_argument("--max-frames", type=int, default=144)
    parser.add_argument("--out", default=os.path.join("audit_output",
                                                      "mcap_v1"))
    args = parser.parse_args()

    from capagent.confidence import iter_spool_frame_records
    from capagent.mcap_writer import write_confidence_mcap

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    # 流式抽样 (与 P2-15a/b 同逻辑)
    total = sum(1 for _ in iter_spool_frame_records(args.spool_root))
    step = max(1, int(math.ceil(total / args.max_frames)))
    sampled = list(itertools.islice(iter_spool_frame_records(args.spool_root),
                                    0, None, step))
    if args.max_frames >= 100:
        check("抽样帧数 >= 100", len(sampled) >= 100, "got=%d" % len(sampled))

    config = ConfidenceConfig()
    frames = []
    for rec in sampled:
        r = compute_confidence_result(rec["depth_u16"], config)
        frames.append({
            "frame_id": rec["frame_id"], "utc_ms": rec["utc_ms"],
            "depth_u16": rec["depth_u16"],
            "confidence_u8": r.confidence_u8,
            "reason_u8": r.reason_bits_u8,
        })
    check("置信度计算完成 (%d 帧)" % len(frames), len(frames) == len(sampled))

    # 写 MCAP
    mcap_path = os.path.join(out_dir, "confidence_%s.mcap"
                             % git_short_sha())
    metadata = {
        "algorithm_id": "hp60c_depth_confidence_v1",
        "software_version": "git:" + git_short_sha(),
        "config_sha256": config.sha256(),
        "input_session_sha256": session_identity_sha256(args.spool_root),
    }
    result = write_confidence_mcap(mcap_path, frames, metadata=metadata)
    check("MCAP 写入完成", os.path.exists(mcap_path)
          and os.path.getsize(mcap_path) > 0,
          "%.1fMB" % (os.path.getsize(mcap_path) / 1e6,))
    check("3 图像 channel + camera_info 消息数",
          result["counts"]["depth"] == len(frames)
          and result["counts"]["confidence"] == len(frames)
          and result["counts"]["reason"] == len(frames)
          and result["counts"]["camera_info"] == 1,
          "counts=%r" % result["counts"])

    # 回放: mcap reader 读回逐字节对比
    from mcap.reader import make_reader
    replayed = {"depth": [], "confidence": [], "reason": []}
    meta_back = {}
    with open(mcap_path, "rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        schemas = summary.schemas
        for _, channel, msg in reader.iter_messages():
            if channel.topic == "/hp60c/depth_registered/camera_info":
                continue
            topic = channel.topic.split("/")[-1]
            if topic == "image_raw":
                key = "depth"
            elif topic == "confidence":
                key = "confidence"
            elif topic == "confidence_reason":
                key = "reason"
            else:
                continue
            replayed[key].append(msg.data)
        for md in reader.iter_metadata():
            meta_back.update(md.metadata)
    check("回放: 每条 topic 消息数一致",
          len(replayed["depth"]) == len(frames)
          and len(replayed["confidence"]) == len(frames)
          and len(replayed["reason"]) == len(frames))

    # 逐字节对比: 从 CDR payload 提取 data 字段 (Image data 是最后字段)
    def extract_image_data(payload):
        # header(4+4+4+len(frame_id)+1) + h(4) + w(4) + enc(len+1) + be(1) + step(4) + data(len+payload)
        i = 0
        def u32():
            nonlocal i
            v = int.from_bytes(payload[i:i + 4], "little"); i += 4; return v
        i += 8                                   # Time sec/nanosec
        frm_len = u32()
        i += frm_len + 1                          # frame_id + NUL
        u32()                                     # height
        u32()                                     # width
        enc_len = u32()
        i += enc_len + 1
        i += 1                                    # is_bigendian
        u32()                                     # step
        n = u32()                                 # data 长度
        return payload[i:i + n]

    depth_bytes = [extract_image_data(p) for p in replayed["depth"]]
    conf_bytes = [extract_image_data(p) for p in replayed["confidence"]]
    reason_bytes = [extract_image_data(p) for p in replayed["reason"]]
    ok_depth = all(depth_bytes[i] == frames[i]["depth_u16"].astype("<u2").tobytes()
                   for i in range(len(frames)))
    ok_conf = all(conf_bytes[i] == frames[i]["confidence_u8"].tobytes()
                  for i in range(len(frames)))
    ok_reason = all(reason_bytes[i] == frames[i]["reason_u8"].tobytes()
                    for i in range(len(frames)))
    check("回放: depth (16UC1) 逐字节一致", ok_depth)
    check("回放: confidence (mono8) 逐字节一致", ok_conf)
    check("回放: confidence_reason (mono8) 逐字节一致", ok_reason)

    check("metadata 审计含 algorithm_id",
          meta_back.get("algorithm_id") == "hp60c_depth_confidence_v1")
    check("metadata 审计含 software_version (git)",
          meta_back.get("software_version", "").startswith("git:"))
    check("metadata 审计含 config_sha256",
          meta_back.get("config_sha256") == config.sha256())
    check("metadata 审计含 input_session_sha256",
          bool(meta_back.get("input_session_sha256", "").startswith("sha256:")))

    summary_out = {
        "mcap": os.path.abspath(mcap_path),
        "mcap_sha256": result["mcap_sha256"],
        "frames": len(frames),
        "topics": result["channels"],
        "counts": result["counts"],
        "metadata": metadata,
        "replayed_byte_identical": ok_depth and ok_conf and ok_reason,
    }
    with open(os.path.join(out_dir, "summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary_out, f, ensure_ascii=False, indent=1)
    print("== P2-15c MCAP 验收: %d 项通过, %d 项失败 ==" % (PASS, len(FAIL)))
    print("  mcap: %s" % summary_out["mcap"])
    print("  mcap_sha256: %s" % summary_out["mcap_sha256"])
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
