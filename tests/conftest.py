"""共享测试工具。"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def ramp_alpha() -> np.ndarray:
    """32x32 垂直阶跃 alpha：左 1 右 0。"""
    a = np.zeros((32, 32), dtype=np.float32)
    a[:, :16] = 1.0
    return a
