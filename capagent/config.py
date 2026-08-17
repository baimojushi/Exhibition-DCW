# -*- coding: utf-8 -*-
"""配置加载: 默认值 + JSON 覆盖. 默认值即规格基线 (640x480/30fps/2s chunk/5GB).

P2-13: chunk 段 = §4.4 切块约束 (时间为主, payload/帧数封顶);
spool 段 = §12.4 使用率分级 (normal/warning/throttle/stop).
capture_config_hash() = §4.1 采集配置指纹, 写入 session.json 供服务器校验.
"""
import hashlib
import json
import os

DEFAULTS = {
    "camera": {
        "width": 640, "height": 480, "fps": 30,       # §7A: 仅 fps=30 成对完整流
        "serial": None,
        "config_file": None,                           # None -> SDK 默认 HP60C 配置
        "include_net": False,
        "dcw_backend": False,                          # True = DaBaiDCWBackend (v1 SDK)
        "dcw": {                                       # DCW 生产 profile (production_profile.json 对齐)
            "color": {
                "width": 640,
                "height": 480,
                "fps": 30,
                "format": "RGB"
            },
            "depth": {
                "width": 640,
                "height": 360,
                "fps": 30,
                "format": "Y16"
            },
            "d2c": {
                "enabled": False,
                "align_mode": "OFF"
            }
        },
    },
    "watchdog": {
        "stall_timeout_ms": 500,                        # §2 (实验 A: p99=74ms)
        "bad_frame_limit": 3,
        "first_frame_timeout_s": 5.0,
        "max_reconnect_attempts": 3,                    # §2: 多次失败终止会话
    },
    "capture": {
        "jpeg_quality": 85,
        "zstd_level": 3,
        "chunk_seconds": 2.0,                           # 2 秒 chunk (已被 chunk.target_duration_ms 取代)
        "queue_max_frames": 64,                         # 内存有界队列
        "denoise": {                                    # P2-15d: temporal median denoising
            "enabled": False,
            "window_size": 5
        },
    },
    "chunk": {                                          # P2-13b: §4.4 切块约束
        "target_duration_ms": 2000,                     # 时间为主 (2s, 不假设固定帧数)
        "max_payload_bytes": 64 * 1024 * 1024,          # 64 MiB payload 上限
        "max_frame_count": 128,                         # 128 帧上限
    },
    "store": {                                          # 已废弃 (被 spool 取代), 仅兼容旧测试
        "root": r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\capture",
        "quota_gb": 5.0,                                # 约 5 GB 本地循环缓存
        "delete_on_ack": True,                          # ACK 后删除
    },
    "spool": {                                          # P2-13b: §12.4 本地原子 Spool
        "root": r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output\spool",
        "max_size_gb": 5.0,                             # 未 ACK 数据保留上限 (只约束, 不驱逐删除)
        "warning_ratio": 0.80,
        "throttle_ratio": 0.90,
        "stop_ratio": 0.97,
        "queue_max_frames": 256,                        # sink 有界队列
    },
    "network": {                                        # P2-13c/d: 网络上传 (hp_upload_ref)
        "enabled": False,
        "url": None,                                    # ws://host:port 或 wss:// (tailscale serve)
        "token": None,                                  # Bearer token (可选)
        "client_id": "hp60c-capture-agent",
        "agent_version": "git:local",
        "max_reconnect_attempts": 8,                    # 单轮 upload_pending 重连次数上限
        "connect_timeout_s": 10.0,                      # §12.2
        "control_idle_timeout_s": 10.0,
        "heartbeat_interval_s": 2.0,
        "stream_no_progress_timeout_s": 30.0,
        "commit_ack_timeout_s": 15.0,
        "requested_max_inflight": 4,                    # §6.1 max_inflight_chunks
        "backoff_s": [1, 2, 4, 8, 15],                  # §12.1 退避 (+20% 抖动在库内)
    },
    "run": {
        "seconds": None,                                # None = 无限运行
        "stats_interval_s": 10.0,
        "log_file": None,
        "visualize": False,                             # 实时点云+倾斜角窗口
    },
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULTS))              # deep copy
    if path:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        _merge(cfg, user)
    # 校验
    if cfg["camera"]["fps"] not in (30,):
        raise ValueError("本 SDK 仅 fps=30 输出 RGB+Depth 成对完整流 (§7A), 收到 %d"
                         % cfg["camera"]["fps"])
    if cfg["store"]["quota_gb"] <= 0:
        raise ValueError("store.quota_gb 必须 > 0")
    sp = cfg["spool"]
    if sp["max_size_gb"] <= 0:
        raise ValueError("spool.max_size_gb 必须 > 0")
    if not (0 < sp["warning_ratio"] < sp["throttle_ratio"] < sp["stop_ratio"] <= 1.0):
        raise ValueError("spool 使用率分级须满足 0 < warning < throttle < stop <= 1")
    net = cfg["network"]
    if net["enabled"] and not (net.get("url") or "").startswith(("ws://", "wss://")):
        raise ValueError("network.enabled 时 url 必须以 ws:// 或 wss:// 开头 (P2-13c/d)")
    return cfg


def capture_config_hash(cfg):
    """§4.1: 采集配置指纹 (camera+capture+chunk), 写 session.json 供服务器校验."""
    subset = {k: cfg[k] for k in ("camera", "capture", "chunk")}
    raw = json.dumps(subset, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _merge(base, user):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
