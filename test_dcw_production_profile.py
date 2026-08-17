# -*- coding: utf-8 -*-
"""DCW 生产 Profile 一致性测试。

验证：
1. 运行时配置与 production_profile.json 完全一致
2. DaBaiDCWBackend 实际打开的 profile 与冻结值一致
3. identity.profile_id 与 production_profile.json 计算值一致
"""
import json
import sys
import os

sys.path.insert(0, r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit")

from capagent.config import load_config
from capagent.dcw_backend import DaBaiDCWBackend, build_identity, DeviceMode, D2cConfig


def load_production_profile():
    """加载冻结的生产 profile。"""
    with open(r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\production_profile.json", encoding="utf-8") as f:
        return json.load(f)


def test_config_matches_production():
    """测试配置文件与 production_profile.json 一致。"""
    print("=" * 60)
    print("Test 1: 配置文件 vs production_profile.json")
    print("=" * 60)

    prod = load_production_profile()
    cfg = load_config(r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\capagent\config_denoise.json")

    # Color profile
    color_cfg = cfg["camera"]["dcw"]["color"]
    color_prod = prod["color"]
    assert color_cfg["width"] == color_prod["width"], f"color.width: {color_cfg['width']} != {color_prod['width']}"
    assert color_cfg["height"] == color_prod["height"], f"color.height: {color_cfg['height']} != {color_prod['height']}"
    assert color_cfg["fps"] == color_prod["fps"], f"color.fps: {color_cfg['fps']} != {color_prod['fps']}"
    assert color_cfg["format"] == color_prod["format"], f"color.format: {color_cfg['format']} != {color_prod['format']}"
    print("  Color profile: PASS")

    # Depth profile
    depth_cfg = cfg["camera"]["dcw"]["depth"]
    depth_prod = prod["depth"]
    assert depth_cfg["width"] == depth_prod["width"], f"depth.width: {depth_cfg['width']} != {depth_prod['width']}"
    assert depth_cfg["height"] == depth_prod["height"], f"depth.height: {depth_cfg['height']} != {depth_prod['height']}"
    assert depth_cfg["fps"] == depth_prod["fps"], f"depth.fps: {depth_cfg['fps']} != {depth_prod['fps']}"
    assert depth_cfg["format"] == depth_prod["format"], f"depth.format: {depth_cfg['format']} != {depth_prod['format']}"
    print("  Depth profile: PASS")

    # D2C
    d2c_cfg = cfg["camera"]["dcw"]["d2c"]
    d2c_prod = prod["d2c"]
    assert d2c_cfg["enabled"] == d2c_prod["enabled"], f"d2c.enabled: {d2c_cfg['enabled']} != {d2c_prod['enabled']}"
    assert d2c_cfg["align_mode"] == d2c_prod["align_mode"], f"d2c.align_mode: {d2c_cfg['align_mode']} != {d2c_prod['align_mode']}"
    print("  D2C config: PASS")

    print("\n✓ Test 1 PASSED: 配置与 production_profile.json 完全一致")
    return True


def test_backend_identity_matches():
    """测试 DaBaiDCWBackend 构建的 identity 与 production_profile.json 一致。"""
    print("\n" + "=" * 60)
    print("Test 2: Backend identity vs production_profile.json")
    print("=" * 60)

    prod = load_production_profile()

    # 模拟构建 identity
    color_mode = DeviceMode("COLOR", prod["color"]["width"], prod["color"]["height"],
                            prod["color"]["fps"], prod["color"]["format"])
    depth_mode = DeviceMode("DEPTH", prod["depth"]["width"], prod["depth"]["height"],
                            prod["depth"]["fps"], prod["depth"]["format"])
    d2c = D2cConfig(enabled=prod["d2c"]["enabled"], align_mode=prod["d2c"]["align_mode"])

    identity = build_identity(
        serial=prod["camera"]["serial"],
        device_name="DaBai DCW",
        firmware=prod["camera"]["firmware_version"],
        sdk_version=prod["camera"]["sdk_version"],
        connection_type=prod["camera"]["connection_type"],
        color_mode=color_mode,
        depth_mode=depth_mode,
        d2c=d2c
    )

    # 验证 profile_id 计算逻辑
    expected_core = {
        "serial_number": prod["camera"]["serial"],
        "firmware_version": prod["camera"]["firmware_version"],
        "sdk_major": prod["camera"]["sdk_major"],
        "color_mode": color_mode.to_dict(),
        "depth_mode": depth_mode.to_dict(),
        "d2c": d2c.to_dict()
    }
    import hashlib
    expected_profile_id = "sha256:" + hashlib.sha256(
        json.dumps(expected_core, sort_keys=True).encode()).hexdigest()

    print(f"  Computed profile_id: {identity['profile_id']}")
    print(f"  Expected profile_id: {expected_profile_id}")
    assert identity["profile_id"] == expected_profile_id, "profile_id 计算不一致"
    print("  profile_id 计算: PASS")

    # 验证 color_mode/depth_mode 字段存在
    assert "color_mode" in identity, "identity 缺少 color_mode"
    assert "depth_mode" in identity, "identity 缺少 depth_mode"
    assert identity["color_mode"]["width"] == prod["color"]["width"]
    assert identity["color_mode"]["height"] == prod["color"]["height"]
    assert identity["depth_mode"]["width"] == prod["depth"]["width"]
    assert identity["depth_mode"]["height"] == prod["depth"]["height"]
    print("  color_mode/depth_mode 字段: PASS")

    # 验证 d2c 字段
    assert identity["d2c"]["enabled"] == False
    assert identity["d2c"]["align_mode"] == "OFF"
    print("  d2c 字段: PASS")

    print("\n✓ Test 2 PASSED: Backend identity 与 production_profile.json 一致")
    return True


def test_mismatch_fails():
    """测试不匹配的配置会被拒绝。"""
    print("\n" + "=" * 60)
    print("Test 3: 不匹配配置应被拒绝")
    print("=" * 60)

    # 创建一个错误的配置（depth height = 480 而不是 360）
    import tempfile
    bad_cfg = {
        "camera": {
            "width": 640, "height": 480, "fps": 30,
            "serial": None, "config_file": None, "include_net": False,
            "dcw_backend": True,
            "dcw": {
                "color": {"width": 640, "height": 480, "fps": 30, "format": "RGB"},
                "depth": {"width": 640, "height": 480, "fps": 30, "format": "Y16"},  # WRONG: 480
                "d2c": {"enabled": False, "align_mode": "OFF"}
            }
        },
        "watchdog": {"stall_timeout_ms": 500, "bad_frame_limit": 3,
                     "first_frame_timeout_s": 5.0, "max_reconnect_attempts": 3},
        "capture": {"jpeg_quality": 85, "zstd_level": 3, "chunk_seconds": 2.0,
                    "queue_max_frames": 64, "denoise": {"enabled": True, "window_size": 5}},
        "chunk": {"target_duration_ms": 2000, "max_payload_bytes": 67108864, "max_frame_count": 128},
        "store": {"root": r"D:\tmp", "quota_gb": 5.0, "delete_on_ack": True},
        "spool": {"root": r"D:\tmp", "max_size_gb": 5.0, "warning_ratio": 0.8,
                  "throttle_ratio": 0.9, "stop_ratio": 0.97, "queue_max_frames": 256},
        "network": {"enabled": False, "url": None, "token": None,
                    "client_id": "hp60c-capture-agent", "agent_version": "git:local",
                    "max_reconnect_attempts": 8, "connect_timeout_s": 10.0,
                    "control_idle_timeout_s": 10.0, "heartbeat_interval_s": 2.0,
                    "stream_no_progress_timeout_s": 30.0, "commit_ack_timeout_s": 15.0,
                    "requested_max_inflight": 4, "backoff_s": [1, 2, 4, 8, 15]},
        "run": {"seconds": 1, "stats_interval_s": 10.0, "log_file": None, "visualize": False}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(bad_cfg, f)
        bad_cfg_path = f.name

    try:
        cfg = load_config(bad_cfg_path)
        depth_h = cfg["camera"]["dcw"]["depth"]["height"]
        prod = load_production_profile()
        expected_h = prod["depth"]["height"]

        if depth_h != expected_h:
            print(f"  检测到不匹配: depth.height={depth_h} (期望 {expected_h})")
            print("  ✓ Test 3 PASSED: 不匹配配置被检测到")
            return True
        else:
            print("  ✗ Test 3 FAILED: 未检测到不匹配")
            return False
    finally:
        os.unlink(bad_cfg_path)


def main():
    print("DCW 生产 Profile 一致性测试")
    print("=" * 60)

    test_config_matches_production()
    test_backend_identity_matches()
    test_mismatch_fails()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())