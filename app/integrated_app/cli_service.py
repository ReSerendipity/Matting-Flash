"""CLI 本地直调层：复用 app.adapter / app.queue，不复制推理逻辑。

产物统一落 config.RESULTS_DIR（安装态为 %LOCALAPPDATA%\\Matting-Flash\\results），
--output-dir 只做最终拷贝。CLI 每命令独立进程，任务台账落 JSON 文件以支撑
jobs list/show/cancel/retry（见 docs/ADR-CLI.md 决策 4）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app import __version__, config
from app.adapter import ModelNotReady
from app.adapter import adapter as default_adapter
from app.postprocess import PostParams, reset_params
from app.queue import InvalidFile, MattingService, validate_upload

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_LEDGER_NAME = "cli_jobs.json"
_SENSITIVE = ("token", "secret", "password", "credential", "api_key", "authorization")


def _adapter():
    """可替换的适配器入口（测试用 fake 注入）。"""
    return default_adapter


def _service() -> MattingService:
    return MattingService(adapter_instance=_adapter(), autostart=False)


def build_params(args: Any) -> PostParams:
    """把 CLI 参数转成既有 PostParams；校验失败抛 ValueError。"""
    params = PostParams(
        feather_radius=float(getattr(args, "feather_radius", 0.0)),
        threshold=getattr(args, "threshold", None),
        binarize=bool(getattr(args, "binarize", False)),
        erode=int(getattr(args, "erode", 0)),
        dilate=int(getattr(args, "dilate", 0)),
    )
    params.validate()
    return params


def _inputs(paths: list[str], input_dir: str | None) -> list[Path]:
    result = [Path(p) for p in paths]
    if input_dir:
        directory = Path(input_dir)
        if not directory.is_dir():
            raise ValueError(f"--input-dir 不是目录: {directory}")
        result.extend(
            p for p in sorted(directory.iterdir()) if p.suffix.lower() in IMAGE_EXTENSIONS
        )
    if not result:
        raise ValueError("至少提供一个输入图片（-i 或 --input-dir）")
    return result


def _copy_results(info: dict[str, Any], output_dir: str | Path) -> list[dict[str, str]]:
    destination = Path(output_dir)
    if destination.resolve() != config.RESULTS_DIR.resolve():
        destination.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, str]] = []
    for key, kind in (("cutout_name", "cutout"), ("mask_name", "mask")):
        name = info.get(key, "")
        if not name:
            continue
        source = config.RESULTS_DIR / name
        target = destination / name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        out.append({"kind": kind, "path": str(target.resolve())})
    return out


def _ledger_path() -> Path:
    return config.RESULTS_DIR / _LEDGER_NAME


def _read_ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _write_ledger(jobs: list[dict[str, Any]]) -> None:
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    target = _ledger_path()
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _ledger_add(entries: list[dict[str, Any]]) -> None:
    jobs = _read_ledger()
    jobs.extend(entries)
    _write_ledger(jobs)


def _require_ready() -> None:
    state = _adapter().state.value
    if state != "READY":
        raise ModelNotReady(_adapter().detail or f"模型未就绪（state={state}）")


def single(image: str, output_dir: str, params: PostParams) -> dict[str, Any]:
    """本地单图抠图：校验 → 复用 service layer → 产物落固定目录。"""
    source = Path(image)
    data = source.read_bytes()  # 缺文件由 CLI 层映射为 NOT_READY
    validate_upload(source.name, data)
    _require_ready()
    info = _service().submit_single(source.name, data, params)
    return {
        "mode": "local",
        "input": str(source.resolve()),
        "width": info["width"],
        "height": info["height"],
        "results": _copy_results(info, output_dir),
        "output_dir": str(Path(output_dir).resolve()),
    }


def batch(
    images: list[str], input_dir: str | None, output_dir: str, params: PostParams
) -> dict[str, Any]:
    """本地批量：逐张同步走既有任务队列，结果写入 CLI 台账。"""
    sources = _inputs(images, input_dir)
    payloads: list[tuple[str, bytes]] = []
    invalid: list[dict[str, str]] = []
    for source in sources:
        try:
            data = source.read_bytes()
            validate_upload(source.name, data)
            payloads.append((source.name, data))
        except (OSError, InvalidFile, ValueError) as exc:
            invalid.append({"input": str(source), "error": str(exc)})
    if payloads:
        _require_ready()

    service = _service()
    jobs = service.submit_batch(payloads, params)
    results: list[dict[str, Any]] = []
    failures = list(invalid)
    entries: list[dict[str, Any]] = []
    for job, source in zip(
        jobs,
        [p for p in sources if not any(f["input"] == str(p) for f in invalid)],
        strict=True,
    ):
        service.process_job(job)
        state = job.state.value
        entry = {
            "job_id": job.job_id,
            "filename": job.filename,
            "input": str(source.resolve()),
            "state": state,
            "error": job.error,
            "result_name": job.result_name,
            "created_at": job.created_at,
        }
        entries.append(entry)
        if state == "done":
            info = {
                "cutout_name": job.result_name,
                "mask_name": next(
                    (f.name for f in config.RESULTS_DIR.glob(f"{Path(job.filename).stem}_mask_*.png")),
                    "",
                ),
            }
            results.append(
                {
                    "job_id": job.job_id,
                    "input": str(source.resolve()),
                    "results": _copy_results(info, output_dir),
                }
            )
        else:
            failures.append({"job_id": job.job_id, "input": str(source), "error": job.error})
    _ledger_add(entries)
    return {
        "mode": "local",
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
        "output_dir": str(Path(output_dir).resolve()),
    }


def jobs_list(state: str | None = None) -> dict[str, Any]:
    jobs = _read_ledger()
    if state:
        jobs = [j for j in jobs if j.get("state") == state]
    return {"jobs": jobs, "count": len(jobs)}


def jobs_show(job_id: str) -> dict[str, Any]:
    for job in _read_ledger():
        if job.get("job_id") == job_id:
            return job
    raise ValueError(f"未知任务: {job_id}")


def _update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    jobs = _read_ledger()
    for job in jobs:
        if job.get("job_id") == job_id:
            job.update(changes)
            _write_ledger(jobs)
            return job
    raise ValueError(f"未知任务: {job_id}")


def jobs_cancel(job_id: str) -> dict[str, Any]:
    job = jobs_show(job_id)
    if job.get("state") in ("done", "canceled"):
        raise ValueError(f"任务 {job_id} 状态为 {job['state']}，不可取消")
    return _update_job(job_id, state="canceled", error="用户通过 CLI 取消")


def jobs_retry(job_id: str, output_dir: str) -> dict[str, Any]:
    """重新读取输入文件执行一次；原记录保留，新记录追加（与服务端 retry 语义一致）。"""
    job = jobs_show(job_id)
    if job.get("state") not in ("failed", "invalid", "canceled"):
        raise ValueError(f"任务 {job_id} 状态为 {job['state']}，不可重试")
    source = Path(job.get("input", ""))
    if not source.is_file():
        raise FileNotFoundError(f"原输入文件不存在，无法重试: {source}")
    params = PostParams()
    data = single(str(source), output_dir, params)
    new_job = {
        "job_id": uuid.uuid4().hex[:12],
        "filename": source.name,
        "input": str(source.resolve()),
        "state": "done",
        "error": "",
        "result_name": next(
            (r["path"] for r in data["results"] if r["kind"] == "cutout"), ""
        ),
        "created_at": time.time(),
        "retried_from": job_id,
    }
    _ledger_add([new_job])
    return {"original": job, "new_job": new_job}


def batch_resume(output_dir: str) -> dict[str, Any]:
    """P2：重跑台账中所有失败/无效任务（已有成功记录不动）。"""
    pending = [j for j in _read_ledger() if j.get("state") in ("failed", "invalid")]
    done = 0
    failures: list[dict[str, str]] = []
    for job in pending:
        try:
            jobs_retry(job["job_id"], output_dir)
            done += 1
        except (ValueError, FileNotFoundError, ModelNotReady, InvalidFile) as exc:
            failures.append({"job_id": job.get("job_id", ""), "error": str(exc)})
    return {"resumed": len(pending), "succeeded": done, "failures": failures}


def results_list() -> dict[str, Any]:
    files = [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
        }
        for p in sorted(config.RESULTS_DIR.iterdir())
        if p.is_file() and p.name != _LEDGER_NAME
    ] if config.RESULTS_DIR.is_dir() else []
    return {"results": files, "count": len(files), "results_dir": str(config.RESULTS_DIR)}


def results_download(name: str, destination: str) -> dict[str, Any]:
    """从固定 results/ 目录下载（复制）一个产物；拒绝路径穿越。"""
    requested = Path(name).name
    if not requested or requested != name:
        raise ValueError("结果名必须是纯文件名")
    source = config.RESULTS_DIR / requested
    if not source.is_file():
        raise FileNotFoundError(f"结果不存在: {requested}")
    target = Path(destination)
    if target.is_dir() or str(destination).endswith(("/", "\\")):
        target = target / requested
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {"name": requested, "path": str(target.resolve()), "size": target.stat().st_size}


def presets() -> dict[str, Any]:
    return {
        "postprocess_defaults": reset_params(),
        "limits": {
            "max_upload_mb": config.MAX_UPLOAD_MB,
            "max_image_edge": config.MAX_IMAGE_EDGE,
            "infer_size": config.INFER_SIZE,
            "allowed_extensions": sorted(config.ALLOWED_EXTENSIONS),
        },
    }


def _model_info() -> dict[str, Any]:
    state = _adapter()
    weights = [
        {"name": name, "exists": (config.WEIGHTS_DIR / name).is_file()}
        for name in config.WEIGHT_FILES
    ]
    return {
        "model_id": config.MODEL_ID,
        "state": state.state.value,
        "detail": state.detail,
        "weights_dir": str(config.WEIGHTS_DIR),
        "weights": weights,
        "infer_size": config.INFER_SIZE,
    }


def models_list() -> dict[str, Any]:
    return {"models": [_model_info()]}


def models_show() -> dict[str, Any]:
    return _model_info()


def engines_list() -> dict[str, Any]:
    return {
        "engines": [
            {
                "name": "BiRefNet",
                "model_id": config.MODEL_ID,
                "state": _adapter().state.value,
                "weights_dir": str(config.WEIGHTS_DIR),
            }
        ]
    }


def model_check() -> dict[str, Any]:
    info = _model_info()
    info["ready"] = info["state"] == "READY"
    return info


def model_download() -> dict[str, Any]:
    """只输出官方下载指引；CLI 不擅自联网、不改用户权重。"""
    return {
        "message": "请按 docs/MODELS.md 从官方源下载 model.safetensors 后放入权重目录",
        "weights_dir": str(config.WEIGHTS_DIR),
        "commands": [
            'pip install "huggingface-hub>0.25"',
            (
                "python -c \"from huggingface_hub import snapshot_download; "
                "snapshot_download('ZhengPeng7/BiRefNet', local_dir='weights')\""
            ),
        ],
    }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if any(s in str(k).lower() for s in _SENSITIVE) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def system_status() -> dict[str, Any]:
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "model_state": _adapter().state.value,
        "model_detail": _adapter().detail,
        "results_dir": str(config.RESULTS_DIR),
    }


def system_gpu() -> dict[str, Any]:
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - 可选依赖缺失必须转成结构化降级信息
        return {"available": False, "reason": f"torch 不可用: {exc}"}
    available = bool(torch.cuda.is_available())
    info: dict[str, Any] = {"available": available, "torch": torch.__version__}
    if available:
        info["device_count"] = torch.cuda.device_count()
        info["device"] = torch.cuda.get_device_name(0)
        free, total = torch.cuda.mem_get_info()
        info["free_bytes"] = free
        info["total_bytes"] = total
    return info


def system_config() -> dict[str, Any]:
    """递归脱敏后输出关键配置（嵌套结构用于验证脱敏递归性）。"""
    return _redact(
        {
            "app": {
                "version": __version__,
                "model_id": config.MODEL_ID,
                "weights_dir": str(config.WEIGHTS_DIR),
                "results_dir": str(config.RESULTS_DIR),
            },
            "server": {"host": config.HOST, "port": config.PORT},
            "limits": {
                "max_upload_mb": config.MAX_UPLOAD_MB,
                "max_image_edge": config.MAX_IMAGE_EDGE,
                "infer_size": config.INFER_SIZE,
            },
            "diagnostics": {"example_token": "do-not-leak", "nested": {"api_key": "secret-value"}},
        }
    )


def serve(host: str, port: int) -> dict[str, Any]:
    """兜底：独立拉起 FastAPI 服务（桌面壳之外的使用场景）。"""
    import uvicorn

    from app.main import app

    uvicorn.run(app, host=host, port=port)
    return {"host": host, "port": port, "message": "服务已退出"}
