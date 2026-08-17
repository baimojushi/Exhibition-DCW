# -*- coding: utf-8 -*-
"""HP60C 采集 + 实时可视化 (点云 + XYZ 倾斜角).

用法:
  python cap_viz.py [--seconds 60] [--config capagent/config.json]

可视化窗口:
  左: 深度点云 3D 散点图 (俯视视角)
  右: XYZ 倾斜角条状图 (Pitch/Yaw/Roll)
     绿色 = 正对墙面 (<5°)  红色 = 需要调整姿态

采集时尽量保持 Pitch/Yaw 在 ±5° 以内, 即可保证直对墙面.
"""
import argparse
import json
import sys

from capagent.agent import CaptureAgent
from capagent.config import load_config


def main():
    ap = argparse.ArgumentParser(description="HP60C 采集 + 实时可视化")
    ap.add_argument("--config", default=None, help="JSON 配置文件路径")
    ap.add_argument("--seconds", type=float, default=None, help="运行秒数 (默认无限)")
    ap.add_argument("--root", default=None, help="覆盖 spool.root")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.seconds is not None:
        cfg["run"]["seconds"] = args.seconds
    if args.root:
        cfg["spool"]["root"] = args.root

    # 强制开启可视化 + v1 SDK 后端 (DaBai DCW)
    cfg["run"]["visualize"] = True
    cfg["camera"]["dcw_backend"] = True

    print("=" * 50)
    print("HP60C Live Capture + Visualization")
    print("=" * 50)
    print("窗口: 点云 3D + XYZ 倾斜角")
    print("绿色 = 正对墙面 (<5°)  红色 = 需调整姿态")
    print("ESC 或在窗口关闭框可退出可视化 (采集继续)")
    print("Ctrl+C 停止采集")
    print("=" * 50)

    from capagent.agent import main as agent_main
    import logging
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
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()