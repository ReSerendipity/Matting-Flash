"""S3 验收：服务发现优先级、6 种降级路径、HTTP 契约与端口文件写入。"""
from __future__ import annotations

import base64
import io
import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from PIL import Image

from app import __version__
from app.integrated_app import cli, cli_remote, cli_service

ROOT = Path(__file__).resolve().parent.parent
PNG = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
buf = io.BytesIO()
PNG.save(buf, format="PNG")
PNG_BYTES = buf.getvalue()
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


def status_json() -> dict:
    return {"state": "READY", "version": __version__, "ready": True}


def patch_client(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    def factory(base_url: str, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            trust_env=False,
        )

    monkeypatch.setattr(cli_remote, "_client", factory)


def test_probe_success_and_contract_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_client(monkeypatch, lambda req: httpx.Response(200, json=status_json()))
    assert cli_remote.probe("http://127.0.0.1:9")["state"] == "READY"

    patch_client(monkeypatch, lambda req: httpx.Response(200, json={"state": "READY", "version": "9.9.9"}))
    with pytest.raises(cli_remote.RemoteError, match="版本不符"):
        cli_remote.probe("http://127.0.0.1:9")

    patch_client(monkeypatch, lambda req: httpx.Response(200, text="not json"))
    with pytest.raises(cli_remote.RemoteError):
        cli_remote.probe("http://127.0.0.1:9")


def test_discover_priority_explicit_env_portfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_client(monkeypatch, lambda req: httpx.Response(200, json=status_json()))
    port_file = tmp_path / ".server_port"
    port_file.write_text("8123", encoding="ascii")
    monkeypatch.setenv("MATTING_CLI_SERVER", "http://env-host:1")
    assert cli_remote.discover("http://explicit:2", port_file=port_file)[0] == "http://explicit:2"

    monkeypatch.delenv("MATTING_CLI_SERVER")
    assert cli_remote.discover(port_file=port_file)[0] == "http://127.0.0.1:8123"

    monkeypatch.setenv("MATTING_CLI_SERVER", "http://env-host:1")
    assert cli_remote.discover(port_file=port_file)[0] == "http://env-host:1"


@pytest.mark.parametrize(
    "case",
    ["explicit_unreachable", "explicit_version", "env_unreachable", "no_port_file", "stale_port", "bad_status"],
)
def test_six_degradation_paths_return_none(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port_file = tmp_path / ".server_port"
    explicit = None
    if case == "explicit_unreachable":
        def boom(base_url: str, timeout: float = 30.0) -> httpx.Client:
            raise OSError("connection refused")
        monkeypatch.setattr(cli_remote, "_client", boom)
        explicit = "http://dead:1"
    elif case == "explicit_version":
        patch_client(monkeypatch, lambda req: httpx.Response(200, json={"state": "READY", "version": "0.0.0"}))
        explicit = "http://old:1"
    elif case == "env_unreachable":
        def boom2(base_url: str, timeout: float = 30.0) -> httpx.Client:
            raise OSError("env host down")
        monkeypatch.setattr(cli_remote, "_client", boom2)
        monkeypatch.setenv("MATTING_CLI_SERVER", "http://env-down:1")
    elif case == "stale_port":
        port_file.write_text("not-a-port", encoding="ascii")
    elif case == "bad_status":
        patch_client(monkeypatch, lambda req: httpx.Response(200, json={"unexpected": True}))
        port_file.write_text("8123", encoding="ascii")
    # no_port_file: 故意不创建端口文件
    base, reason = cli_remote.discover(explicit=explicit, port_file=port_file)
    assert base is None
    assert reason


def test_remote_single_saves_png_and_uses_multipart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status_json())
        assert req.url.path == "/api/matting/single"
        body = req.content.decode("utf-8", errors="ignore")
        seen["params"] = body
        assert "feather_radius" in body and "cat.png" in body
        return httpx.Response(
            200,
            json={
                "state": "DONE",
                "width": 8,
                "height": 8,
                "cutout_name": "cat_cutout.png",
                "mask_name": "cat_mask.png",
                "preview": {"result": PNG_DATA_URL, "mask": PNG_DATA_URL},
            },
        )

    patch_client(monkeypatch, handler)
    image = tmp_path / "cat.png"
    image.write_bytes(PNG_BYTES)
    out = tmp_path / "out"
    data = cli_remote.single("http://mock:1", str(image), str(out), cli_service.PostParams())
    assert data["mode"] == "remote" and data["width"] == 8
    assert (out / "cat_cutout.png").read_bytes() == PNG_BYTES
    assert json.loads(next(line for line in seen["params"].splitlines() if "feather_radius" in line))["feather_radius"] == 0.0


def test_transport_error_becomes_remote_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    patch_client(monkeypatch, handler)
    image = tmp_path / "cat.png"
    image.write_bytes(PNG_BYTES)
    with pytest.raises(cli_remote.RemoteError, match="单图请求失败"):
        cli_remote.single("http://mock:1", str(image), str(tmp_path), cli_service.PostParams())


def test_remote_409_maps_to_not_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status_json())
        return httpx.Response(
            409, json={"detail": {"code": "MODEL_NOT_READY", "message": "缺权重"}}
        )

    patch_client(monkeypatch, handler)
    image = tmp_path / "cat.png"
    image.write_bytes(PNG_BYTES)
    with pytest.raises(cli_remote.RemoteNotReady, match="缺权重"):
        cli_remote.single("http://mock:1", str(image), str(tmp_path), cli_service.PostParams())


