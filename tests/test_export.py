"""透明 PNG 导出测试：alpha、颜色模式与尺寸保持。"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from app.export_png import alpha_to_image, composite_rgba, save_transparent_png


def test_composite_keeps_size_mode_and_alpha():
    img = Image.new("RGB", (16, 12), (10, 20, 30))
    alpha = np.linspace(0.0, 1.0, 16 * 12, dtype=np.float32).reshape(12, 16)
    out = composite_rgba(img, alpha)
    assert out.mode == "RGBA"
    assert out.size == (16, 12)
    a = np.asarray(out.getchannel("A"), dtype=np.float32) / 255.0
    assert np.allclose(a, alpha, atol=1 / 255.0 + 1e-3)


def test_rgb_preserved_where_opaque():
    img = Image.new("RGB", (8, 8), (200, 30, 30))
    alpha = np.ones((8, 8), dtype=np.float32)
    out = composite_rgba(img, alpha)
    px = out.getpixel((3, 3))
    assert px[:3] == (200, 30, 30) and px[3] == 255


def test_fully_transparent_background():
    img = Image.new("RGB", (8, 8), (200, 30, 30))
    alpha = np.zeros((8, 8), dtype=np.float32)
    out = composite_rgba(img, alpha)
    assert out.getpixel((0, 0))[3] == 0


def test_save_roundtrip(tmp_path):
    img = Image.new("RGB", (20, 10), (5, 200, 90))
    alpha = np.tile(np.linspace(0, 1, 20, dtype=np.float32), (10, 1))
    rgba = composite_rgba(img, alpha)
    p = save_transparent_png(rgba, tmp_path / "out.png")
    with Image.open(p) as re:
        assert re.mode == "RGBA"
        assert re.size == (20, 10)
        a = np.asarray(re.getchannel("A"), dtype=np.float32) / 255.0
        assert np.allclose(a, alpha, atol=1 / 255.0 + 1e-3)


def test_size_mismatch_raises():
    img = Image.new("RGB", (8, 8))
    with pytest.raises(ValueError):
        composite_rgba(img, np.zeros((4, 4), dtype=np.float32))


def test_mask_preview_range():
    alpha = np.array([[0.0, 1.0], [0.5, 0.25]], dtype=np.float32)
    m = alpha_to_image(alpha)
    assert m.mode == "RGB" and m.size == (2, 2)
    g = np.asarray(m.convert("L"), dtype=np.float32) / 255.0
    assert abs(g.max() - 1.0) < 1e-6 and abs(g.min() - 0.0) < 1e-6
