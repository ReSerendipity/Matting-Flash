"""Matting-Lite 配置。所有值均可被环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 模型：官方 BiRefNet（HF）。权重不入库，由用户自行下载到本地目录。
MODEL_ID = os.environ.get("MATTING_MODEL_ID", "ZhengPeng7/BiRefNet")
WEIGHTS_DIR = Path(os.environ.get("MATTING_WEIGHTS_DIR", str(PROJECT_ROOT / "weights")))
# 期望的权重文件名（safetensors 优先，兼容 pth）
WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "BiRefNet.pth")

RESULTS_DIR = Path(os.environ.get("MATTING_RESULTS_DIR", str(PROJECT_ROOT / "results")))

HOST = os.environ.get("MATTING_HOST", "127.0.0.1")  # 仅监听本机
PORT = int(os.environ.get("MATTING_PORT", "8765"))

MAX_UPLOAD_MB = int(os.environ.get("MATTING_MAX_UPLOAD_MB", "20"))
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_EDGE = int(os.environ.get("MATTING_MAX_IMAGE_EDGE", "4096"))

# 真实推理分辨率：官方 handler 口径（固定正方形，最长边 <= 1024）
INFER_SIZE = int(os.environ.get("MATTING_INFER_SIZE", "1024"))

# 批量与重试
MAX_RETRY_ATTEMPTS = int(os.environ.get("MATTING_MAX_RETRY", "2"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("MATTING_JOB_TIMEOUT", "120"))


def find_weight_file() -> Path | None:
    """返回本地已存在的权重文件路径；不存在则 None（对应 UI 的 NOT_READY）。"""
    for name in WEIGHT_FILES:
        candidate = WEIGHTS_DIR / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None
