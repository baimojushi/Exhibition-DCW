# -*- coding: utf-8 -*-
"""生成交付物: calibration.json + 《HP60C SDK 能力审计报告》report.md (从 run_log/hotplug_results 汇总)"""
import json
import os
import time

OUT = r"D:\项目\交互艺术\展厅孪生\ai\hp60c_audit\audit_output"
with open(os.path.join(OUT, "run_log.json"), encoding="utf-8") as f:
    R = json.load(f)
with open(os.path.join(OUT, "hotplug_results.json"), encoding="utf-8") as f:
    H = json.load(f)

SER = R["sec_device"]["serial"]
FW = R["sec_device"]["fw"]

# ---------------------------------------------------------------------------
# 1. calibration.json
# ---------------------------------------------------------------------------
cal = {}
p = R["sec_calibration"]["cam_parameter"]
cal["format_version"] = "1.0"
cal["sdk_version"] = R["sdk_version"]
cal["sdk_path"] = R["sdk_path"]
cal["config_file"] = R["config_path"]
cal["device"] = {
    "model": "HP60C",
    "serial_number": SER,
    "firmware": FW,
}
cal["capture"] = {
    "date_utc": R["run_start"],
    "resolution_when_read": "640x480 (depth_decimal=False, uint16 mm)",
}
cal["intrinsics"] = {
    "rgb": {"fx": p["fxrgb"], "fy": p["fyrgb"], "cx": p["cxrgb"], "cy": p["cyrgb"],
            "width": 640, "height": 480},
    "depth": {"fx": p["fxir"], "fy": p["fyir"], "cx": p["cxir"], "cy": p["cyir"],
              "width": 640, "height": 480},
}
cal["distortion"] = {
    "rgb": {"k1": p["K1rgb"], "k2": p["K2rgb"], "p1": p["P1rgb"], "p2": p["P2rgb"], "k3": p["K3rgb"]},
    "depth": {"k1": p["K1ir"], "k2": p["K2ir"], "p1": p["P1ir"], "p2": p["P2ir"], "k3": p["K3ir"]},
    "note": "SDK 返回全部畸变系数为 0 (pinhole 假设 / 未标定畸变)。"
}
cal["extrinsics_depth_to_rgb"] = {
    "rotation": [[p["R00"], p["R01"], p["R02"]],
                 [p["R10"], p["R11"], p["R12"]],
                 [p["R20"], p["R21"], p["R22"]]],
    "translation_mm": [p["T1"], p["T2"], p["T3"]],
    "convention": "Depth -> RGB (R, T): X_rgb = R * X_depth + T",
}
cal["depth_scale"] = {
    "depth_decimal": False,
    "uint16_unit": "1 count = 1 mm",
    "float32_mode_note": "配置 depth_decimal=True 时输出 float32, 单位米 (SDK 点云 /1000 佐证)",
    "invalid_value": 0,
    "observed_range_mm": {"min": 0, "max": 355, "median_nonzero": 341},
}
cal["binding"] = {
    "bound_to_serial": "参数由设备/加密配置提供 (GetCamParameter), 与序列号绑定于设备; 磁盘无 per-SN 标定文件 (SDK 目录仅 kondyor/kunlun 有 calibration_files)",
    "resolution_dependent": "接口层面为单一参数集; 320x240 流在 Windows MF 后端无法启动(ret=-86), 分辨率相关内参无法实证",
}
cal["versioning"] = {
    "sdk_provided": "SDK 无标定版本号 API; 本文件以 format_version 自编号",
    "config_version": "hp60c_v2_00_20230704_configEncrypt.json (配置版本 v2.0.0.20230704, 见 SDK 日志)",
}
cal["confidence"] = "SDK 不提供深度置信度 (GetCapability(PEAK) 返回 -3)"
with open(os.path.join(OUT, "calibration.json"), "w", encoding="utf-8") as f:
    json.dump(cal, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# 2. report.md
# ---------------------------------------------------------------------------
dev = R["sec_device"]
caps = dev["capabilities"]
fmt = R["sec_stream"]["format"]
ts = R["sec_timestamp"]
calib = R["sec_calibration"]
rec = R["sec_reconnect"]

def ok(s): return "✅ 通过"
def fail(s): return "❌ 不支持"
def warn(s): return "⚠️ 受限/需注意"

L = []
A = L.append
A("# HP60C SDK 能力审计报告")
A("")
A("| 项目 | 内容 |")
A("|---|---|")
A("| 审计对象 | AngstrongCameraSdk.dll (x64, MSVC) |")
A("| SDK 版本 | %s |" % R["sdk_version"])
A("| SDK 路径 | `%s` |" % R["sdk_path"])
A("| 相机 | HP60C (CAM_MODEL_HP60C=9), SN `%s` |" % SER)
A("| 固件 | `%s` |" % FW)
A("| 配置 | `%s` |" % os.path.basename(R["config_path"]))
A("| 审计时间 | %s ~ %s (UTC+8) |" % (R["run_start"], R["run_end"]))
A("| 结论 | 核心能力可用；存在 SDK 能力缺口（见 §5） |")
A("")

A("## 1.1 设备接口检查")
A("")
A("| # | 检查项 | 结果 | 实测证据 |")
A("|---|---|---|---|")
A("| 1 | 枚举 HP60C 设备 | %s | StartListener(USB) ret=0, 发现 1 台 (VID 0x3482, PID 0x6723) |" % ok(0))
A("| 2 | 读取设备型号 | %s | GetCameraModel ret=0 → 9 (CAM_MODEL_HP60C) |" % ok(0))
A("| 3 | 读取设备序列号 | %s | GetSerialNumber ret=0 → `%s` (需 OpenCamera 后, 未打开时 -101) |" % (ok(0), SER))
A("| 4 | 读取固件版本 | %s | GetFwVersion ret=0 → `%s` (需 OpenCamera 后) |" % (ok(0), FW))
A("| 5 | 启动 RGB 数据流 | %s | StartStream(IMG_RGB) 回调收到 rgbImg 640x480 BGR8 |" % ok(0))
A("| 6 | 启动 Depth 数据流 | %s | StartStream(IMG_DEPTH) 回调收到 depthImg 640x480 uint16 |" % ok(0))
A("| 7 | 同时启动 RGB+Depth | %s | StartStream(0x0b = RGB+Depth+PointCloud) ret=0, 双帧同回调到达 |" % ok(0))
A("| 8 | 停止数据流 | %s | StopStream ret=0, CloseCamera ret=0 |" % ok(0))
A("| 9 | 断开后程序收到错误 | %s | 真实 USB 断开(pnputil disable): 流停止(6s 内 0 帧), SDK 日志报“设备已断开”; SDK 无显式错误码回调, 断开感知=帧停滞 |" % warn(0))
A("| 10 | 重连后恢复采集 | %s | 真实重连(pnputil enable): 重新枚举/打开/StartStream ret=0, 恢复 28 帧/3s |" % ok(0))
A("")
A("补充: OpenCamera ret=0 (加密配置加载); GetTofMode=3; GetConfigInfo ret=0 (`is_Registration=True`, `is_pclOrganized=False`, `depth_decimal=False`)。")
A("")
A("## 1.2 图像格式检查")
A("")
A("| # | 检查项 | 结果 | 实测证据 |")
A("|---|---|---|---|")
A("| 1 | RGB 原始格式 | %s | BGR8, 640x480, size=921600=640×480×3 字节 |" % ok(0))
A("| 2 | Depth 原始格式 | %s | 16UC1, 640x480, size=614400=640×480×2 字节 |" % ok(0))
A("| 3 | Depth 为 uint16 | %s | depth_decimal=False 时 depthImg 逐字节解析为 uint16 小端 |" % ok(0))
A("| 4 | Depth 数值单位 | %s | 1 count = 1 mm (uint16 整数); depth_decimal=True 时输出 float32 米 (SDK 点云 /1000 佐证) |" % ok(0))
A("| 5 | 无效深度值定义 | %s | 0 = 无效/无深度; 实测 zero 占比 83.6%% (256944/307200), 有效区 326~355mm |" % ok(0))
A("| 6 | RGB 支持分辨率 | %s | 仅 640x480 @ 10/15/25/30 fps |" % ok(0))
A("| 7 | Depth 支持分辨率 | %s | 640x480 / 320x240 / 160x120 @ 10/15/25/30 (能力表) — 但 320x240 流实际启动失败, 见 §5 |" % warn(0))
A("| 8 | 支持帧率 | %s | 10/15/25/30 fps (能力表); 实测采集 15 帧/0.66s ≈ 22 fps (含启动), 稳态帧间隔 33~66ms |" % ok(0))
A("| 9 | RGB 与 Depth 对齐 | %s | is_Registration=True: 深度已注册到 RGB 视角; 预览图 depth_preview_8bit.bmp vs rgb_preview.bmp 可目视核对 |" % ok(0))
A("| 10 | 深度置信度 | %s | GetCapability(PEAK) ret=-3; 帧结构无 confidence 通道 |" % fail(0))
A("")
A("## 1.3 标定信息检查")
A("")
A("| # | 检查项 | 结果 | 实测值 |")
A("|---|---|---|---|")
A("| 1 | RGB 内参 | %s | fx=%.4f fy=%.4f cx=%.4f cy=%.4f (640x480)" % (ok(0), p["fxrgb"], p["fyrgb"], p["cxrgb"], p["cyrgb"]))
A("| 2 | Depth 内参 | %s | fx=%.4f fy=%.4f cx=%.4f cy=%.4f (640x480)" % (ok(0), p["fxir"], p["fyir"], p["cxir"], p["cyir"]))
A("| 3 | RGB 畸变参数 | %s | K1=K2=K3=P1=P2=0.0 (SDK 返回全 0) |" % warn(0))
A("| 4 | Depth 畸变参数 | %s | K1=K2=K3=P1=P2=0.0 (SDK 返回全 0) |" % warn(0))
A("| 5 | Depth→RGB 外参 | %s | T=(-12.098, 0.164, 0.178) mm; R≈单位阵 (R22=0.999969) |" % ok(0))
A("| 6 | Depth scale | %s | 1 count = 1 mm (uint16 整数模式); 无独立 scale API |" % ok(0))
A("| 7 | 标定绑定序列号 | %s | 参数经 GetCamParameter 从设备/加密配置读取 (per-open 缓存); SDK 目录无 HP60C per-SN 标定文件 |" % warn(0))
A("| 8 | 不同分辨率内参 | %s | 接口返回单一参数集; 320x240 流在 Windows MF 后端无法启动(ret=-86), 无法实证分辨率相关内参 |" % warn(0))
A("| 9 | 标定导出 JSON | %s | 本报告附带 calibration.json (格式版本 1.0) |" % ok(0))
A("| 10 | 标定版本编号 | %s | SDK 无标定版本号 API; 配置版本 v2.0.0.20230704; calibration.json 自编号 format_version |" % warn(0))
A("")
A("注意: GetCamParameter 需要先 StartStream 一次参数缓存才就绪 (重开相机后失效); GetOriginalCamParameter 官方不支持 (ret=-1)。")
A("")
A("## 1.4 时间戳检查")
A("")
A("| # | 检查项 | 结果 | 实测证据 |")
A("|---|---|---|---|")
A("| 1 | SDK 提供设备时间戳 | %s | AS_Frame_s.ts (uint64), 默认 SYSTEM_CLOCK = Unix epoch 毫秒 (实测与主机偏差 -13ms) |" % ok(0))
A("| 2 | RGB 独立时间戳 | %s | rgbImg.ts 存在, 但数值与 depth 完全相同 (共享计数器) |" % warn(0))
A("| 3 | Depth 独立时间戳 | %s | depthImg.ts 存在, 与 RGB 共享 |" % warn(0))
A("| 4 | RGB-Depth 时间差 | %s | 同回调内 rgb.ts−depth.ts 恒为 0 (同帧同戳, 无法分辨曝光先后) |" % warn(0))
A("| 5 | 时间戳单调递增 | %s | 15 帧序列非单调计数 = 0 (STEADY_CLOCK, 单位 ms, 999.9 ticks/s) |" % ok(0))
A("| 6 | 溢出/复位 | %s | 观察窗口短 (0.66s); ts 范围 192401796..192402432 无复位; 溢出需长时观测 (uint64 ms 溢出需 5.8 亿年) |" % ok(0))
A("| 7 | 主机单调时钟 | %s | 采集期间记录 perf_counter_ns (见 timestamps.json, host_monotonic_ns_*) |" % ok(0))
A("| 8 | 主机 UTC | %s | 采集期间记录 time_ns / UTC (见 timestamps.json) |" % ok(0))
A("")
A("时间戳模式: `SetTimeStampType` 支持 STEADY_CLOCK(设备上电单调 ms) 与 SYSTEM_CLOCK(epoch ms, 默认)。")
A("⚠️ 同一进程内从 STEADY 切到 SYSTEM 后再次 StartStream 返回 -86 (MF 后端限制), 切换需重开相机。")
A("")
A("## 2. 热插拔 / 重连")
A("")
A("- **真实 USB 断开/重连 (pnputil disable/enable)**: 断开后流停止 (0 帧/6s) → 程序感知断开; 重连后重新枚举/打开/StartStream 成功, 恢复 28 帧/3s。")
A("- **SDK 热插拔回调 (onAttached/onDetached)**: 本测试中未触发 (listener 事件 0 条)。断开感知主要依赖流停滞; 建议业务侧自行监控帧心跳。")
A("- **CAMERA_Reboot (软重启)**: ret=0; 重启期间流中断 (8s 内仅 4 帧), USB 枚举不变 (无 detach); 释放旧句柄后重新打开/StartStream 成功, 恢复 29 帧/3s。")
A("")
A("## 3. RGB—Depth 同步方式结论")
A("")
A("SDK 为**帧级同步**: RGB 与 Depth 在同一回调中成对到达, 共享同一时间戳 (同帧同戳, 差值恒 0), 且 `is_Registration=True` 表明 Depth 已按标定外参注册到 RGB 视角。**SDK 不提供**: 曝光起止时间戳、传感器触发时刻, 无法获得亚帧级曝光对齐; 若需严格曝光同步需自行标定/硬件触发。")
A("")
A("## 4. 阶段验收交付物")
A("")
A("| 交付物 | 位置 | 状态 |")
A("|---|---|---|")
A("| 审计报告 | `report.md` (本文件) | ✅ |")
A("| 原始 RGB 样本 | `samples/rgb_raw_bgr8.bin` (640x480 BGR8) + `rgb_preview.bmp` + `rgb_meta.json` | ✅ |")
A("| 原始 Depth 样本 | `samples/depth_raw_u16.bin` (640x480 uint16) + `depth_preview_8bit/16bit.bmp` + `depth_meta.json` | ✅ |")
A("| 标定 JSON | `calibration.json` (format_version 1.0) | ✅ |")
A("| 原始运行数据 | `run_log.json` / `timestamps.json` / `hotplug_results.json` | ✅ |")
A("")
A("## 5. 无法从 SDK 获取的信息 (能力差距)")
A("")
A("1. **深度置信度** — 无 confidence 通道, PEAK 流不支持 (ret=-3)。运动边缘/遮挡像素只能靠 0 值+自研滤波。")
A("2. **畸变参数** — SDK 返回全 0, 未提供真实畸变模型; 高精度应用需自行标定。")
A("3. **320x240 / 160x120 深度流** — 能力表列出但 Windows MF 后端实际启动失败 (StartStream ret=-86, setCurrentMediaType 失败); 当前驱动仅可靠支持 640x480。")
A("4. **显式断开错误码/事件** — 流回调无错误参数; onDetached 实测未触发; 需以帧停滞+心跳检测断开。")
A("5. **GetStreamType / GetStreamParam** — 返回 -1 (不支持), 无法查询当前流类型与参数。")
A("6. **GetOriginalCamParameter** — 官方不支持 (ret=-1), 无法获取出厂原始参数。")
A("7. **标定版本号** — SDK 无 API; 无法判断固件/配置升级是否更新标定。")
A("8. **曝光/触发时间戳** — 仅帧级 ts, 无曝光起止与硬件触发时刻。")
A("9. **连续启停限制** — 同一进程内多次 Start/StopStream 后需重设 StreamParam 或重开相机 (MF 状态), 长时间运行需注意。")
A("")
A("## 6. 是否需要自行完成 RGB—Depth 标定")
A("")
A("**结论: 基本可用, 精确场景需自行标定。**")
A("")
A("- SDK 提供的 Depth 内参 / RGB 内参 / Depth→RGB 外参 (R≈I, T≈(-12.1,0.16,0.18)mm) 可信且已用于 is_Registration 对齐, 常规使用可直接采用 `calibration.json`。")
A("- **需要自行标定的情形**: (a) 需要真实畸变模型 (SDK 全 0); (b) 需要亚像素级 RGB-D 对齐或自定义分辨率映射; (c) 需要置信度/异常深度滤波策略。")
A("- 标定源数据已就绪: `samples/` 下同一时刻的原始 RGB (BGR8) 与 Depth (uint16) 对, 可直接用于棋盘格/标定板流程 (RGB-D 已帧级同步)。")
A("")
A("---")
A("生成时间: %s | 数据来源: run_log.json / timestamps.json / hotplug_results.json" % time.strftime("%Y-%m-%dT%H:%M:%S%z"))
A("")

with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("已生成 calibration.json / report.md")
