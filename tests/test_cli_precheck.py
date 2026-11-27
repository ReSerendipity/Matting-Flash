"""S4 验收：GPU 前置校验在模型加载前 0 秒拦截，映射为退出码 3。"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.adapter import ModelNotReady
from app.integrated_app import cli, cli_service


def fake_run(stdout: str):
    return lambda *a, **kw: SimpleNamespace(stdout=stdout, returncode=0)


def test_precheck_passes_with_enough_free_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATTING_ALLOW_CPU", raising=False)
    monkeypatch.setattr(cli_service.subprocess, "run", fake_run("6144\n"))
    cli_service._gpu_precheck()


def test_precheck_blocks_low_free_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATTING_ALLOW_CPU", raising=False)
    monkeypatch.setattr(cli_service.subprocess, "run", fake_run("3000\n"))
    with pytest.raises(ModelNotReady, match="空闲显存.*< 5GB"):
        cli_service._gpu_precheck()


@pytest.mark.parametrize("error", [FileNotFoundError("no nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 2)])
def test_precheck_blocks_when_cuda_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.delenv("MATTING_ALLOW_CPU", raising=False)

    def boom(*a, **kw):
        raise error

    monkeypatch.setattr(cli_service.subprocess, "run", boom)
    with pytest.raises(ModelNotReady, match="CUDA 不可用"):
        cli_service._gpu_precheck()


def test_cpu_escape_hatch_warns_but_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATTING_ALLOW_CPU", "1")

    def boom(*a, **kw):
        raise FileNotFoundError("no nvidia-smi")

    monkeypatch.setattr(cli_service.subprocess, "run", boom)
    warnings: list[str] = []
    cli_service._gpu_precheck(warnings)
    assert any("MATTING_ALLOW_CPU" in w for w in warnings)


def test_cli_maps_precheck_to_exit_3(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from PIL import Image

    image = tmp_path / "cat.png"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(image, format="PNG")

    def boom(warnings=None):
        raise ModelNotReady("空闲显存 2.10GB < 5GB 阈值")

    monkeypatch.setattr(cli_service, "_gpu_precheck", boom)
    monkeypatch.setattr(
        cli_service,
        "_adapter",
        lambda: SimpleNamespace(state=SimpleNamespace(value="READY"), detail="fake"),
    )
    from app.integrated_app import cli_remote

    monkeypatch.setattr(cli_remote, "discover", lambda **kw: (None, "直调"))
    code = cli.main(["single", "-i", str(image), "--json"])
    import json

    payload = json.loads(capsys.readouterr().out.strip())
    assert code == 3 and payload["code"] == "NOT_READY" and "2.10GB" in payload["error"]

    # 非推理命令不应被 GPU 前置校验误拦截
    assert cli.main(["model", "check", "--json"]) == 0
