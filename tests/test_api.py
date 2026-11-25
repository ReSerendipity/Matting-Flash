"""API 冒烟（开发模式）：未就绪状态必须可见，且不得伪造推理成功。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image


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
