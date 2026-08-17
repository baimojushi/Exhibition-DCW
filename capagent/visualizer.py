# -*- coding: utf-8 -*-
"""实时点云可视化 + XYZ 倾斜角提示 (tkinter + PIL 图像渲染, 无外部依赖).

在独立线程运行, 从最新帧引用读取 (非阻塞 SD K 回调线程).
点云使用 PIL/Numpy 渲染为图像, 避免 tkinter 逐个绘制.
"""
import math
import threading
import tkinter as tk
from tkinter import font as tkfont

import numpy as np

# PIL 可选, 无 PIL 时降级为 tkinter 散点
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

FX, FY = 361.376, 361.376
CX, CY = 318.517, 179.502
ZMIN, ZMAX = 200, 4000
MAX_PTS = 2000
GOOD_ANGLE = 5.0


class LiveVisualizer:
    def __init__(self, fx=FX, fy=FY, cx=CX, cy=CY, interval_ms=300):
        self.fx, self.fy, self.cx, self.cy = fx, fy, cx, cy
        self.interval = interval_ms
        self.latest_frame = None
        self.lock = threading.Lock()
        self.running = False
        self._thread = None
        self._pc_w, self._pc_h = 600, 460

    def update_frame(self, depth, rgb):
        with self.lock:
            if isinstance(depth, bytes) and len(depth) < 100:
                return
            self.latest_frame = (depth if isinstance(depth, bytes) else depth.copy(),
                                 rgb if isinstance(rgb, bytes) else rgb.copy())

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._run, name="visualizer", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        root = tk.Tk()
        root.title("HP60C Live - Point Cloud | RGB | Tilt")
        root.geometry("1480x520")
        root.configure(bg="#1a1a2e")

        bold_font = tkfont.Font(family="Consolas", size=11, weight="bold")
        title_font = tkfont.Font(family="Consolas", size=14, weight="bold")
        num_font = tkfont.Font(family="Consolas", size=22, weight="bold")

        # 左: 点云
        pc_frame = tk.Frame(root, bg="#16213e", width=500, height=480)
        pc_frame.pack(side=tk.LEFT, padx=5, pady=5)
        pc_frame.pack_propagate(False)
        tk.Label(pc_frame, text="Point Cloud (俯视)", bg="#16213e",
                 fg="#e0e0e0", font=bold_font).pack()
        pc_display = tk.Label(pc_frame, bg="#16213e")
        pc_display.pack()

        # 中: RGB
        rgb_frame = tk.Frame(root, bg="#0a0a1a", width=440, height=480)
        rgb_frame.pack(side=tk.LEFT, padx=5, pady=5)
        rgb_frame.pack_propagate(False)
        tk.Label(rgb_frame, text="RGB Camera", bg="#0a0a1a",
                 fg="#e0e0e0", font=bold_font).pack()
        rgb_display = tk.Label(rgb_frame, bg="#0a0a1a")
        rgb_display.pack()

        # 右: 倾斜角面板
        tilt_frame = tk.Frame(root, bg="#1a1a2e", width=480, height=480)
        tilt_frame.pack(side=tk.LEFT, padx=5, pady=5, fill=tk.BOTH, expand=True)

        status_label = tk.Label(tilt_frame, text="HP60C Live Capture",
                                bg="#1a1a2e", fg="#e0e0e0", font=title_font)
        status_label.pack(pady=10)

        pitch_val = tk.Label(tilt_frame, text="+0.0°", bg="#1a1a2e",
                             fg="#2ecc71", font=num_font)
        pitch_val.pack()
        tk.Label(tilt_frame, text="Pitch (up/down)", bg="#1a1a2e",
                 fg="#aaaaaa", font=bold_font).pack()

        yaw_val = tk.Label(tilt_frame, text="+0.0°", bg="#1a1a2e",
                           fg="#2ecc71", font=num_font)
        yaw_val.pack()
        tk.Label(tilt_frame, text="Yaw (left/right)", bg="#1a1a2e",
                 fg="#aaaaaa", font=bold_font).pack()

        roll_val = tk.Label(tilt_frame, text="+0.0°", bg="#1a1a2e",
                            fg="#2ecc71", font=num_font)
        roll_val.pack()
        tk.Label(tilt_frame, text="Roll (twist)", bg="#1a1a2e",
                 fg="#aaaaaa", font=bold_font).pack()

        indicator = tk.Label(tilt_frame, text="", bg="#1a1a2e",
                             fg="#2ecc71", font=tkfont.Font(family="Consolas", size=32, weight="bold"))
        indicator.pack(pady=15)

        bar_frame = tk.Frame(tilt_frame, bg="#1a1a2e", height=40)
        bar_frame.pack(fill=tk.X, padx=20, pady=5)
        bar_canvas = tk.Canvas(bar_frame, bg="#1a1a2e", height=30, highlightthickness=0)
        bar_canvas.pack(fill=tk.X)

        self._tk_refs = (root, pc_display, rgb_display, pitch_val, yaw_val, roll_val,
                         indicator, bar_canvas)
        self._tk_image = None
        self._tk_rgb = None
        root.after(100, self._update, root)
        root.mainloop()
        self.running = False

    def _update(self, root):
        if not self.running:
            root.destroy()
            return

        frame = None
        with self.lock:
            if self.latest_frame is not None:
                frame = self.latest_frame

        if frame is not None:
            depth, rgb = frame
            pts, angles = self._process(depth)
            _, pc_display, rgb_display, pitch_val, yaw_val, roll_val, indicator, bar_canvas = self._tk_refs

            # 点云渲染
            if HAS_PIL and pts is not None:
                img = self._render_pil(pts)
                new_img = ImageTk.PhotoImage(img)
                pc_display.config(image=new_img)
                self._tk_image = new_img  # 保持引用防 GC
            elif HAS_PIL:
                pc_display.config(image="")
                self._tk_image = None

            # RGB 渲染
            if HAS_PIL and isinstance(rgb, bytes) and len(rgb) >= 921600:
                arr = np.frombuffer(rgb, dtype=np.uint8).reshape((480, 640, 3))
                # 保持原始方向 (不做镜像)
                rgb_img = Image.fromarray(arr).resize((420, 315), Image.NEAREST)
                new_rgb = ImageTk.PhotoImage(rgb_img)
                rgb_display.config(image=new_rgb)
                self._tk_rgb = new_rgb

            # 倾斜角
            if angles is not None:
                pitch, yaw, roll = angles
                p_good = abs(pitch) < GOOD_ANGLE
                y_good = abs(yaw) < GOOD_ANGLE
                r_good = abs(roll) < GOOD_ANGLE

                pitch_val.config(text=f"{pitch:+.1f}°", fg="#2ecc71" if p_good else "#e74c3c")
                yaw_val.config(text=f"{yaw:+.1f}°", fg="#2ecc71" if y_good else "#e74c3c")
                roll_val.config(text=f"{roll:+.1f}°", fg="#2ecc71" if r_good else "#e74c3c")
                indicator.config(text="● GOOD" if (p_good and y_good) else "● ADJUST",
                                 fg="#2ecc71" if (p_good and y_good) else "#e74c3c")

                bar_canvas.delete("all")
                bw, bh = 440, 30
                for i, (name, v) in enumerate([("Pitch", pitch), ("Yaw", yaw), ("Roll", roll)]):
                    y0 = i * 10
                    bar_canvas.create_rectangle(60, y0, 60 + bw, y0 + 9, fill="#333355", outline="")
                    cx_b = 60 + bw // 2
                    bar_canvas.create_line(cx_b, y0, cx_b, y0 + 9, fill="#666666")
                    th_px = int(bw * GOOD_ANGLE / 30)
                    bar_canvas.create_rectangle(cx_b - th_px, y0, cx_b + th_px, y0 + 9,
                                                fill="#1a4a1a", outline="")
                    vx = int(cx_b + v / 30 * bw)
                    vx = max(60, min(60 + bw, vx))
                    color = "#2ecc71" if abs(v) < GOOD_ANGLE else "#e74c3c"
                    bar_canvas.create_line(vx, y0, vx, y0 + 9, fill=color, width=3)
                    bar_canvas.create_text(30, y0 + 5, text=name, fill="#aaaaaa",
                                           font=("Consolas", 8), anchor="e")

        root.after(self.interval, self._update, root)

    def _render_pil(self, pts):
        """PIL 渲染点云: 正面投影 (X=水平, Y=垂直高度), 深度(Z)颜色映射."""
        w, h = self._pc_w, self._pc_h
        px, py, pz = pts[:, 0], pts[:, 1], pts[:, 2]

        x_min, x_max = -900, 900
        y_min, y_max = -700, 700
        sx = np.clip(((px - x_min) / (x_max - x_min) * w * 0.9 + w * 0.05).astype(int), 0, w - 1)
        sy = np.clip(((py - y_min) / (y_max - y_min) * h * 0.9 + h * 0.05).astype(int), 0, h - 1)

        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[:, :] = [0x16, 0x21, 0x3e]

        z_norm = np.clip(pz / 2000.0, 0, 1)
        r = (z_norm * 255).astype(np.uint8)
        g = ((1 - np.abs(z_norm - 0.5) * 2) * 200).astype(np.uint8)
        b = ((1 - z_norm) * 255).astype(np.uint8)
        arr[sy, sx] = np.stack([r, g, b], axis=1)

        return Image.fromarray(arr)

    def _process(self, depth):
        if isinstance(depth, bytes):
            if len(depth) == 640 * 360 * 2:
                depth = np.frombuffer(depth, dtype=np.uint16).reshape((360, 640))
            elif len(depth) == 640 * 480 * 2:
                depth = np.frombuffer(depth, dtype=np.uint16).reshape((480, 640))
            else:
                return None, None
        mask = (depth > ZMIN) & (depth < ZMAX)
        ys, xs = np.nonzero(mask)
        if len(ys) < 200:
            return None, None

        z = depth[ys, xs].astype(np.float64)
        x = (xs - self.cx) / self.fx * z
        y = (ys - self.cy) / self.fy * z
        pts_all = np.stack([x, y, z], axis=1)

        if len(pts_all) > MAX_PTS:
            idx = np.random.default_rng().choice(len(pts_all), MAX_PTS, replace=False)
            pts = pts_all[idx]
        else:
            pts = pts_all

        c = pts.mean(axis=0)
        _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
        normal = vt[-1]
        if normal[2] > 0:
            normal = -normal

        pitch = math.degrees(math.atan2(normal[1], abs(normal[2])))
        yaw = math.degrees(math.atan2(normal[0], abs(normal[2])))
        roll = math.degrees(math.atan2(normal[0], normal[1])) if abs(normal[1]) > 1e-6 else 0.0

        return pts, (pitch, yaw, roll)