"""S2 验收：Result 信封、双输出、退出码、全子命令族与跨进程任务台账。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.integrated_app import cli, cli_service


class FakeAdapter:
    def __init__(self, state: str = "READY") -> None:
        self.state = type("State", (), {"value": state})()
        self.detail = "fake adapter ready" if state == "READY" else "fake adapter missing weights"

    def predict(self, image: Image.Image) -> np.ndarray:
        return np.full((image.height, image.width), 0.8, dtype=np.float32)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(cli_service.config, "RESULTS_DIR", results)
    monkeypatch.setattr(cli_service, "_adapter", lambda: FakeAdapter())
    monkeypatch.setattr(cli_service, "_gpu_precheck", lambda warnings=None: None)
    return tmp_path


def make_png(path: Path, size: tuple[int, int] = (32, 24)) -> Path:
    Image.new("RGB", size, (10, 120, 200)).save(path, format="PNG")
    return path


def one_json(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, f"stdout 必须只有一行 JSON，实际: {out[:3]}"
    return json.loads(out[0])


def test_single_json_success_and_artifacts(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    image = make_png(cli_env / "cat.png")
    output = cli_env / "out"
    code = cli.main(["single", "-i", str(image), "-o", str(output), "--json"])
    payload = one_json(capsys)
    assert code == 0
    assert payload["success"] is True
    assert payload["command"] == "single"
    assert payload["data"]["width"] == 32
    paths = [item["path"] for item in payload["data"]["results"]]
    assert len(paths) == 2
    assert all(Path(p).is_file() for p in paths)


def test_json_flag_works_after_subcommand(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["presets", "list", "--json"])
    payload = one_json(capsys)
    assert code == 0 and payload["data"]["limits"]["infer_size"] == 1024


def test_not_ready_maps_to_exit_3(cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli_service, "_adapter", lambda: FakeAdapter("NOT_READY"))
    image = make_png(cli_env / "cat.png")
    code = cli.main(["single", "-i", str(image), "--json"])
    payload = one_json(capsys)
    assert code == 3 and payload["code"] == "NOT_READY" and "missing weights" in payload["error"]


def test_usage_error_maps_to_exit_2(capsys) -> None:
    code = cli.main(["single", "--json"])
    payload = one_json(capsys)
    assert code == 2 and payload["success"] is False


def test_batch_jobs_ledger_and_retry(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_png(cli_env / "a.png", (24, 24))
    make_png(cli_env / "b.png", (24, 24))
    code = cli.main(
        ["batch", "--input-dir", str(cli_env), "-o", str(cli_env / "out"), "--json"]
    )
    payload = one_json(capsys)
    assert code == 0 and payload["data"]["succeeded"] == 2
    assert (cli_service._ledger_path()).is_file()

    assert cli.main(["jobs", "list", "--json"]) == 0
    jobs = one_json(capsys)["data"]["jobs"]
    assert len(jobs) == 2
    job_id = jobs[0]["job_id"]

    assert cli.main(["jobs", "show", job_id, "--json"]) == 0
    assert one_json(capsys)["data"]["job_id"] == job_id

    assert cli.main(["jobs", "cancel", job_id, "--json"]) == 2  # 已完成不可取消
    assert one_json(capsys)["code"] == "USAGE_ERROR"

    assert cli.main(["jobs", "retry", job_id, "--json"]) == 2  # 已完成不可重试
    assert one_json(capsys)["code"] == "USAGE_ERROR"


def test_failed_job_can_be_retried(cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    source = make_png(cli_env / "bad.png")
    cli_service._ledger_add(
        [
            {
                "job_id": "failed001",
                "filename": source.name,
                "input": str(source),
                "state": "failed",
                "error": "previous error",
                "result_name": "",
                "created_at": 0,
            }
        ]
    )
    assert cli.main(["jobs", "retry", "failed001", "--json"]) == 0
    payload = one_json(capsys)["data"]
    assert payload["new_job"]["retried_from"] == "failed001"
    assert payload["new_job"]["state"] == "done"


def test_results_download_traversal_rejected(cli_env: Path, capsys) -> None:
    assert cli.main(["results", "download", "../secret.png", "--json"]) == 2
    assert one_json(capsys)["code"] == "USAGE_ERROR"
    assert cli.main(["results", "download", "", "--json"]) == 2
    assert one_json(capsys)["code"] == "USAGE_ERROR"


def test_stdout_isolated_from_third_party_print(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = cli_service.presets

    def noisy() -> dict:
        print("import-noise-should-not-pollute-json")
        return original()

    monkeypatch.setattr(cli_service, "presets", noisy)
    assert cli.main(["presets", "list", "--json"]) == 0
    captured = capsys.readouterr()
    assert "import-noise" in captured.err
    assert "import-noise" not in captured.out
    assert json.loads(captured.out.strip())["success"] is True


def test_system_config_redacts_nested_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["system", "config", "--json"]) == 0
    raw = capsys.readouterr().out
    assert "do-not-leak" not in raw and "secret-value" not in raw
    assert raw.count("***REDACTED***") == 2


def test_engines_and_models_are_json_listable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["engines", "list", "--json"]) == 0
    assert one_json(capsys)["data"]["engines"][0]["name"] == "BiRefNet"
    assert cli.main(["models", "list", "--json"]) == 0
    assert one_json(capsys)["data"]["models"][0]["state"] in ("READY", "NOT_READY")
