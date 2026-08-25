# -*- coding: utf-8 -*-
"""Capture Agent: 相机采集 -> 有界队列 -> 编码(JPEG/Zstd) -> sink 提交.

P2-13a/b: sink 为 LocalSpoolSink (规格 §3.1/§3.2/§3.4):
- 会话 id 用 uuid (协议要求, §5 幂等键组件)
- 编码后帧 submit() 入 sink 有界队列 (ACCEPTED), sink 写线程做
  ChunkBuilder 切块 (§4.4) + §4.3 十步本地提交 -> .ready (LOCAL_DURABLE)
- session.json/calibration.json 由 sink.open_session 分离写入 (§4.1)
- spool 使用率 (§12.4): stop 级暂停 submit (丢帧计数), 不删除任何未 ACK 数据

线程模型 (禁止网络线程阻塞相机线程):
- SDK 回调线程: 深拷贝 + 入有界队列 (put_nowait, 满则丢帧计数) — 永不阻塞
- 消费者线程: 取帧 -> 编码 -> sink.submit (入队, 不阻塞)
- sink 写线程: 切块 + crc32c/sha256 + 落盘 (与相机线程解耦)
- Watchdog 线程: adapter 内部, 帧停滞检测
- 主线程: 事件循环 (STALLED -> recover), 统计, 超时退出
"""
import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from capagent.adapter import SdkAdapter
from capagent.config import capture_config_hash, load_config
from capagent.dcw_backend import DaBaiDCWBackend
from capagent.encoder import FrameEncoder
from capagent.errors import CameraError
from capagent.frames import now_mono_ms
from capagent.sink import LocalSpoolSink, SinkState
from capagent.spool import SessionSpool
from capagent.state import CameraState
from capagent.visualizer import LiveVisualizer
from capagent.denoise import TemporalMedianDenoiser

log = logging.getLogger("capagent")


# ---------------------------------------------------------------------- 统计
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = 0
        self.dropped = 0
        self.zero_depth_frames = 0
        self.encoded = 0
        self.encode_ms_total = 0.0
        self.spool_stop_drops = 0
        self.stall_events = 0
        self.recovered = 0
        self.t0 = time.monotonic()

    def snapshot(self, sink, state):
        st = sink.status() if sink else {}
        with self.lock:
            elapsed = time.monotonic() - self.t0
            fps = self.frames / elapsed if elapsed > 0 else 0.0
            return {
                "elapsed_s": round(elapsed, 1),
                "frames": self.frames,
                "fps": round(fps, 2),
                "dropped": self.dropped,
                "drop_ratio": round(self.dropped / max(1, self.frames + self.dropped), 4),
                "zero_depth_frames": self.zero_depth_frames,
                "encoded": self.encoded,
                "avg_encode_ms": round(self.encode_ms_total / max(1, self.encoded), 2),
                "spool_stop_drops": self.spool_stop_drops,
                "sink_state": st.get("state"),
                "chunks_local_durable": st.get("chunks_local_durable"),
                "pending_count": st.get("pending_count"),
                "spool_usage_gb": round((st.get("usage_bytes") or 0) / 1024 ** 3, 3),
                "spool_usage_ratio": st.get("usage_ratio"),
                "spool_usage_level": st.get("usage_level"),
                "stall_events": self.stall_events,
                "recovered": self.recovered,
                "state": state,
            }

    def bump(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)


