"""真实 BiRefNet 推理脚本（R2 phase-c 交付物）。

走项目适配层 ``app.adapter``（与 Web 服务 /api/matting/single 同一链路），
不做 mock。输出到 results/real-<YYYYMMDD-HHMMSS>/：

- input.png        输入样本（由调用方放置）
- cutout.png       透明背景 PNG（app/export_png 合成）
- mask.png         可视化的黑白 alpha 图
- metrics.json     权重哈希、尺寸、精度、耗时、峰值显存等指标
- inference.log    运行日志

用法（在 .venv-real 下）：

    python scripts/real_infer.py --input results/real-<ts>/input.png --outdir results/real-<ts>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 工单阈值：GPU 加载前空闲显存必须 >= 5GB，不足则硬失败
MIN_FREE_GPU_BYTES = 5 * 1024 ** 3


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Matting-Flash 真实 BiRefNet 推理")
    ap.add_argument("--input", required=True, help="输入图片路径")
    ap.add_argument("--outdir", required=True, help="输出目录（results/real-<ts>）")
    args = ap.parse_args()

    out: Path = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    metrics: dict = {
        "input_path": str(Path(args.input)),
        "outdir": str(out),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    rc = 0
    try:
        import torch
        from PIL import Image

        from app import config
        from app.adapter import adapter
        from app.export_png import alpha_to_image, composite_rgba, save_transparent_png

        # ---- GPU 前置检查：空闲显存 >= 5GB，否则硬失败 ----
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用；真实 BiRefNet 推理要求 GPU")
        free, total = torch.cuda.mem_get_info()
        log(f"[gpu] free={free / 2**30:.2f}GB total={total / 2**30:.2f}GB device={torch.cuda.get_device_name(0)}")
        if free < MIN_FREE_GPU_BYTES:
            raise RuntimeError(
                f"加载前空闲显存 {free / 2**30:.2f}GB < 5GB 阈值，硬失败"
            )

        weight = config.find_weight_file()
        if weight is None:
            raise RuntimeError(f"未找到本地权重（{config.WEIGHTS_DIR}）")
        metrics["weight"] = {
            "path": str(weight),
            "bytes": weight.stat().st_size,
            "sha256": sha256_file(weight),
        }
        metrics["model_id"] = config.MODEL_ID
        inp = Path(args.input)
        metrics["input_sha256"] = sha256_file(inp)

        image = Image.open(inp).convert("RGB")
        w, h = image.size
        metrics["source_size"] = [w, h]

        # ---- 加载模型（计时）----
        t0 = time.time()
        model = adapter._ensure_model()
        load_s = time.time() - t0
        precision = str(next(model.parameters()).dtype)
        device = str(next(model.parameters()).device)
        log(f"[model] loaded in {load_s:.2f}s device={device} dtype={precision}")
        metrics.update(
            batch=1,
            infer_size=[config.INFER_SIZE, config.INFER_SIZE],
            precision=precision,
            device=device,
            load_seconds=round(load_s, 3),
        )

        # ---- 真实前向（计时 + 峰值显存）----
        torch.cuda.reset_peak_memory_stats()
        t1 = time.time()
        alpha = adapter.predict(image)
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        infer_s = time.time() - t1
        peak_gb = (
            torch.cuda.max_memory_allocated() / 2**30
            if device.startswith("cuda")
            else None
        )
        a_min, a_max = float(alpha.min()), float(alpha.max())
        log(
            f"[infer] {infer_s:.3f}s peak={peak_gb:.2f}GB "
            f"alpha shape={alpha.shape} min={a_min:.4f} max={a_max:.4f} mean={float(alpha.mean()):.4f}"
        )
        if alpha.shape != (h, w):
            raise RuntimeError(f"alpha 尺寸 {alpha.shape} 与原图 {(h, w)} 不一致")
        if not (a_max > 0.5 and a_min < 0.5):
            raise RuntimeError("alpha 退化（非空校验失败）")
        metrics.update(
            infer_seconds=round(infer_s, 3),
            peak_gpu_gb=round(peak_gb, 3) if peak_gb is not None else None,
            alpha_min=a_min,
            alpha_max=a_max,
            alpha_mean=round(float(alpha.mean()), 4),
        )

        # ---- 导出透明 PNG 与蒙版 ----
        cutout_path = save_transparent_png(composite_rgba(image, alpha), out / "cutout.png")
        mask_path = out / "mask.png"
        alpha_to_image(alpha).save(mask_path, format="PNG")
        metrics["outputs"] = {
            "cutout_png": {
                "path": str(cutout_path),
                "bytes": cutout_path.stat().st_size,
                "size": list(image.size),
            },
            "mask_png": {
                "path": str(mask_path),
                "bytes": mask_path.stat().st_size,
            },
        }
        log(f"[output] {cutout_path} ({cutout_path.stat().st_size} bytes)")
        log("[done] 真实推理完成")
    except Exception as exc:  # noqa: BLE001 - 记录后按非零退出码收口
        rc = 1
        log(f"[error] {type(exc).__name__}: {exc}")
        metrics["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        metrics["exit_code"] = rc
        (out / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "inference.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
