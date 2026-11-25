"""API 冒烟（开发模式）：未就绪状态必须可见，且不得伪造推理成功。"""
from __future__ import annotations

import asyncio
import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return TestClient(app)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (9, 9, 9)).save(buf, format="PNG")
    return buf.getvalue()


def test_status_reports_not_ready(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] in ("NOT_READY", "READY", "FAILED")
    # 本机未下载权重且未安装 torch，首版交付必须是 NOT_READY
    assert body["state"] == "NOT_READY"
    assert "weights_dir" in body and "defaults" in body


def test_single_matting_refuses_when_not_ready(client):
    r = client.post(
        "/api/matting/single",
        files={"file": ("a.png", _png_bytes(), "image/png")},
        data={"params": "{}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "MODEL_NOT_READY"


def test_batch_accepts_valid_file_but_never_fakes_result(client):
    r = client.post(
        "/api/batch",
        files=[("files", ("a.png", _png_bytes(), "image/png"))],
        data={"params": "{}"},
    )
    assert r.status_code == 200
    (job,) = r.json()["jobs"]
    # 模型未就绪时任务只会 queued→failed 或 invalid；不允许出现 done 假成功
    assert job["state"] in ("queued", "running", "failed", "invalid")


def test_index_page_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Matting-Flash" in r.text


def test_result_file_blocks_traversal(client):
    r = client.get("/results/..%2F..%2Fconfig.py")
    assert r.status_code == 404


def test_status_endpoint_stays_responsive_during_inference(tmp_path, monkeypatch):
    """阻塞推理必须在线程池执行：单图请求进行中，状态端点仍能立即返回。"""
    import httpx

    from app import main as main_mod
    from app.adapter import ModelState

    class SlowAdapter:
        state = ModelState.READY
        detail = "fake"

        def predict(self, image):
            time.sleep(0.4)
            w, h = image.size
            a = np.zeros((h, w), dtype=np.float32)
            a[:, : w // 2] = 1.0
            return a

    monkeypatch.setattr(main_mod.service, "adapter", SlowAdapter())
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    payload = _png_bytes()

    async def scenario():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            timings: dict[str, tuple[float, int]] = {}

            async def single():
                t0 = time.time()
                r = await ac.post(
                    "/api/matting/single",
                    files={"file": ("a.png", payload, "image/png")},
                    data={"params": "{}"},
                )
                timings["single"] = (time.time() - t0, r.status_code)

            async def status():
                await asyncio.sleep(0.05)
                t0 = time.time()
                r = await ac.get("/api/status")
                timings["status"] = (time.time() - t0, r.status_code)

            await asyncio.gather(single(), status())
            return timings

    timings = asyncio.run(scenario())
    assert timings["single"][1] == 200
    assert timings["status"][1] == 200
    assert timings["single"][0] >= 0.35, "单图请求应受 slow 推理支配"
    assert timings["status"][0] < 0.3, "状态端点不得等待推理线程"
