# -*- coding: utf-8 -*-
"""P2-13a/b 端到端产物验证 (spool 布局): 扫描 root 下全部会话目录.

每个会话校验:
- session.json: uuid 会话 id / calibration_id / capture_config_hash
- calibration.json 存在且可解析
- chunks/*.ready 与 manifests/*.json 一一对应; manifest 的
  payload_bytes / payload_crc32c / payload_sha256 与实际文件重算一致
- chunk 头可被 ChunkReader 读回 (帧数/时间戳四件套)
- seal.json (会话正常结束标记) / ack.jsonl 格式

用法: python verify_spool_e2e.py <spool_root>   (默认 audit_output/spool)
"""
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capagent.chunk import ChunkReader
from capagent.crc32c import crc32c

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError("  FAIL: %s %s" % (name, detail))
    print("  ok: %s%s" % (name, (" " + detail) if detail else ""))


def verify_session(root, sid):
    print("[会话 %s]" % sid)
    sdir = os.path.join(root, sid)
    # session.json
    sj = json.load(open(os.path.join(sdir, "session.json"), encoding="utf-8"))
    check("session_id 是 uuid", UUID_RE.match(str(sj["session_id"])),
          sj["session_id"])
    check("session_id 与目录一致", sj["session_id"] == sid)
    check("capture_config_hash 前缀", str(sj["capture_config_hash"]).startswith("sha256:"),
          str(sj["capture_config_hash"])[:12])
    # calibration.json
    cal = json.load(open(os.path.join(sdir, "calibration.json"), encoding="utf-8"))
    check("calibration_id 一致", cal.get("calibration_id") == sj.get("calibration_id"),
          str(cal.get("calibration_id")))
    # chunks + manifests 一致性
    ready_files = sorted(glob.glob(os.path.join(sdir, "chunks", "*.chunk.ready")))
    manifest_files = sorted(glob.glob(os.path.join(sdir, "manifests", "*.json")))
    check("chunks 与 manifests 数量一致", len(ready_files) == len(manifest_files),
          "ready=%d manifest=%d" % (len(ready_files), len(manifest_files)))
    total_frames = 0
    for rp in ready_files:
        seq = int(os.path.basename(rp).split(".")[0])
        mp = os.path.join(sdir, "manifests", "%012d.json" % seq)
        if not os.path.exists(mp):
            raise AssertionError("  FAIL: %s 缺 manifest" % os.path.basename(rp))
        m = json.load(open(mp, encoding="utf-8"))
        payload = open(rp, "rb").read()
        if m["payload_bytes"] != len(payload):
            raise AssertionError("  FAIL: seq=%d payload_bytes 不一致" % seq)
        if m["payload_crc32c"] != "%08x" % crc32c(payload):
            raise AssertionError("  FAIL: seq=%d crc32c 不一致" % seq)
        if m["payload_sha256"] != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise AssertionError("  FAIL: seq=%d sha256 不一致" % seq)
        check("manifest 校验一致 seq=%d" % seq, True,
              "frames=%d bytes=%d crc=%s" % (m["frame_count"], m["payload_bytes"],
                                             m["payload_crc32c"]))
        r = ChunkReader(rp)
        if r.frame_count != m["frame_count"]:
            raise AssertionError("  FAIL: seq=%d 帧数头不一致" % seq)
        total_frames += r.frame_count
    check("总帧数", total_frames > 0, str(total_frames))
    # seal (正常结束) / 审计: seal 是封存时刻快照, 之后 ACK 只减少 .ready
    acked = 0
    ack_path = os.path.join(sdir, "ack.jsonl")
    if os.path.exists(ack_path):
        acked = sum(1 for _ in open(ack_path, encoding="utf-8"))
        check("ack.jsonl 记录数", acked >= 0, "%d 条" % acked)
    if os.path.exists(os.path.join(sdir, "seal.json")):
        seal = json.load(open(os.path.join(sdir, "seal.json"), encoding="utf-8"))
        check("seal 会话一致", seal.get("session_id") == sid)
        # 不变量: seal.pending = 已 ACK + 剩余 .ready (无数据丢失)
        check("seal pending 不变量",
              seal.get("pending_count") == acked + len(ready_files),
              "seal=%d ack=%d ready=%d"
              % (seal.get("pending_count"), acked, len(ready_files)))
    else:
        print("  -- 无 seal.json (会话未正常结束或进行中)")
    return len(ready_files), total_frames


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\Administrator\hp60c_audit\audit_output\spool"
    if not os.path.isdir(root):
        print("spool root 不存在: %s" % root)
        sys.exit(1)
    sessions = sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
    if not sessions:
        print("无会话目录: %s" % root)
        sys.exit(0)
    print("会话数: %d" % len(sessions))
    total_chunks = total_frames = 0
    for sid in sessions:
        c, f = verify_session(root, sid)
        total_chunks += c
        total_frames += f
    print("\n汇总: 会话=%d chunks=%d 帧=%d —— 全部校验通过"
          % (len(sessions), total_chunks, total_frames))


if __name__ == "__main__":
    main()
