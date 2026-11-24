"""BiRefNet 模型适配层。

原则：
- 权重不随项目分发。未检测到本地权重或缺少运行依赖时，状态为 NOT_READY。
- 绝不伪造推理结果：NOT_READY 时 predict() 抛 ModelNotReady。
- torch / transformers 为可选依赖：缺失时同样视为 NOT_READY（而不是崩溃）。
"""
from __future__ import annotations

import threading
from enum import Enum

import numpy as np
from PIL import Image

from . import config


class ModelState(str, Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    LOADING = "LOADING"
    FAILED = "FAILED"


class ModelNotReady(RuntimeError):
    pass


class BiRefNetAdapter:
    """按需加载的 BiRefNet 适配层（模块级单例 adapter）。"""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._state = ModelState.NOT_READY
        self._detail = ""
        self._refresh_state()

    # ---- 状态 ----
    def _refresh_state(self) -> None:
        weight = config.find_weight_file()
        if weight is None:
            self._state = ModelState.NOT_READY
            self._detail = f"未找到本地权重（{config.WEIGHTS_DIR}），请按 README 下载"
            return
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except Exception as exc:  # pragma: no cover - 依赖缺失路径
            self._state = ModelState.NOT_READY
            self._detail = f"权重存在但缺少推理依赖：{exc}"
            return
        self._state = ModelState.READY
        self._detail = f"权重就绪：{weight}"

    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    def status_dict(self) -> dict:
        return {
            "model_id": config.MODEL_ID,
            "state": self._state.value,
            "detail": self._detail,
            "weights_dir": str(config.WEIGHTS_DIR),
        }

    # ---- 加载与推理 ----
    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            weight = config.find_weight_file()
            if weight is None:
                raise ModelNotReady(self._detail)
            self._state = ModelState.LOADING
            try:
                import torch
                from transformers import AutoModelForImageSegmentation

                self._model = AutoModelForImageSegmentation.from_pretrained(
                    str(config.WEIGHTS_DIR), trust_remote_code=True
                )
                self._model.eval()
                device = "cuda" if torch.cuda.is_available() else "cpu"
                self._model.to(device)
                if device == "cuda":
                    self._model.half()  # 真实推理使用 FP16（CPU 不支持半精度前向）
                self._state = ModelState.READY
                return self._model
            except Exception as exc:
                self._state = ModelState.FAILED
                self._detail = f"加载失败：{exc}"
                raise

    def predict(self, image: Image.Image) -> np.ndarray:
        """真实 BiRefNet 推理，返回 [H,W] float32 alpha。

        预处理与官方 handler 一致：固定 Resize 到 MATTING_INFER_SIZE 正方形、
        ImageNet 归一化；eval 模式取输出的最后一个尺度预测并 sigmoid。
        模型为 FP16（CUDA）时输入同步转半精度。
        """
        model = self._ensure_model()  # 未就绪时抛 ModelNotReady
        import torch
        from torchvision import transforms as T

        img = image.convert("RGB")
        w, h = img.size
        size = config.INFER_SIZE
        to_tensor = T.Compose([
            T.Resize((size, size)),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        x = to_tensor(img).unsqueeze(0)
        param = next(model.parameters())
        x = x.to(device=param.device, dtype=param.dtype)
        with torch.no_grad():
            out = model(x)
        preds = out[-1] if isinstance(out, (list, tuple)) else out
        alpha = preds.squeeze().float().sigmoid().cpu().numpy()
        if alpha.ndim == 3:
            alpha = alpha[0]
        # 尺寸对齐回原图（保持尺寸保持原则）
        if alpha.shape != (h, w):
            alpha_img = Image.fromarray((alpha * 255).astype(np.uint8)).resize(
                (w, h), Image.BILINEAR
            )
            alpha = np.asarray(alpha_img, dtype=np.float32) / 255.0
        return np.clip(alpha, 0.0, 1.0).astype(np.float32)


adapter = BiRefNetAdapter()
