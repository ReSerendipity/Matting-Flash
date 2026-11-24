"""输入校验、单图/批量任务队列与失败重试。

不伪造结果：模型未就绪时 submit/process 直接抛 ModelNotReady 或标记失败，
绝不用假图冒充推理结果。
"""
from __future__ import annotations

import io
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from . import config
from .adapter import ModelNotReady, adapter as default_adapter


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"
    INVALID = "invalid"


class InvalidFile(ValueError):
    pass


@dataclass
class Job:
    job_id: str
    filename: str
    state: JobState = JobState.QUEUED
    attempts: int = 0
    max_attempts: int = 2
    error: str = ""
    result_name: str = ""
    created_at: float = field(default_factory=time.time)
    # 原始载荷与参数不进入 to_dict，避免泄漏
    raw_data: bytes = field(default=b"", repr=False)
    params: object = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "state": self.state.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "error": self.error,
            "result_name": self.result_name,
        }


def validate_upload(filename: str, data: bytes) -> None:
    """校验扩展名、大小与真实图像可解码性。失败抛 InvalidFile。"""
    ext = Path(filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise InvalidFile(f"不支持的文件类型：{ext or '(无)'}")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise InvalidFile(f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidFile(f"无法解码图像：{exc}") from exc
    with Image.open(io.BytesIO(data)) as probe:
        w, h = probe.size
        if w > config.MAX_IMAGE_EDGE or h > config.MAX_IMAGE_EDGE:
            raise InvalidFile(f"图像边长超过 {config.MAX_IMAGE_EDGE}px 上限")


class MattingService:
    def __init__(self, adapter_instance=None, autostart: bool = True) -> None:
        self.adapter = adapter_instance or default_adapter
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if autostart:
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    # ---- 单图 ----
    def submit_single(self, filename: str, data: bytes, params) -> dict:
        if self.adapter.state.value != "READY":
            raise ModelNotReady(self.adapter.detail)
        validate_upload(filename, data)
        image = Image.open(io.BytesIO(data))
        alpha_raw = self.adapter.predict(image)
        return self.render_and_save(filename, image, alpha_raw, params)

    # ---- 批量 ----
    def submit_batch(self, files: list[tuple[str, bytes]], params) -> list[Job]:
        jobs: list[Job] = []
        for filename, data in files:
            job = Job(
                job_id=uuid.uuid4().hex[:12],
                filename=filename,
                max_attempts=config.MAX_RETRY_ATTEMPTS + 1,
                raw_data=data,
                params=params,
            )
            try:
                validate_upload(filename, data)
            except InvalidFile as exc:
                job.state = JobState.INVALID
                job.error = str(exc)
            with self._lock:
                self._jobs[job.job_id] = job
            jobs.append(job)
        return jobs

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.state in (JobState.QUEUED, JobState.RUNNING):
            job.state = JobState.CANCELED
            job.error = "用户取消"
            return True
        return False

    def retry(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.state not in (JobState.FAILED, JobState.INVALID):
            return False
        job.state = JobState.QUEUED
        job.error = ""
        job.attempts = 0
        return True

    # ---- 处理 ----
    def render_and_save(self, filename: str, image: Image.Image, alpha_raw, params) -> dict:
        from .export_png import alpha_to_image, composite_rgba, save_transparent_png
        from .postprocess import apply_alpha

        alpha = apply_alpha(alpha_raw, params)
        stem = Path(filename).stem
        cutout_name = f"{stem}_cutout_{uuid.uuid4().hex[:6]}.png"
        mask_name = f"{stem}_mask_{uuid.uuid4().hex[:6]}.png"
        save_transparent_png(composite_rgba(image, alpha), config.RESULTS_DIR / cutout_name)
        alpha_to_image(alpha).save(config.RESULTS_DIR / mask_name, format="PNG")
        return {
            "cutout_name": cutout_name,
            "mask_name": mask_name,
            "width": image.width,
            "height": image.height,
        }

    def process_job(self, job: Job) -> None:
        """同步处理一个任务，含重试。供 worker 与测试共用。"""
        while job.attempts < job.max_attempts:
            job.state = JobState.RUNNING
            try:
                if self.adapter.state.value != "READY":
                    raise ModelNotReady(self.adapter.detail)
                image = Image.open(io.BytesIO(job.raw_data))
                alpha_raw = self.adapter.predict(image)
                info = self.render_and_save(job.filename, image, alpha_raw, job.params)
                job.result_name = info["cutout_name"]
                job.state = JobState.DONE
                job.error = ""
                return
            except Exception as exc:
                job.attempts += 1
                job.error = str(exc)
        job.state = JobState.FAILED

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = [j for j in self._jobs.values() if j.state == JobState.QUEUED]
            if not pending:
                self._stop.wait(0.2)
                continue
            job = sorted(pending, key=lambda j: j.created_at)[0]
            if job.state == JobState.QUEUED:
                self.process_job(job)
            self._stop.wait(0.05)
