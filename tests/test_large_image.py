""">2K 大图契约：长边缩放至 1024 推理、输出还原原尺寸、alpha 与尺寸一致。

全部使用 fake 运行时/模型，不加载权重、不碰 GPU。
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app import config
from app.adapter import ModelState, infer_target_size, restore_alpha
from app.export_png import composite_rgba
from app.postprocess import PostParams
from app.queue import MattingService

from runtime_fakes import FakeModel, FakeTorch, FakeTransforms


def _patched_adapter(monkeypatch, tmp_path, *, cuda: bool = True):
    import app.adapter as mod

    wdir = tmp_path / "weights"
    wdir.mkdir()
    (wdir / "model.safetensors").write_bytes(b"fake-weights")  # 存在即可，不加载
    monkeypatch.setattr(config, "WEIGHTS_DIR", wdir)
    fake = FakeModel()
    monkeypatch.setattr(mod, "_load_runtime", lambda: (FakeTorch(cuda), FakeTransforms))
    monkeypatch.setattr(mod, "_build_model", lambda: fake)
    return mod.BiRefNetAdapter(), fake


@pytest.mark.parametrize(
    "width,height,expect",
    [
        (3200, 2000, (1024, 640)),   # 3.2K 横图：长边 1024，短边等比 + 32 对齐
        (2000, 3200, (640, 1024)),   # 竖图：长边仍是高度
        (4096, 4096, (1024, 1024)),
        (3000, 2400, (1024, 832)),
        (512, 600, (1024, 1024)),    # 小图沿用官方 handler 正方形口径
    ],
)
def test_infer_target_size(width, height, expect):
    assert infer_target_size(width, height) == expect
    tw, th = infer_target_size(width, height)
    assert max(tw, th) == config.INFER_SIZE, "长边必须恰好落在 INFER_SIZE"
    if max(width, height) > config.INFER_SIZE:
        assert min(tw, th) % 32 == 0, "短边需 32 对齐"


def test_restore_alpha_resizes_in_float_without_quantization():
    a = np.full((64, 48), 0.5, dtype=np.float32)
    out = restore_alpha(a, (200, 100))
    assert out.shape == (100, 200)
    assert out.dtype == np.float32
    assert np.allclose(out, 0.5, atol=1e-6), "常值 alpha 缩放后不得被 8bit 量化拖偏"


def test_predict_large_image_scales_long_edge_and_restores(monkeypatch, tmp_path):
    adapter, fake = _patched_adapter(monkeypatch, tmp_path)
    src = Image.new("RGB", (3200, 2000), (12, 60, 120))
    alpha = adapter.predict(src)

    assert fake.calls == ["eval", "half", "to:cuda"]
    assert fake.seen_tensor.shape == (1, 3, 640, 1024), "推理输入应为 长边=1024、纵横比保持"
    assert fake.seen_tensor.tgt == ("cuda", "float16"), "FP16 模型必须收到半精度输入"
    assert alpha.shape == (2000, 3200), "alpha 必须还原到原图尺寸"
    assert alpha.dtype == np.float32
    assert 0.0 <= float(alpha.min()) and float(alpha.max()) <= 1.0
    # fake 模型输出左黑右白的斜坡，缩放后方向与覆盖度保持
    assert alpha[:, :100].mean() < 0.05
    assert alpha[:, -100:].mean() > 0.95

    rgba = composite_rgba(src, alpha)  # 尺寸不一致会抛 ValueError
    assert rgba.size == (3200, 2000) and rgba.mode == "RGBA"


def test_predict_portrait_large_image(monkeypatch, tmp_path):
    adapter, fake = _patched_adapter(monkeypatch, tmp_path)
    alpha = adapter.predict(Image.new("RGB", (2000, 3200)))
    assert fake.seen_tensor.shape == (1, 3, 1024, 640)
    assert alpha.shape == (3200, 2000)


def test_predict_small_image_keeps_square_handler_input(monkeypatch, tmp_path):
    adapter, fake = _patched_adapter(monkeypatch, tmp_path)
    alpha = adapter.predict(Image.new("RGB", (512, 600)))
    assert fake.seen_tensor.shape == (1, 3, 1024, 1024)
    assert alpha.shape == (600, 512)


class _FullSizeAdapter:
    """模拟 predict 契约（返回原尺寸 alpha），用于端到端导出校验。"""

    state = ModelState.READY
    detail = "fake"

    def predict(self, image):
        w, h = image.size
        a = np.zeros((h, w), dtype=np.float32)
        a[:, : max(1, w // 2)] = 1.0
        return a


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 80, 120)).save(buf, format="PNG")
    return buf.getvalue()


def test_single_flow_large_image_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    svc = MattingService(_FullSizeAdapter(), autostart=False)
    info = svc.submit_single("big.png", _png(3200, 2000), PostParams())

    assert (info["width"], info["height"]) == (3200, 2000)
    assert info["cutout_name"] != info["mask_name"]
    with Image.open(tmp_path / info["cutout_name"]) as im:
        assert im.mode == "RGBA" and im.size == (3200, 2000)
        a = np.asarray(im.getchannel("A"))
        assert a[:, :100].mean() > 200 and a[:, -100:].mean() < 30
    with Image.open(tmp_path / info["mask_name"]) as im:
        assert im.size == (3200, 2000)


def test_large_alpha_downscaled_cpu_path_is_float32(monkeypatch, tmp_path):
    adapter, fake = _patched_adapter(monkeypatch, tmp_path, cuda=False)
    alpha = adapter.predict(Image.new("RGB", (2600, 1300)))
    assert fake.calls == ["eval", "to:cpu"]
    assert fake.seen_tensor.shape == (1, 3, 512, 1024)
    assert alpha.shape == (1300, 2600) and alpha.dtype == np.float32
