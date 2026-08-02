# -*- coding: utf-8 -*-
"""P2-15c MCAP 写入 (规格 §18 ROS 2 / MCAP 表达).

Topic 约定 (规格 §18):
    /hp60c/depth_registered/image_raw     sensor_msgs/Image  16UC1  (原始 depth, 永不修改)
    /hp60c/depth_registered/confidence    sensor_msgs/Image  mono8
    /hp60c/depth_registered/confidence_reason  sensor_msgs/Image  mono8
    /hp60c/depth_registered/camera_info   sensor_msgs/CameraInfo

profile="ros2", message_encoding="cdr" (ROS 2 little-endian CDR), 可由
mcap / rosbag2 直接回放. 每次任务写 metadata 审计 (规格 §18):
algorithm_id / software_version / config_sha256 / input_session_sha256.
"""
import hashlib
import struct

from mcap.writer import Writer

IMAGE_MSGDEF = """\
std_msgs/Header header
uint32 height
uint32 width
string encoding
uint8 is_bigendian
uint32 step
uint8[] data
"""

CAMERA_INFO_MSGDEF = """\
std_msgs/Header header
uint32 height
uint32 width
string distortion_model
float64[] d
float64[9] k
float64[9] r
float64[12] p
uint32 binning_x
uint32 binning_y
sensor_msgs/RegionOfInterest roi
"""

# 实验 C 标定内参 (report_expC.md §6): 精确点云应用模型
CALIBRATION = {
    "fx": 586.11, "fy": 589.18, "cx": 330.83, "cy": 233.56,
    "k1": -0.025, "k2": 0.529, "k3": -1.372,
}


class _CDR:
    """ROS 2 little-endian CDR 序列化器 (基础类型 + 嵌套结构体按字段顺序)."""

    def __init__(self):
        self._parts = []

    def i32(self, v):
        self._parts.append(struct.pack("<i", v))

    def u32(self, v):
        self._parts.append(struct.pack("<I", v))

    def u8(self, v):
        self._parts.append(struct.pack("<B", v))

    def f64(self, v):
        self._parts.append(struct.pack("<d", v))

    def string(self, s):
        b = s.encode("utf-8")
        self.u32(len(b))
        self._parts.append(b)
        self._parts.append(b"\x00")           # CDR string 尾随 NUL

    def u8_array(self, data):
        self.u32(len(data))
        self._parts.append(bytes(data))

    def f64_array(self, values):
        self.u32(len(values))
        self._parts.append(struct.pack("<%dd" % len(values), *values))

    def bytes(self):
        return b"".join(self._parts)


def header_cdr(stamp_sec, stamp_nsec, frame_id=""):
    c = _CDR()
    c.i32(stamp_sec)
    c.u32(stamp_nsec)
    c.string(frame_id)
    return c


def image_cdr(height, width, encoding, data, step=None):
    """sensor_msgs/Image CDR payload."""
    c = header_cdr(0, 0)
    c.u32(height)
    c.u32(width)
    c.string(encoding)
    c.u8(0)                                   # is_bigendian
    c.u32(step if step is not None else width * 2 if encoding == "16UC1" else width)
    c.u8_array(data)
    return c.bytes()


def camera_info_cdr(cal=None, width=640, height=480):
    """sensor_msgs/CameraInfo CDR payload (实验 C 标定模型)."""
    cal = cal or CALIBRATION
    c = header_cdr(0, 0)
    c.u32(height)
    c.u32(width)
    c.string("plumb_bob")
    c.f64_array([cal["k1"], cal["k2"], 0.0, 0.0, cal["k3"]])
    k = [cal["fx"], 0.0, cal["cx"],
         0.0, cal["fy"], cal["cy"],
         0.0, 0.0, 1.0]
    c.f64_array(k)
    r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    c.f64_array(r)
    p = [cal["fx"], 0.0, cal["cx"], 0.0,
         0.0, cal["fy"], cal["cy"], 0.0,
         0.0, 0.0, 1.0, 0.0]
    c.f64_array(p)
    c.u32(0)                                  # binning_x
    c.u32(0)                                  # binning_y
    c.u32(0)                                  # roi.x_offset
    c.u32(0)                                  # roi.y_offset
    c.u32(0)                                  # roi.height
    c.u32(0)                                  # roi.width
    c.u8(0)                                   # roi.do_rectify
    return c.bytes()


def sha256_hex(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_confidence_mcap(out_path, frames, cal=None, metadata=None):
    """写 MCAP. frames: iterable of dicts 含:
    frame_id, utc_ms, depth_u16(np.ndarray <u2), confidence_u8, reason_u8.
    metadata: dict 审计字段 (algorithm_id/software_version/config_sha256/
    input_session_sha256/output_sha256 除外, 见返回说明).
    返回 dict: mcap_sha256 + 各 channel 消息数.
    """
    with open(out_path, "wb") as f:
        writer = Writer(f)
        writer.start(profile="ros2", library="capagent")
        schema_image = writer.register_schema(
            name="sensor_msgs/msg/Image", encoding="ros1msg",
            data=IMAGE_MSGDEF.encode("utf-8"))
        schema_cam = writer.register_schema(
            name="sensor_msgs/msg/CameraInfo", encoding="ros1msg",
            data=CAMERA_INFO_MSGDEF.encode("utf-8"))
        ch_depth = writer.register_channel(
            topic="/hp60c/depth_registered/image_raw",
            message_encoding="cdr", schema_id=schema_image)
        ch_conf = writer.register_channel(
            topic="/hp60c/depth_registered/confidence",
            message_encoding="cdr", schema_id=schema_image)
        ch_reason = writer.register_channel(
            topic="/hp60c/depth_registered/confidence_reason",
            message_encoding="cdr", schema_id=schema_image)
        ch_cam = writer.register_channel(
            topic="/hp60c/depth_registered/camera_info",
            message_encoding="cdr", schema_id=schema_cam)
        counts = {"depth": 0, "confidence": 0, "reason": 0, "camera_info": 0}
        cam_cdr = camera_info_cdr(cal)
        writer.add_message(channel_id=ch_cam, log_time=0,
                           data=cam_cdr, publish_time=0)
        counts["camera_info"] = 1
        for frame in frames:
            log_time = int(frame["utc_ms"]) * 1_000_000   # ms -> ns
            depth = frame["depth_u16"]
            height, width = depth.shape
            writer.add_message(
                channel_id=ch_depth, log_time=log_time,
                data=image_cdr(height, width, "16UC1",
                               depth.astype("<u2").tobytes(), step=width * 2),
                publish_time=log_time)
            writer.add_message(
                channel_id=ch_conf, log_time=log_time,
                data=image_cdr(height, width, "mono8",
                               frame["confidence_u8"].tobytes()),
                publish_time=log_time)
            writer.add_message(
                channel_id=ch_reason, log_time=log_time,
                data=image_cdr(height, width, "mono8",
                               frame["reason_u8"].tobytes()),
                publish_time=log_time)
            counts["depth"] += 1
            counts["confidence"] += 1
            counts["reason"] += 1
        if metadata:
            writer.add_metadata(name="confidence_task", data=dict(metadata))
        writer.finish()
    with open(out_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {"mcap_sha256": "sha256:" + digest, "counts": counts,
            "channels": ["/hp60c/depth_registered/image_raw",
                         "/hp60c/depth_registered/confidence",
                         "/hp60c/depth_registered/confidence_reason",
                         "/hp60c/depth_registered/camera_info"]}
