# -*- coding: utf-8 -*-
"""P2-13a/b 离线端到端冒烟: 合成帧走完整本地管线 (无需相机).

流程:
1. 会话 A: 开启会话 (uuid) -> submit 合成帧 -> seal -> close
2. 会话 A 重启: 新 spool 实例扫描恢复, pending 保留, seq 幂等续传
3. 会话 B (崩溃模拟): 提交中途进程"崩溃" (不 seal) -> 重启扫描
   (孤儿 .part 清理 / .ready 保留)
4. ACK 闭环: 对 A 的 2 个 chunk 模拟服务器 ACK -> payload+manifest 删除,
   ack.jsonl 记录
产物目录: audit_output/smoke_p2_13  (可用 verify_spool_e2e.py 校验)

用法: python smoke_p2_13.py
"""
import os
import shutil
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capagent.frames import Frame
from capagent.sink import LocalSpoolSink
from capagent.spool import SessionSpool

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "audit_output", "smoke_p2_13")
CHUNK_CFG = {"jpeg_quality": 85, "target_duration_ms": 2000,
             "max_payload_bytes": 64 * 1024 * 1024, "max_frame_count": 128}


def frame(i, utc_ms):
    return Frame(frame_id=i, sdk_ts_ms=utc_ms, host_qpc=i * 100, host_mono_ms=i,
                 utc_ms=utc_ms, arrival_mono_ms=utc_ms,
                 rgb=b"R" * 1000, depth=b"D" * 500,
                 rgb_wh=(640, 480), depth_wh=(640, 480))


def run_session_a(root):
    print("[会话 A: 完整生命周期]")
    sid = str(uuid.uuid4())
    sp = SessionSpool(root, sid)
    sink = LocalSpoolSink(sp, CHUNK_CFG, queue_max_frames=256)
    sink.open_session(sid, {"calibration_id": "smoke-cal-1"},
                      "sha256:smoke-config")
    # 合成 200 帧, ~30ms/帧 -> 10s 跨 5 个 2s chunk
    for i in range(200):
        if not sink.submit(frame(i, 1_000_000 + i * 30), b"J" * 400, b"Z" * 200):
            print("  submit 被拒 i=%d" % i)
    seal = sink.seal_session()
    sink.close()
    print("  会话 %s: chunks=%d seal.pending=%d"
          % (sid, sp.chunk_count(), seal["pending_count"]))
    # 重启恢复
    sp2 = SessionSpool(root, sid)
    print("  重启恢复: pending=%d next_seq=%d stats=%s"
          % (len(sp2.pending_chunks()), sp2.next_seq(), sp2.stats))
    assert sp2.next_seq() == sp.chunk_count() + 1, "幂等续传失败"
    # ACK 前 2 个 chunk
    for rec in sp2.pending_chunks()[:2]:
        m = sp2.chunk_info(rec["seq"])
        sp2.ack_chunk({"chunk_seq": rec["seq"], "chunk_id": m["chunk_id"],
                       "payload_sha256": m["payload_sha256"]})
    print("  ACK 2 个 chunk 后: pending=%d" % len(sp2.pending_chunks()))
    return sid


def run_session_b(root):
    print("[会话 B: 崩溃恢复模拟]")
    sid = str(uuid.uuid4())
    sp = SessionSpool(root, sid)
    sink = LocalSpoolSink(sp, CHUNK_CFG, queue_max_frames=256)
    sink.open_session(sid, {"calibration_id": "smoke-cal-2"},
                      "sha256:smoke-config")
    # 完整提交一个 chunk (确定性落盘)
    for i in range(70):
        if not sink.submit(frame(i, 2_000_000 + i * 30), b"J" * 400, b"Z" * 200):
            print("  submit 被拒 i=%d" % i)
    sink.flush_local(timeout=30)
    committed = sp.chunk_count()
    # 模拟提交中崩溃: 在远超 next_seq 的槽位留孤儿 .part (不撞写线程)
    orphan_seq = sp.next_seq() + 100
    with open(sp.chunk_path(orphan_seq, "part"), "wb") as f:
        f.write(b"partial-crash")
    sink._stop.set()                       # 模拟崩溃: 剩余内存数据丢弃
    # 重启扫描
    sp2 = SessionSpool(root, sid)
    print("  恢复: 已提交=%d 恢复后 pending=%d 孤儿 part 清理=%d"
          % (committed, len(sp2.pending_chunks()), sp2.stats["part_cleaned"]))
    assert sp2.stats["part_cleaned"] >= 1, "孤儿 .part 未清理"
    assert len(sp2.pending_chunks()) == committed, ".ready 未保留"
    return sid


def main():
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    sid_a = run_session_a(ROOT)
    run_session_b(ROOT)
    print("\n冒烟产物: %s (会话 %s)" % (ROOT, sid_a))
    print("下一步: python verify_spool_e2e.py %s" % ROOT)


if __name__ == "__main__":
    main()
