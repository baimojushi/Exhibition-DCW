# -*- coding: utf-8 -*-
"""Temporal Median Denoising for HP60C Depth Frames.

Strategy:
  - Maintain a sliding window of recent depth frames (uint16 mm)
  - For each incoming frame, compute per-pixel median across the window
  - Output denoised depth frame (uint16 mm) for encoding
  - Zero-depth pixels (invalid) are excluded from median computation

This runs in the consumer thread, after frame dequeue and before encoding.
Window size is configurable (default 5 frames, matching gallery analysis).
"""
import numpy as np
from collections import deque


class TemporalMedianDenoiser:
    """Sliding-window temporal median filter for depth frames."""

    def __init__(self, window_size=5, depth_shape=(480, 640)):
        """
        Args:
            window_size: Number of frames in sliding window (odd preferred)
            depth_shape: (H, W) of depth frames
        """
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if window_size % 2 == 0:
            window_size += 1  # ensure odd for true median
        self.window_size = window_size
        self.depth_shape = depth_shape
        self._window = deque(maxlen=window_size)
        self._frame_count = 0

    def process(self, depth_bytes):
        """
        Apply temporal median filter to a depth frame.

        Args:
            depth_bytes: Raw uint16 LE depth bytes (H*W*2)

        Returns:
            Denoised depth bytes (uint16 LE, same shape)
        """
        # Parse input depth
        dep = np.frombuffer(depth_bytes, dtype=np.uint16).reshape(self.depth_shape)

        # Add to window
        self._window.append(dep.astype(np.float32))
        self._frame_count += 1

        # Not enough frames yet - return original
        if len(self._window) < self.window_size:
            return depth_bytes

        # Stack window: (window, H, W)
        dep_stack = np.stack(self._window, axis=0)

        # Compute per-pixel median, ignoring zeros (invalid depth)
        # Replace zeros with NaN for median computation
        dep_stack_masked = np.where(dep_stack > 0, dep_stack, np.nan)

        # Suppress All-NaN slice warning (expected for all-zero pixels)
        with np.errstate(invalid='ignore'):
            dep_med = np.nanmedian(dep_stack_masked, axis=0)

        # Where all window values were zero/NaN, keep zero
        dep_med = np.where(np.isnan(dep_med), 0, dep_med)

        # Clip and convert back to uint16
        dep_out = np.clip(dep_med, 0, 65535).astype(np.uint16)

        return dep_out.tobytes()

    def reset(self):
        """Clear the sliding window."""
        self._window.clear()
        self._frame_count = 0

    @property
    def frames_in_window(self):
        return len(self._window)

    @property
    def total_frames_processed(self):
        return self._frame_count