# ---------------------------------------------------------------------- Agent
class CaptureAgent:
    def __init__(self, cfg):
        self.cfg = cfg
        cam, wd, cap, sp, run = (cfg["camera"], cfg["watchdog"], cfg["capture"],
                                 cfg["spool"], cfg["run"])
        self.session_id = str(uuid.uuid4())           # 协议要求 uuid (§5)
        self.spool = SessionSpool(
            root=sp["root"], session_id=self.session_id,
            max_size_bytes=int(sp["max_size_gb"] * 1024 ** 3),
            warning_ratio=sp["warning_ratio"],
            throttle_ratio=sp["throttle_ratio"],
            stop_ratio=sp["stop_ratio"])
        chunk_cfg = dict(cfg["chunk"])
        chunk_cfg["jpeg_quality"] = cap["jpeg_quality"]
        chunk_cfg["depth_codec"] = 2 if cap.get("raw_depth", False) else 1
        self.sink = LocalSpoolSink(self.spool, chunk_cfg,
                                   queue_max_frames=sp["queue_max_frames"])
        self.encoder = FrameEncoder(jpeg_quality=cap["jpeg_quality"],
                                    zstd_level=cap["zstd_level"])
        self.raw_depth = cap.get("raw_depth", False)
        self.queue = queue.Queue(maxsize=cap["queue_max_frames"])
        self.stats = Stats()
        self.running = threading.Event()
        self.running.set()
        self._frame_id_lock = threading.Lock()
        self._next_frame_id = 1                        # 会话内递增 (uuid 会话隔离)
        self._consumer = None
        self._last_level = None
        self.calibration = None
        self.worker = None                     # NetworkDeliveryWorker (P2-13c/d)
        self.visualizer = None                 # 实时点云可视化 (P2-vis)
        if cfg["run"].get("visualize", False):
            cam = cfg["camera"]
            self.visualizer = LiveVisualizer(
                fx=cam.get("fx", 361.376), fy=cam.get("fy", 361.376),
                cx=cam.get("cx", 318.517), cy=cam.get("cy", 179.502))

        # Temporal median denoiser (P2-15d: gallery analysis showed 7mm RMS)
        denoise_cfg = cfg["capture"].get("denoise", {})
        self.denoiser = None
        if denoise_cfg.get("enabled", False):
            window = denoise_cfg.get("window_size", 5)
            depth_w = cam.get("width", 640)
            depth_h = cam.get("height", 480)
            self.denoiser = TemporalMedianDenoiser(window_size=window,
                                                    depth_shape=(depth_h, depth_w))
            log.info("Temporal median denoiser enabled: window=%d, shape=(%d,%d)",
                     window, depth_h, depth_w)

        if cfg["network"]["enabled"]:
            from capagent.network_worker import NetworkDeliveryWorker
            net = cfg["network"]
            self.worker = NetworkDeliveryWorker(
                self.spool, net, url=net["url"], token=net["token"],
                client_id=net["client_id"], agent_version=net["agent_version"])
            self.sink.attach_network(self.worker)
        if cam.get("dcw_backend", False):
            # DCW 生产 profile: color 640x480@30 RGB, depth 640x360@30 Y16, D2C off
            dcw_cam = cam.get("dcw", {})
            color_cfg = dcw_cam.get("color", {})
            depth_cfg = dcw_cam.get("depth", {})
            d2c_cfg = dcw_cam.get("d2c", {})
            self.adapter = DaBaiDCWBackend(
                width=color_cfg.get("width", 640),
                height=color_cfg.get("height", 480),
                fps=color_cfg.get("fps", 30),
                depth_width=depth_cfg.get("width", 640),
                depth_height=depth_cfg.get("height", 360),
                serial=cam["serial"],
                d2c=d2c_cfg.get("enabled", False),
                align_mode=d2c_cfg.get("align_mode", "OFF"),
                color_format=color_cfg.get("format", "RGB"),
                depth_format=depth_cfg.get("format", "Y16"),
                on_frame=self._on_frame, on_state=self._on_state, on_event=self._on_event)
        else:
            self.adapter = SdkAdapter(
                width=cam["width"], height=cam["height"], fps=cam["fps"],
                serial=cam["serial"], config_file=cam["config_file"],
                include_net=cam["include_net"],
                on_frame=self._on_frame, on_state=self._on_state, on_event=self._on_event)
        self.adapter.state.max_reconnect_attempts = wd["max_reconnect_attempts"]

    # ------------------------------------------------------------------ 回调 (SDK 线程, 非阻塞!)
    def _on_frame(self, frame):
        with self._frame_id_lock:
            fid = self._next_frame_id
            self._next_frame_id += 1
        frame.frame_id = fid
        frame.arrival_mono_ms = now_mono_ms()
        # 过滤空帧: SDK 偶尔返回空 RGB/Depth 数据
        if not frame.rgb or not frame.depth:
            self.stats.bump(dropped=1)
            return
        if frame.depth_zeros:
            self.stats.bump(zero_depth_frames=1)
        # 实时可视化: 喂入最新帧 (非阻塞)
        if self.visualizer is not None:
            self.visualizer.update_frame(frame.depth, frame.rgb)
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            self.stats.bump(dropped=1)              # 有界队列: 满则丢帧, 不阻塞相机线程
            return
        self.stats.bump(frames=1)

    def _on_state(self, src, dst, reason):
        log.info("state: %s -> %s%s", src.value, dst.value,
                 (" (%s)" % reason) if reason else "")

    def _on_event(self, name, payload):
        log.warning("event: %s %s", name, payload)
        if name == "STALLED":
            self.stats.bump(stall_events=1)

    # ------------------------------------------------------------------ 消费者线程
    def _consumer_loop(self):
        while self.running.is_set() or not self.queue.empty():
            try:
                frame = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            t0 = time.perf_counter()
            try:
                rgb_jpeg = self.encoder.encode_rgb(frame.rgb, frame.rgb_wh[1], frame.rgb_wh[0])

                # Temporal median denoising (P2-15d)
                depth_bytes = frame.depth
                if self.denoiser is not None:
                    depth_bytes = self.denoiser.process(depth_bytes)

                depth_z = self.encoder.encode_depth(depth_bytes, raw=self.raw_depth)
            except Exception as e:
                log.error("编码失败 frame=%d: %r", frame.frame_id, e)
                self.stats.bump(encoded=1)          # 记一次失败, 继续
                continue
            self.stats.bump(encoded=1, encode_ms_total=(time.perf_counter() - t0) * 1000)
            self._maybe_pause_for_spool()
            if self.sink.state == SinkState.PAUSED:
                # §12.4 stop 级: 暂停 submit, 丢帧计数 (不删除任何未 ACK 数据)
                self.stats.bump(spool_stop_drops=1)
                continue
            try:
                ok = self.sink.submit(frame, rgb_jpeg, depth_z)
            except CameraError as e:
                log.error("sink 拒绝 frame=%d: %r", frame.frame_id, e)
                self.stats.bump(dropped=1)
                continue
            if not ok:
                self.stats.bump(dropped=1)   # sink 队列满 (写线程积压), 丢帧保相机

    def _maybe_pause_for_spool(self):
        """§12.4: spool 使用率分级 -> Agent 决定暂停/恢复 submit.

        stop 级: 暂停 submit (丢帧计数, 不删除任何未 ACK 数据);
        throttle/warning: 记录但不干预 (sink 写线程与采集解耦, 正常消化).
        """
        try:
            level = self.sink.status()["usage_level"]
        except Exception:
            return
        if level == self._last_level:
            return
        if level == "stop":
            log.error("spool 使用率达 stop 级 (%.2f), 暂停 submit, 未 ACK 数据保留",
                      self.sink.status()["usage_ratio"])
            self.sink.set_paused(True)
            self.spool.append_events({"type": "SPOOL_STOP", "session_id": self.session_id,
                                      "usage_ratio": self.sink.status()["usage_ratio"]})
        elif self._last_level == "stop":
            log.warning("spool 使用率回落, 恢复 submit")
            self.sink.set_paused(False)
        elif level in ("warning", "throttle"):
            log.warning("spool 使用率 %s 级 (%.2f)", level,
                        self.sink.status()["usage_ratio"])
        self._last_level = level

    # ------------------------------------------------------------------ 运行
    def run(self, seconds=None):
        seconds = seconds if seconds is not None else self.cfg["run"]["seconds"]
        log.info("CaptureAgent 启动: session=%s spool=%s",
                 self.session_id, self.spool.dir)
        self.adapter.start()
        self.calibration = self.adapter.open_session()
        meta = self.sink.open_session(
            self.session_id, self.calibration,
            capture_config_hash(self.cfg),
            session_meta={"camera": self.cfg["camera"],
                          "capture": {k: self.cfg["capture"][k]
                                      for k in ("jpeg_quality", "zstd_level")}})
        log.info("会话已开启: %s (calibration=%s)",
                 meta["session_id"], meta.get("calibration_id"))
        self._consumer = threading.Thread(target=self._consumer_loop,
                                          name="consumer", daemon=True)
        self._consumer.start()
        if self.visualizer is not None:
            self.visualizer.start()
            log.info("实时可视化已启动")
        if self.worker is not None:
            self.worker.start()                # 网络线程: 持续上传 .ready (P2-13c/d)

        t_start = time.monotonic()
        last_stats = 0.0
        try:
            while self.running.is_set():
                time.sleep(0.2)
                st = self.adapter.state.state
                if st is CameraState.STALLED:
                    log.info("检测到停滞, 开始恢复...")
                    ok = self.adapter.recover()
                    if ok and self.adapter.state.state is CameraState.STREAMING:
                        self.stats.bump(recovered=1)
                    elif self.adapter.state.state is CameraState.DISCONNECTED:
                        log.error("恢复失败达上限, 会话终止")
                        break
                elif st is CameraState.DISCONNECTED:
                    log.error("相机断开, 会话终止")
                    break
                now = time.monotonic()
                if seconds and now - t_start >= seconds:
                    log.info("达到运行时长 %.0fs", seconds)
                    break
                if now - last_stats >= self.cfg["run"]["stats_interval_s"]:
                    last_stats = now
                    self._log_stats()
        except KeyboardInterrupt:
            log.info("用户中断")
        finally:
            self.stop()

    def _log_stats(self):
        s = self.stats.snapshot(self.sink, self.adapter.state.state.value)
        log.info("stats: %s", json.dumps(s, ensure_ascii=False))

    def stop(self):
        self.running.clear()
        if self.visualizer is not None:
            self.visualizer.stop()
        if self._consumer:
            self._consumer.join(timeout=5)
        try:
            # §11 会话结束闭环: 本地 seal (含协议快照) -> 远程 flush ->
            # SESSION_SEAL/ACK -> receipt. seal 在 flush 前, 快照取自
            # session_manifest.jsonl, 与 ACK 删除 manifests 解耦.
            self.sink.seal_session()
            if self.worker is not None:
                self.worker.flush_remote()
                self.sink.commit_session()
            self.sink.close()
        except CameraError as e:
            log.error("seal/close 失败: %r", e)
        finally:
            if self.worker is not None:
                self.worker.stop()
            log.info("CaptureAgent 停止: %s", json.dumps(
                self.stats.snapshot(self.sink, "stopped"), ensure_ascii=False))
            self.adapter.stop_session(terminate=True)   # 无论 seal 成败必关相机


def main():
    ap = argparse.ArgumentParser(description="HP60C Capture Agent (Windows)")
    ap.add_argument("--config", default=None, help="JSON 配置文件路径")
    ap.add_argument("--seconds", type=float, default=None, help="运行秒数 (默认无限)")
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--root", default=None, help="覆盖 spool.root")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.seconds is not None:
        cfg["run"]["seconds"] = args.seconds
    if args.log_file:
        cfg["run"]["log_file"] = args.log_file
    if args.root:
        cfg["spool"]["root"] = args.root

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        filename=cfg["run"]["log_file"],
        filemode="a" if cfg["run"]["log_file"] else None)
    if not cfg["run"]["log_file"]:
        logging.getLogger().handlers[0].stream = sys.stdout

    try:
        CaptureAgent(cfg).run()
    except CameraError as e:
        log.error("CameraError: %r", e)
        sys.exit(1)
    except Exception as e:
        log.error("未预期异常: %r", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
