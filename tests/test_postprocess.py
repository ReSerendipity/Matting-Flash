"""alpha 后处理数值边界测试。"""
from __future__ import annotations

import numpy as np
import pytest

from app.postprocess import PostParams, apply_alpha, reset_params


def test_constant_alpha_survives_feather():
    a = np.full((24, 24), 0.5, dtype=np.float32)
    out = apply_alpha(a, PostParams(feather_radius=3))
    assert out.dtype == np.float32
    assert np.allclose(out, 0.5, atol=1e-6), "常值场羽化后应保持不变"


def test_feather_preserves_range_and_shape(ramp_alpha):
    out = apply_alpha(ramp_alpha, PostParams(feather_radius=4))
    assert out.shape == ramp_alpha.shape
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_feather_softens_step_edge(ramp_alpha):
    assert set(np.unique(ramp_alpha[:, 15:18])) <= {0.0, 1.0}, "未羽化时只有纯 0/1"
    out = apply_alpha(ramp_alpha, PostParams(feather_radius=3))
    mid = out[:, 15:18]
    assert ((mid > 0.05) & (mid < 0.95)).any(), "羽化后边缘应出现中间灰度"


def test_threshold_hard_floor():
    a = np.array([[0.2, 0.6, 0.9]], dtype=np.float32)
    out = apply_alpha(a, PostParams(threshold=0.5))
    assert np.allclose(out, [[0.0, 0.6, 0.9]])


def test_binarize_uses_default_gate():
    a = np.array([[0.2, 0.6, 0.9]], dtype=np.float32)
    out = apply_alpha(a, PostParams(binarize=True))
    assert np.allclose(out, [[0.0, 1.0, 1.0]])
    assert set(np.unique(out)) <= {0.0, 1.0}


def test_erode_dilate_on_block():
    a = np.zeros((9, 9), dtype=np.float32)
    a[3:6, 3:6] = 1.0
    eroded = apply_alpha(a, PostParams(erode=1))
    dilated = apply_alpha(a, PostParams(dilate=1))
    assert eroded[4, 4] == 1.0 and eroded.sum() == 1.0, "erode=1 应只保留中心像素"
    assert dilated.sum() == 25.0, "dilate=1 应由 3x3 扩张为 5x5"
    assert dilated[2, 2] == 1.0 and dilated[1, 1] == 0.0


def test_reset_returns_defaults():
    d = reset_params()
    assert d["feather_radius"] == 0.0
    assert d["threshold"] is None
    assert d["binarize"] is False
    assert d["erode"] == 0 and d["dilate"] == 0


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        apply_alpha(np.zeros((4, 4), np.float32), PostParams(threshold=1.5))
    with pytest.raises(ValueError):
        apply_alpha(np.zeros((4, 4), np.float32), PostParams(feather_radius=-1))


def test_input_not_mutated(ramp_alpha):
    before = ramp_alpha.copy()
    apply_alpha(ramp_alpha, PostParams(feather_radius=2, threshold=0.3))
    assert np.array_equal(ramp_alpha, before)
