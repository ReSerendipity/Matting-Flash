"""批量队列：无效文件、重试耗尽、取消、模型未就绪、串行与结果隔离。"""
from __future__ import annotations

import io
import threading
import time

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


class SlowAdapter(FakeReadyAdapter):
    """记录并发度，用于验证 worker 串行。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[int, int]] = []
        self._lock = threading.Lock()

    def predict(self, image):
        w, h = image.size
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((w, h))
        time.sleep(self.delay)
        a = np.zeros((h, w), dtype=np.float32)
        a[:, : max(1, w // 2)] = 1.0
        with self._lock:
            self.active -= 1
        return a


class GatedAdapter(FakeReadyAdapter):
    """predict 卡在闸门上，便于在 running 中取消。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.entered = threading.Event()
        self.gate = threading.Event()

    def predict(self, image):
        w, h = image.size
        self.calls.append((w, h))
        self.entered.set()
        self.gate.wait(3.0)
        a = np.zeros((h, w), dtype=np.float32)
        a[:, : max(1, w // 2)] = 1.0
        return a


class FlakyAdapter(FakeReadyAdapter):
    """指定尺寸恒定失败，其余成功：验证失败任务不阻塞队列。"""

    def __init__(self, fail_size: tuple[int, int] = (16, 16)) -> None:
        self.fail_size = fail_size
        self.calls = 0

    def predict(self, image):
        self.calls += 1
        if image.size == self.fail_size:
            raise RuntimeError("fake predict boom")
        return super().predict(image)


def _png(w=16, h=16, color=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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
    svc.process_job(job)
    assert job.state == JobState.CANCELED, "取消后的任务不得被 process 复活"
    assert job.result_name == ""


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


def test_retry_creates_new_job_and_keeps_failed_record():
    svc = MattingService(FakeNotReadyAdapter(), autostart=False)
    (old,) = svc.submit_batch([("ok.png", _png())], PostParams())
    old.max_attempts = 1
    svc.process_job(old)
    assert old.state == JobState.FAILED

    new = svc.retry(old.job_id)
    assert new is not None
    assert new.job_id != old.job_id, "重试必须生成新 job，不得复用原记录"
    assert new.state == JobState.QUEUED
    assert new.filename == old.filename and new.raw_data == old.raw_data
    assert new.attempts == 0 and new.max_attempts == old.max_attempts
    assert old.state == JobState.FAILED, "原失败记录保留，便于对照原因"
    assert new.job_id in {j["job_id"] for j in svc.list_jobs()}

    assert svc.retry(old.job_id) is not None, "同一失败记录可再次派生新任务"
    assert svc.retry(new.job_id) is None, "排队中的任务不可重试"
    assert svc.retry("missing-id") is None


def test_retry_of_invalid_file_stays_invalid():
    svc = MattingService(FakeNotReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("bad.png", b"nope")], PostParams())
    assert job.state == JobState.INVALID
    new = svc.retry(job.job_id)
    assert new is not None and new.state == JobState.INVALID and new.error == job.error
    assert new.job_id != job.job_id


def test_done_job_cannot_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    svc = MattingService(FakeReadyAdapter(), autostart=False)
    (job,) = svc.submit_batch([("ok.png", _png())], PostParams())
    svc.process_job(job)
    assert job.state == JobState.DONE
    assert svc.retry(job.job_id) is None
    assert svc.cancel(job.job_id) is False


def test_worker_runs_serially_and_every_job_gets_own_result(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    sizes = [(40, 24), (28, 18), (20, 20)]
    ad = SlowAdapter(delay=0.05)
    svc = MattingService(ad, autostart=True)
    try:
        jobs = svc.submit_batch(
            [(f"img{i}.png", _png(w, h)) for i, (w, h) in enumerate(sizes)], PostParams()
        )
        assert _wait_for(lambda: all(j.state == JobState.DONE for j in jobs))
    finally:
        svc.stop()

    assert ad.max_active == 1, "串行队列：同一时刻只允许一次推理"
    assert len(ad.calls) == 3
    assert len({j.result_name for j in jobs}) == 3, "任务间结果名不得重复"
    for job, (w, h) in zip(jobs, sizes, strict=True):
        with Image.open(tmp_path / job.result_name) as im:
            assert im.size == (w, h), f"{job.filename} 拿到了别的任务的结果"
            assert im.mode == "RGBA"


def test_cancel_running_discards_result_and_queue_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    ad = GatedAdapter()
    svc = MattingService(ad, autostart=True)
    try:
        (a, b) = svc.submit_batch([("a.png", _png(24, 16)), ("b.png", _png(18, 12))], PostParams())
        assert ad.entered.wait(2.0), "worker 应已开始处理首个任务"
        assert svc.cancel(a.job_id) is True
        ad.gate.set()
        assert _wait_for(lambda: b.state == JobState.DONE)
    finally:
        ad.gate.set()
        svc.stop()

    assert a.state == JobState.CANCELED
    assert a.result_name == "", "取消的任务不得落盘结果"
    assert b.state == JobState.DONE
    assert len(list(tmp_path.glob("*.png"))) == 2, "只应存在 b 的 cutout 与 mask"
    assert len(ad.calls) == 2, "取消只丢弃 a 的结果，不打断 b 的推理"


def test_failed_job_does_not_block_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    ad = FlakyAdapter(fail_size=(16, 16))
    svc = MattingService(ad, autostart=False)
    try:
        (bad, good) = svc.submit_batch(
            [("bad.png", _png(16, 16)), ("good.png", _png(24, 12))], PostParams()
        )
        bad.max_attempts = 2
        svc.start()
        assert _wait_for(lambda: bad.state == JobState.FAILED and good.state == JobState.DONE)
    finally:
        svc.stop()

    assert bad.attempts == 2 and "fake predict boom" in bad.error
    assert bad.result_name == ""
    assert good.state == JobState.DONE
    with Image.open(tmp_path / good.result_name) as im:
        assert im.size == (24, 12)
