"""BiRefNet 模型适配层。

原则：
- 权重不随项目分发。未检测到本地权重或缺少运行依赖时，状态为 NOT_READY。
- 绝不伪造推理结果：NOT_READY 时 predict() 抛 ModelNotReady。
- torch / transformers 为可选依赖：缺失时同样视为 NOT_READY（而不是崩溃）。
"""
from __future__ import annotations

import logging
import threading
from enum import Enum

import numpy as np
from PIL import Image

from . import config

# 推理输入边长对齐：BiRefNet 编码器 32 倍下采样，非 32 倍数容易在侧向输出插值处失配
INFER_ALIGN = 32


class ModelState(str, Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    LOADING = "LOADING"
    FAILED = "FAILED"


class ModelNotReady(RuntimeError):
    pass


def _load_runtime():
    """导入推理运行时，返回 (torch, torchvision.transforms)。

    依赖缺失时抛 ImportError，由调用方按 NOT_READY 处理。
    单测用 fake 运行时替换本函数，不加载真实权重、不碰 GPU。
    """
    import torch
    import torchvision.transforms as transforms
    import transformers  # noqa: F401

    return torch, transforms


def _build_model():
    """构造 BiRefNet；单测替换为 fake 模型。"""
    from transformers import AutoModelForImageSegmentation

    return AutoModelForImageSegmentation.from_pretrained(
        str(config.WEIGHTS_DIR), trust_remote_code=True
    )


def _align32(value: float) -> int:
    return max(INFER_ALIGN, int(round(value / INFER_ALIGN)) * INFER_ALIGN)


def infer_target_size(width: int, height: int, infer_size: int | None = None) -> tuple[int, int]:
    """推理输入尺寸 (w, h)，长边恰好等于 infer_size。

    - 长边 <= infer_size：沿用官方 handler 的正方形放大口径（正方形输入），
      保持已验证的 512x600 等小图路径不变。
    - 长边 > infer_size：等比缩小到长边 = infer_size，短边按 32 对齐，
      不把 4K 大图拉成正方形（避免纵横比失真与无谓的像素重采样）。
    """
    limit = infer_size or config.INFER_SIZE
    longest = max(width, height)
    if longest <= limit:
        return (limit, limit)
    scale = limit / float(longest)
    if width >= height:
        return (limit, _align32(height * scale))
    return (_align32(width * scale), limit)


def restore_alpha(alpha: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """把推理输出 alpha 还原到原图尺寸 (w, h)，返回 float32 [h,w]。

    用 PIL "F" 模式双线性缩放（保留浮点精度），不经过 8bit 量化。
    """
    a = np.asarray(alpha, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError("alpha 必须是二维数组")
    w, h = size
    a = np.clip(a, 0.0, 1.0)
    if a.shape != (h, w):
        a = np.asarray(Image.fromarray(a, mode="F").resize((w, h), Image.BILINEAR), dtype=np.float32)
    return np.clip(a, 0.0, 1.0).astype(np.float32)


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
            _load_runtime()
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
            "infer_max_edge": config.INFER_SIZE,
        }

    # ---- 加载与推理 ----
    def _ensure_model(self):
        with self._lock:
            if self._model is not None:
                return self._model
            weight = config.find_weight_file()
            if weight is None:
                raise ModelNotReady(self._detail)
            # S3 KPI 锚点：每次真实模型加载都记一行；复用常驻服务时该计数必须为 0。
            logging.getLogger("matting.adapter").info("自动加载模型: %s", weight)
            self._state = ModelState.LOADING
            try:
                torch, _ = _load_runtime()
                model = _build_model()
                model.eval()
                if torch.cuda.is_available():
                    # 先在半精度（宿主内存）转换，再整模型上卡：
                    # 避免 FP32 全量权重先占用 GPU 再就地转 FP16 的双份峰值。
                    model.half()
                    model.to("cuda")
                else:
                    model.to("cpu")
                self._model = model
                self._state = ModelState.READY
                return model
            except Exception as exc:
                self._state = ModelState.FAILED
                self._detail = f"加载失败：{exc}"
                raise

    def predict(self, image: Image.Image) -> np.ndarray:
        """真实 BiRefNet 推理，返回 [H,W] float32 alpha（尺寸=原图）。

        预处理与官方 handler 一致：ImageNet 归一化、eval 模式取最后一个尺度
        预测并 sigmoid；输入尺寸见 infer_target_size（长边 = INFER_SIZE）。
        模型为 FP16（CUDA）时输入同步转半精度；alpha 浮点还原到原图尺寸。
        """
        model = self._ensure_model()  # 未就绪时抛 ModelNotReady
        torch, transforms = _load_runtime()

        img = image.convert("RGB")
        w, h = img.size
        tw, th = infer_target_size(w, h)
        to_tensor = transforms.Compose([
            transforms.Resize((th, tw)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
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
        return restore_alpha(alpha, (w, h))


adapter = BiRefNetAdapter()
