"""批量队列：无效文件、重试耗尽、取消、模型未就绪。"""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app import config
from app.adapter import ModelState
from app.postprocess import PostParams
from app.queue import InvalidFile, JobState, MattingService, validate_upload


class FakeReadyAdapter:
    state = ModelState.READY
    detail = "fake"

    def predict(self, image):
        w, h = image.size
        a = np.zeros((h, w), dtype=np.float32)
        a[:, : w // 2] = 1.0
        return a


class FakeNotReadyAdapter:
    state = ModelState.NOT_READY
    detail = "no weights"


def _png(w=16, h=16, color=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_validate_rejects_bad_extension():
    with pytest.raises(InvalidFile):
        validate_upload("note.txt", b"hello")


def test_validate_rejects_corrupt_image():
    with pytest.raises(InvalidFile):
        validate_upload("broken.png", b"this is not an image")


def test_validate_accepts_real_png(png_bytes):
    validate_upload("ok.png", png_bytes)  # 不抛错即通过


def test_validate_rejects_oversize(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    with pytest.raises(InvalidFile):
        validate_upload("big.png", _png())


def test_invalid_file_marked_on_submit():
    svc = MattingService(FakeNotReadyAdapter(), autostart=False)
    jobs = svc.submit_batch([("a.txt", b"x"), ("bad.png", b"nope")], PostParams())
    assert all(j.state == JobState.INVALID for j in jobs)
    assert all(j.error for j in jobs)


def test_model_not_ready_fails_after_retries():
    svc = MattingService(FakeNotReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("ok.png", _png())], PostParams())
    job.max_attempts = 2
    svc.process_job(job)
    assert job.state == JobState.FAILED
    assert job.attempts == 2, "应重试至上限后失败"
    assert "no weights" in job.error


def test_cancel_queued_job():
    svc = MattingService(FakeReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("ok.png", _png())], PostParams())
    assert svc.cancel(job.job_id) is True
    assert job.state == JobState.CANCELED


def test_successful_job_writes_transparent_png(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    svc = MattingService(FakeReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("ok.png", _png())], PostParams(feather_radius=1))
    svc.process_job(job)
    assert job.state == JobState.DONE, job.error
    from PIL import Image as I

    with I.open(tmp_path / job.result_name) as im:
        assert im.mode == "RGBA"
        assert im.size == (16, 16)


def test_retry_resets_failed_job():
    svc = MattingService(FakeNotReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("ok.png", _png())], PostParams())
    job.max_attempts = 1
    svc.process_job(job)
    assert job.state == JobState.FAILED
    assert svc.retry(job.job_id) is True
    assert job.state == JobState.QUEUED and job.attempts == 0
    assert svc.retry("missing-id") is False
