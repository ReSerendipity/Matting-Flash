"""透明 PNG 导出：保持原图颜色与尺寸，仅替换/新增 alpha 通道。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def composite_rgba(image: Image.Image, alpha: np.ndarray) -> Image.Image:
    """把灰度 alpha（float32 [0,1]，[H,W]）合成到图像上，返回 RGBA。

    - 输入图像可为 RGB / RGBA / L / P 等；统一先转 RGB（P 模式按调色板展开）。
    - alpha 尺寸必须与图像一致，否则报错（不静默缩放，避免错位）。
    """
    img = image.convert("RGB")
    a = np.asarray(alpha, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError("alpha 必须是二维数组")
    if a.shape != (img.height, img.width):
        raise ValueError(f"alpha 尺寸 {a.shape} 与图像 {(img.height, img.width)} 不一致")
    a8 = np.clip(a * 255.0 + 0.5, 0, 255).astype(np.uint8)
    rgba = np.dstack([np.asarray(img, dtype=np.uint8), a8])
    return Image.fromarray(rgba, mode="RGBA")


def save_transparent_png(image_rgba: Image.Image, path: Path) -> Path:
    """保存 RGBA PNG，保持尺寸与颜色模式。"""
    if image_rgba.mode != "RGBA":
        raise ValueError("只接受 RGBA 图像")
    path.parent.mkdir(parents=True, exist_ok=True)
    image_rgba.save(path, format="PNG")
    return path


def alpha_to_image(alpha: np.ndarray) -> Image.Image:
    """把 alpha 单独渲染成可视化的黑白蒙版图（用于预览）。"""
    a = np.clip(np.asarray(alpha, dtype=np.float32), 0.0, 1.0)
    a8 = (a * 255.0 + 0.5).astype(np.uint8)
    return Image.fromarray(a8, mode="L").convert("RGB")
