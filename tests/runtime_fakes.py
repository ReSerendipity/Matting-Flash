"""推理运行时替身：用 fake torch/torchvision/模型跑通适配层契约，不加载权重、不碰 GPU。"""
from __future__ import annotations

import numpy as np
from PIL import Image


class FakeTensor:
    def __init__(self, array) -> None:
        self.array = np.asarray(array, dtype=np.float32)
        self.tgt = None

    @property
    def shape(self):
        return self.array.shape

    def unsqueeze(self, dim: int):
        return FakeTensor(np.expand_dims(self.array, dim))

    def to(self, device=None, dtype=None):
        self.tgt = (device, dtype)
        return self

    def squeeze(self):
        return FakeTensor(np.squeeze(self.array))

    def float(self):
        return self

    def sigmoid(self):
        return FakeTensor(1.0 / (1.0 + np.exp(-self.array)))

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class FakeResize:
    def __init__(self, size) -> None:
        self.size = size  # torchvision 口径 (h, w)

    def __call__(self, img):
        h, w = self.size
        return img.resize((w, h), Image.BILINEAR)


class FakeToTensor:
    def __call__(self, img):
        arr = np.asarray(img, dtype=np.float32) / 255.0
        return FakeTensor(np.transpose(arr, (2, 0, 1)))


class FakeNormalize:
    def __init__(self, mean, std) -> None:
        self.mean = np.asarray(mean, dtype=np.float32).reshape(3, 1, 1)
        self.std = np.asarray(std, dtype=np.float32).reshape(3, 1, 1)

    def __call__(self, tensor):
        return FakeTensor((tensor.array - self.mean) / self.std)


class FakeCompose:
    def __init__(self, ops) -> None:
        self.ops = ops

    def __call__(self, value):
        for op in self.ops:
            value = op(value)
        return value


class FakeTransforms:
    Compose = FakeCompose
    Resize = FakeResize
    ToTensor = FakeToTensor
    Normalize = FakeNormalize


class FakeCuda:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class FakeTorch:
    def __init__(self, cuda_available: bool = True) -> None:
        self.cuda = FakeCuda(cuda_available)

    def no_grad(self):
        import contextlib

        return contextlib.nullcontext()


class FakeParam:
    def __init__(self, device="cpu", dtype="float32") -> None:
        self.device = device
        self.dtype = dtype


class FakeModel:
    """记录 eval/half/to 调用顺序；前向返回宽度方向的 logits 斜坡。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen_tensor = None
        self._param = FakeParam()
        self.fail_on_call = False

    def parameters(self):
        return iter([self._param])

    @property
    def param(self) -> FakeParam:
        return self._param

    def eval(self):
        self.calls.append("eval")
        return self

    def half(self):
        self.calls.append("half")
        self._param.dtype = "float16"
        return self

    def to(self, device):
        self.calls.append(f"to:{device}")
        self._param.device = str(device)
        return self

    def __call__(self, x):
        if self.fail_on_call:
            raise RuntimeError("fake model boom")
        self.seen_tensor = x
        th, tw = x.shape[-2:]
        ramp = np.linspace(-6.0, 6.0, tw, dtype=np.float32)
        return [FakeTensor(np.tile(ramp, (th, 1))[None, None])]
