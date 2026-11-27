"""CLI 远程层：复用常驻 FastAPI 服务，避免每次调用重新加载 444MB 权重。

端点契约已用 httpx 实测（2026-11-26）：
- `GET /api/status` → 200 JSON（含 state/version/ready）；无 `/health`；
- `POST /api/matting/single` → multipart `file` + form `params`（JSON 字符串），
  返回 JSON `{state:DONE, preview:{result:"data:image/png;base64,..."}, cutout_name,...}`；
  缺权重时 409 `{detail:{code:"MODEL_NOT_READY",...}}`；
- `POST /api/batch` → multipart 重复 `files` + form `params`，返回 queued jobs；
- `GET /results/{name}` → `image/png`；
- 无 CSRF 握手（status 响应无 X-CSRF-Token / Set-Cookie）。

服务发现：显式 `--server` > `MATTING_CLI_SERVER` > 端口文件；2s 短超时。
任何异常一律回落直调，行为不退化（见 tests/test_cli_remote.py 的 6 条降级路径）。
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from app import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT_FILE = PROJECT_ROOT / ".server_port"


class RemoteError(RuntimeError):
    """远端不可用或返回了无法解析的结果。"""


class RemoteNotReady(RemoteError):
    """远端明确报告 MODEL_NOT_READY。"""


def _client(base_url: str, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)


def _check_status(payload: Any, base_url: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or "state" not in payload:
        raise RemoteError(f"{base_url} 的 /api/status 响应缺少 state 字段")
    version = payload.get("version")
    if version and str(version) != __version__:
        raise RemoteError(f"服务版本不符（客户端 {__version__} / 服务 {version}）")
    return payload


def probe(base_url: str, timeout: float = 2.0) -> dict[str, Any]:
    """探测服务身份与版本；失败抛 RemoteError。"""
    try:
        with _client(base_url, timeout=timeout) as client:
            resp = client.get("/api/status")
            resp.raise_for_status()
            payload = resp.json()
    except RemoteError:
        raise
    except Exception as exc:  # noqa: BLE001 - 所有远端异常统一转成降级信号
        raise RemoteError(f"无法连接常驻服务 {base_url}: {exc}") from exc
    return _check_status(payload, base_url)


def discover(
    explicit: str | None = None,
    *,
    env_var: str = "MATTING_CLI_SERVER",
    port_file: Path | None = None,
    timeout: float = 2.0,
) -> tuple[str | None, str]:
    """按优先级发现可用服务；返回 (base_url, 说明)。任何失败返回 (None, 原因)。"""
    if explicit:
        try:
            probe(explicit, timeout)
            return explicit, f"使用显式服务 {explicit}"
        except RemoteError as exc:
            return None, f"显式服务不可用，回落直调: {exc}"

    from_env = os.environ.get(env_var)
    if from_env:
        try:
            probe(from_env, timeout)
            return from_env, f"使用环境变量服务 {from_env}"
        except RemoteError as exc:
            return None, f"环境变量服务不可用，回落直调: {exc}"

    candidate_file = port_file or DEFAULT_PORT_FILE
    if not candidate_file.is_file():
        return None, "未发现端口文件，直调"
    try:
        raw = candidate_file.read_text(encoding="ascii").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= 65535):
            raise RemoteError(f"端口文件内容非法: {raw!r}")
        url = f"http://127.0.0.1:{raw}"
        probe(url, timeout)
        return url, f"从端口文件发现服务 {url}"
    except (OSError, RemoteError) as exc:
        return None, f"端口文件不可用，回落直调: {exc}"


def _params_json(params: Any) -> str:
    return json.dumps(asdict(params), ensure_ascii=False)


def _decode_png(data_url: str, label: str) -> bytes:
    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise RemoteError(f"{label} 不是 PNG data URL")
    try:
        return base64.b64decode(data_url[len(prefix):], validate=True)
    except Exception as exc:  # noqa: BLE001 - 非法 base64 必须触发直调降级
        raise RemoteError(f"{label} base64 解码失败: {exc}") from exc


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("detail", {})
    except Exception:  # noqa: BLE001 - 非 JSON 错误体也按普通远端错误回落
        detail = {}
    code = detail.get("code") if isinstance(detail, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else resp.text[:200]
    if code == "MODEL_NOT_READY":
        raise RemoteNotReady(str(message))
    raise RemoteError(f"服务返回 HTTP {resp.status_code}: {message}")


def single(
    base_url: str, image: str, output_dir: str, params: Any
) -> dict[str, Any]:
    """通过常驻服务做一次单图抠图，返回与 cli_service.single 兼容的结构。"""
    source = Path(image)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise RemoteNotReady(f"输入图片不存在: {source}")

    with _client(base_url) as client:
        try:
            with source.open("rb") as fh:
                resp = client.post(
                    "/api/matting/single",
                    files={"file": (source.name, fh, "image/png")},
                    data={"params": _params_json(params)},
                )
        except httpx.HTTPError as exc:
            # 连接中断/超时属于降级信号，必须由 CLI 层回落直调，不能冒泡成业务错误。
            raise RemoteError(f"单图请求失败: {exc}") from exc
        _raise_for_error(resp)
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - 非 JSON 成功体触发降级
            raise RemoteError(f"单图响应不是 JSON: {exc}") from exc
        if payload.get("state") != "DONE":
            raise RemoteError(f"单图响应 state 异常: {payload.get('state')}")
        preview = payload.get("preview") or {}
        cutout_name = payload.get("cutout_name") or f"{source.stem}_cutout.png"
        mask_name = payload.get("mask_name") or f"{source.stem}_mask.png"
        results = []
        for key, name, label in (
            ("result", cutout_name, "抠图结果"),
            ("mask", mask_name, "蒙版"),
        ):
            data_url = preview.get(key, "")
            if not data_url:
                continue
            target = destination / name
            target.write_bytes(_decode_png(data_url, label))
            results.append({"kind": "cutout" if key == "result" else "mask", "path": str(target.resolve())})
        if not any(item["kind"] == "cutout" for item in results):
            raise RemoteError("单图响应缺少 preview.result")

    return {
        "mode": "remote",
        "server_url": base_url.rstrip("/"),
        "input": str(source.resolve()),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "results": results,
        "output_dir": str(destination.resolve()),
    }


def batch(
    base_url: str, images: list[str], output_dir: str, params: Any
) -> dict[str, Any]:
    """逐张复用服务；单张失败记录到 failures，不影响其它图片。"""
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    started = time.perf_counter()
    for image in images:
        try:
            results.append(single(base_url, image, output_dir, params))
        except RemoteError as exc:
            failures.append({"input": image, "error": str(exc)})
    return {
        "mode": "remote",
        "server_url": base_url.rstrip("/"),
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
        "output_dir": str(Path(output_dir).resolve()),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def results_download(base_url: str, name: str, destination: str) -> dict[str, Any]:
    """通过 /results/{name} 下载产物；拒绝路径穿越。"""
    if not name or Path(name).name != name:
        raise ValueError("结果名必须是纯文件名")
    target = Path(destination)
    if target.is_dir() or str(destination).endswith(("/", "\\")):
        target = target / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with _client(base_url) as client:
        try:
            resp = client.get(f"/results/{name}")
        except httpx.HTTPError as exc:
            raise RemoteError(f"结果下载失败: {exc}") from exc
        _raise_for_error(resp)
        if "image/png" not in resp.headers.get("content-type", ""):
            raise RemoteError(f"/results/{name} 返回了非图片内容")
        target.write_bytes(resp.content)
    return {"name": name, "path": str(target.resolve()), "size": target.stat().st_size}


__all__ = [
    "DEFAULT_PORT_FILE",
    "RemoteError",
    "RemoteNotReady",
    "batch",
    "discover",
    "probe",
    "results_download",
    "single",
]
