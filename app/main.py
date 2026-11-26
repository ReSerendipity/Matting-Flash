"""Matting-Flash 本地 Web 工作台（默认仅监听 127.0.0.1）。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__, config
from .adapter import ModelNotReady, adapter
from .postprocess import PostParams, reset_params
from .queue import InvalidFile, MattingService

app = FastAPI(title="Matting-Flash", version=__version__)
service = MattingService()


def _b64(img_bytes: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(img_bytes).decode("ascii")


def _parse_params(raw: str | None) -> PostParams:
    data = json.loads(raw) if raw else {}
    p = PostParams(
        feather_radius=float(data.get("feather_radius", 0.0)),
        threshold=data.get("threshold"),
        binarize=bool(data.get("binarize", False)),
        erode=int(data.get("erode", 0)),
        dilate=int(data.get("dilate", 0)),
    )
    p.validate()
    return p


@app.get("/api/status")
def status() -> dict:
    out = adapter.status_dict()
    ready = out.get("state") == "READY"
    # 桌面壳契约字段：ready/missing_weights 与 weights_dir/results_dir/version 一一对应，
    # 缺权重时 ready=false 且明确列出候选权重文件名，绝不伪造就绪或推理。
    out["ready"] = ready
    out["missing_weights"] = [] if ready else [
        name for name in config.WEIGHT_FILES if not (config.WEIGHTS_DIR / name).is_file()
    ]
    out["version"] = __version__
    out["results_dir"] = str(config.RESULTS_DIR)
    out["defaults"] = reset_params()
    out["max_upload_mb"] = config.MAX_UPLOAD_MB
    out["allowed_extensions"] = sorted(config.ALLOWED_EXTENSIONS)
    return out


@app.post("/api/matting/single")
async def matting_single(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入要求默认值
    params: str | None = Form(None),  # noqa: B008
) -> JSONResponse:
    p = _parse_params(params)
    data = await file.read()
    try:
        # 推理是阻塞调用：放线程池，避免占住事件循环导致页面轮询/状态端点假死
        info = await run_in_threadpool(
            service.submit_single, file.filename or "upload.png", data, p
        )
    except ModelNotReady as exc:
        raise HTTPException(status_code=409, detail={"code": "MODEL_NOT_READY", "message": str(exc)}) from exc
    except InvalidFile as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_FILE", "message": str(exc)}) from exc
    cutout = (config.RESULTS_DIR / info["cutout_name"]).read_bytes()
    mask = (config.RESULTS_DIR / info["mask_name"]).read_bytes()
    orig_b64 = _b64(data, mime=file.content_type or "image/png")
    return JSONResponse({
        "state": "DONE",
        "width": info["width"],
        "height": info["height"],
        "preview": {
            "original": orig_b64,
            "mask": _b64(mask),
            "result": _b64(cutout),
        },
        "cutout_name": info["cutout_name"],
        "mask_name": info["mask_name"],
    })


@app.post("/api/batch")
async def batch_submit(
    files: list[UploadFile] = File(...),  # noqa: B008 - FastAPI 依赖注入要求默认值
    params: str | None = Form(None),  # noqa: B008
) -> JSONResponse:
    p = _parse_params(params)
    payloads = []
    for f in files:
        payloads.append((f.filename or "upload.png", await f.read()))
    jobs = service.submit_batch(payloads, p)
    return JSONResponse({"jobs": [j.to_dict() for j in jobs]})


@app.get("/api/jobs")
def jobs_list() -> JSONResponse:
    return JSONResponse({"jobs": service.list_jobs()})


@app.get("/api/jobs/{job_id}")
def job_get(job_id: str) -> JSONResponse:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return JSONResponse(job.to_dict())


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> JSONResponse:
    if not service.cancel(job_id):
        raise HTTPException(status_code=409, detail={"code": "CANNOT_CANCEL"})
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{job_id}/retry")
def job_retry(job_id: str) -> JSONResponse:
    job = service.retry(job_id)
    if job is None:
        raise HTTPException(status_code=409, detail={"code": "CANNOT_RETRY"})
    # 重试是新建一条任务记录：原失败记录保留，新 job 进入队列
    return JSONResponse({"ok": True, "job": job.to_dict()})


@app.get("/results/{name}")
def result_file(name: str) -> FileResponse:
    path = (config.RESULTS_DIR / Path(name).name).resolve()
    if not str(path).startswith(str(config.RESULTS_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return FileResponse(path, media_type="image/png")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")
