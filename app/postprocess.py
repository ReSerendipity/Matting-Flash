"""Alpha matte 轻量后处理：羽化 / 阈值 / 二值化 / 腐蚀膨胀。

纯 numpy 实现，不依赖模型，可在无权重环境下完整测试。
所有 alpha 均为 float32 的 [H, W] 数组，取值范围 [0, 1]。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class PostParams:
    # 羽化半径（像素）。0 表示不羽化。
    feather_radius: float = 0.0
    # 硬阈值下限 [0,1]；None 表示不做阈值。
    threshold: float | None = None
    # 是否在二值化模式下输出纯 0/1（threshold 缺省按 0.5）。
    binarize: bool = False
    # 腐蚀半径（像素，灰度 min 滤波）。
    erode: int = 0
    # 膨胀半径（像素，灰度 max 滤波）。
    dilate: int = 0

    def validate(self) -> None:
        if self.feather_radius < 0:
            raise ValueError("feather_radius 必须 >= 0")
        if self.threshold is not None and not (0.0 <= self.threshold <= 1.0):
            raise ValueError("threshold 必须在 [0,1]")
        if self.erode < 0 or self.dilate < 0:
            raise ValueError("erode/dilate 必须 >= 0")
        if self.erode > 8 or self.dilate > 8:
            raise ValueError("erode/dilate 半径过大（<=8）")


DEFAULTS = PostParams()


def reset_params() -> dict:
    """所有参数复位为默认值。"""
    return asdict(PostParams())


def apply_alpha(alpha: np.ndarray, p: PostParams) -> np.ndarray:
    """按参数处理 alpha，返回新的 float32 [H,W]（不改原数组）。"""
    p.validate()
    a = np.asarray(alpha, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError("alpha 必须是二维数组")
    a = np.clip(a, 0.0, 1.0)

    if p.dilate > 0:
        a = _morphology(a, p.dilate, extend=True)
    if p.erode > 0:
        a = _morphology(a, p.erode, extend=False)
    if p.feather_radius > 0:
        a = _box_blur(a, radius=max(1, int(round(p.feather_radius))), passes=3)

    gate = p.threshold if p.threshold is not None else (0.5 if p.binarize else None)
    if gate is not None:
        if p.binarize:
            a = (a >= gate).astype(np.float32)
        else:
            a = np.where(a < gate, 0.0, a)
    return np.clip(a, 0.0, 1.0).astype(np.float32)


def _box_blur(a: np.ndarray, radius: int, passes: int = 3) -> np.ndarray:
    """可分离盒式模糊（重复 3 次逼近高斯），edge 填充。"""
    out = a
    for _ in range(passes):
        out = _box_1d(out, radius, axis=1)
        out = _box_1d(out, radius, axis=0)
    return out


def _box_1d(a: np.ndarray, radius: int, axis: int) -> np.ndarray:
    n = a.shape[axis]
    if radius <= 0 or n == 0:
        return a
    pad = [(0, 0), (0, 0)]
    pad[axis] = (radius, radius)
    ap = np.pad(a, pad, mode="edge")
    zeros_shape = list(ap.shape)
    zeros_shape[axis] = 1
    c = np.concatenate([np.zeros(zeros_shape, dtype=np.float32),
                        np.cumsum(ap, axis=axis, dtype=np.float32)], axis=axis)
    w = 2 * radius + 1
    sl_hi = [slice(None), slice(None)]
    sl_lo = [slice(None), slice(None)]
    sl_hi[axis] = slice(w, w + n)
    sl_lo[axis] = slice(0, n)
    return (c[tuple(sl_hi)] - c[tuple(sl_lo)]) / float(w)


def _morphology(a: np.ndarray, radius: int, extend: bool) -> np.ndarray:
    """灰度形态学：extend=True 为膨胀（max），False 为腐蚀（min）。"""
    out = a.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            shifted = _shift_edge(a, dy, dx)
            out = np.maximum(out, shifted) if extend else np.minimum(out, shifted)
    return out


def _shift_edge(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    h, w = a.shape
    out = np.empty_like(a)
    ys_src = slice(max(0, -dy), h - max(0, dy))
    xs_src = slice(max(0, -dx), w - max(0, dx))
    ys_dst = slice(max(0, dy), h - max(0, -dy))
    xs_dst = slice(max(0, dx), w - max(0, -dx))
    out[ys_dst, xs_dst] = a[ys_src, xs_src]
    if dy > 0:
        out[:dy, :] = out[dy:dy + 1, :]
    elif dy < 0:
        out[dy:, :] = out[dy - 1:dy, :]
    if dx > 0:
        out[:, :dx] = out[:, dx:dx + 1]
    elif dx < 0:
        out[:, dx:] = out[:, dx - 1:dx]
    return out
