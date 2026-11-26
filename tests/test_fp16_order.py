"""FP16 加载顺序与加载失败/未就绪语义（fake 模型，不加载真实权重）。"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image
from runtime_fakes import FakeModel, FakeTorch, FakeTransforms

from app import config
from app.adapter import ModelNotReady, ModelState


def _fresh_adapter(monkeypatch, tmp_path, *, cuda: bool = True, weight: bool = True):
    import app.adapter as mod

    wdir = tmp_path / "weights"
    wdir.mkdir()
    if weight:
        (wdir / "model.safetensors").write_bytes(b"fake-weights")
    monkeypatch.setattr(config, "WEIGHTS_DIR", wdir)
    monkeypatch.setattr(mod, "_load_runtime", lambda: (FakeTorch(cuda), FakeTransforms))
    return mod


def test_cuda_path_converts_to_half_before_moving_to_gpu(monkeypatch, tmp_path):
    mod = _fresh_adapter(monkeypatch, tmp_path, cuda=True)
    fake = FakeModel()
    monkeypatch.setattr(mod, "_build_model", lambda: fake)

    adapter = mod.BiRefNetAdapter()
    adapter._ensure_model()

    assert fake.calls == ["eval", "half", "to:cuda"], (
        "必须先在宿主内存 .half() 再 .to('cuda')，避免 FP32 全量权重先占卡"
    )
    assert fake.param.dtype == "float16" and fake.param.device == "cuda"
    assert adapter.state is ModelState.READY


def test_cpu_path_never_calls_half(monkeypatch, tmp_path):
    mod = _fresh_adapter(monkeypatch, tmp_path, cuda=False)
    fake = FakeModel()
    monkeypatch.setattr(mod, "_build_model", lambda: fake)

    adapter = mod.BiRefNetAdapter()
    adapter._ensure_model()
    assert fake.calls == ["eval", "to:cpu"], "CPU 不支持半精度前向，不得 .half()"
    assert fake.param.dtype == "float32"


def test_half_order_is_observable_in_predict_dtype_contract(monkeypatch, tmp_path):
    mod = _fresh_adapter(monkeypatch, tmp_path, cuda=True)
    fake = FakeModel()
    monkeypatch.setattr(mod, "_build_model", lambda: fake)
    adapter = mod.BiRefNetAdapter()
    alpha = adapter.predict(Image.new("RGB", (64, 48)))
    # 输入按模型精度转半精度；输出仍是 float32 [0,1] 且尺寸=原图
    assert fake.seen_tensor.tgt == ("cuda", "float16")
    assert alpha.dtype == np.float32
    assert alpha.shape == (48, 64)
    assert 0.0 <= float(alpha.min()) and float(alpha.max()) <= 1.0


def test_model_build_failure_marks_failed(monkeypatch, tmp_path):
    mod = _fresh_adapter(monkeypatch, tmp_path, cuda=True)

    def boom():
        raise RuntimeError("build boom")

    monkeypatch.setattr(mod, "_build_model", boom)
    adapter = mod.BiRefNetAdapter()
    with pytest.raises(RuntimeError):
        adapter._ensure_model()
    assert adapter.state is ModelState.FAILED
    assert "build boom" in adapter.detail


def test_missing_weight_refuses_without_loading(monkeypatch, tmp_path):
    mod = _fresh_adapter(monkeypatch, tmp_path, weight=False)
    adapter = mod.BiRefNetAdapter()
    assert adapter.state is ModelState.NOT_READY
    with pytest.raises(ModelNotReady):
        adapter.predict(Image.new("RGB", (16, 16)))