def test_remote_batch_and_results_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/status":
            return httpx.Response(200, json=status_json())
        if req.url.path == "/api/matting/single":
            return httpx.Response(
                200,
                json={
                    "state": "DONE",
                    "width": 8,
                    "height": 8,
                    "preview": {"result": PNG_DATA_URL},
                    "cutout_name": "x.png",
                },
            )
        if req.url.path == "/results/x.png":
            return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    patch_client(monkeypatch, handler)
    image = tmp_path / "x.png"
    image.write_bytes(PNG_BYTES)
    data = cli_remote.batch("http://mock:1", [str(image), str(image)], str(tmp_path / "out"), cli_service.PostParams())
    assert data["succeeded"] == 2 and data["failed"] == 0
    downloaded = cli_remote.results_download("http://mock:1", "x.png", str(tmp_path / "dl"))
    assert Path(downloaded["path"]).read_bytes() == PNG_BYTES


def test_cli_falls_back_to_direct_when_remote_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(cli_service.config, "RESULTS_DIR", results)
    monkeypatch.setattr(
        cli_service,
        "_adapter",
        lambda: SimpleNamespace(
            state=SimpleNamespace(value="READY"),
            detail="fake",
            predict=lambda image: np.full((image.height, image.width), 0.7, dtype=np.float32),
        ),
    )
    monkeypatch.setattr(cli_remote, "discover", lambda **kw: ("http://mock:1", "mock"))
    monkeypatch.setattr(
        cli_remote, "single", lambda *a, **kw: (_ for _ in ()).throw(cli_remote.RemoteError("mock down"))
    )
    image = tmp_path / "cat.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(image, format="PNG")
    code = cli.main(["single", "-i", str(image), "--json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 0 and payload["data"]["mode"] == "local"
    assert any("回落直调" in w for w in payload["warnings"])


def test_launcher_writes_and_cleans_port_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port_file = tmp_path / ".server_port"
    monkeypatch.setenv("MATTING_PORT_FILE", str(port_file))
    seen: dict[str, str] = {}

    def fake_run(app, host, port, log_level):  # noqa: ANN001 - 替换 uvicorn.run
        seen["port"] = port_file.read_text(encoding="ascii")

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=fake_run))
    module = runpy.run_path(str(ROOT / "scripts" / "desktop_launch.py"), run_name="probe_launcher")
    assert module["main"](["--host", "127.0.0.1", "--port", "8765"]) == 0
    assert seen["port"] == "8765"
    assert not port_file.exists(), "服务正常退出后端口文件必须清理"
    assert "端口文件" in capsys.readouterr().out
