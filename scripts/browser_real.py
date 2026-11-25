"""Matting-Flash 浏览器真实链路验收脚本。

阶段 A（--prepare-only）只验证 NOT_READY 页面语义、文件预览与大图/批量准备态，
不触发任何推理；真实阶段通过页面控件上传、观察 RUNNING 态、抠图、下载并校验透明 PNG。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT = "Matting-Flash"
DEFAULT_PORT = 8872
VIEWPORT = {"width": 1440, "height": 1000}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Matting-Flash 浏览器验收（--prepare-only 只验证 NOT_READY 语义）"
    )
    parser.add_argument("--base-url", default=f"http://127.0.0.1:{DEFAULT_PORT}")
    parser.add_argument("--input", default="results/real-20261124-214136/input.png")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--timeout", type=float, default=900.0, help="秒；默认 900")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只验证页面状态、大图/批量准备态与按钮禁用语义，禁止触发真实任务",
    )
    return parser.parse_args(argv)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_sample(path: Path, size: tuple[int, int], color=(40, 80, 120)) -> Path:
    """生成本地样本（不下载）；用于大图与批量准备态检查。"""
    from PIL import Image, ImageDraw

    if not path.is_file():
        img = Image.new("RGB", size, color)
        draw = ImageDraw.Draw(img)
        w, h = size
        draw.ellipse([w * 0.2, h * 0.15, w * 0.8, h * 0.85], fill=(235, 215, 190))
        draw.rectangle([w * 0.35, h * 0.45, w * 0.65, h * 0.95], fill=(60, 60, 70))
        img.save(path, format="PNG")
    return path


def validate_screenshot(path: Path, viewport: dict) -> dict:
    from PIL import Image, ImageStat

    image = Image.open(path).convert("RGB")
    stat = ImageStat.Stat(image)
    stddev = sum(stat.stddev) / 3.0
    ok = image.size == (viewport["width"], viewport["height"]) and stddev > 3.0
    return {"file": path.name, "size": list(image.size), "stddev": round(stddev, 2), "ok": ok}


def main(argv=None) -> int:
    args = parse_args(argv)
    base = args.base_url.rstrip("/")
    port = urlparse(base).port

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        print(f"[blocked] 样本不存在：{input_path}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "browser-prep" if args.prepare_only else "browser-real"
    out_dir = Path(args.output_dir) if args.output_dir else Path("results") / f"{prefix}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"[blocked] Playwright 不可用：{exc}", file=sys.stderr)
        return 2

    started = now_iso()
    t0 = time.time()
    checks = {
        "status_ready": False,
        "status_not_ready": False,
        "notready_badge": False,
        "notready_note": False,
        "runbutton_disabled_in_not_ready": False,
        "file_preview_visible": False,
        "big_sample_generated": False,
        "big_image_preview": False,
        "batch_preview": False,
        "progress_ui_idle": False,
        "progress_hook_present": False,
        "no_job_created": False,
        "no_third_party_requests": False,
        "screenshots_valid": False,
        "real_job_started": False,
        "single_progress_visible": False,
        "completion_feedback": False,
        "terminal_success": False,
        "result_visible": False,
        "download_nonempty": False,
        "artifact_valid": False,
        "no_demo": False,
    }
    big_path = make_sample(out_dir / "prep-big-3200x2000.png", (3200, 2000))
    small_path = make_sample(out_dir / "prep-small-640x480.png", (640, 480), (20, 120, 60))
    checks["big_sample_generated"] = big_path.is_file() and small_path.is_file()
    console_lines: list[str] = []
    page_errors: list[str] = []
    network: list[dict] = []
    screenshots: list[dict] = []
    artifacts: dict = {}
    status_payload: dict = {}
    error_msg = ""

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[blocked] 无法启动系统 Edge：{exc}", file=sys.stderr)
            return 2

        ctx = browser.new_context(viewport=VIEWPORT, accept_downloads=True, locale="zh-CN")
        page = ctx.new_page()
        page.on("console", lambda m: console_lines.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("response", lambda r: network.append(
            {"method": r.request.method, "url": r.url, "status": r.status,
             "resource_type": r.request.resource_type}
        ))

        def same_origin(url: str) -> bool:
            p = urlparse(url)
            if p.scheme not in ("http", "https"):
                return True
            return p.hostname in ("127.0.0.1", "localhost", "::1") and p.port == port

        def shot(name: str) -> None:
            path = out_dir / name
            page.screenshot(path=str(path), full_page=False)
            screenshots.append(validate_screenshot(path, VIEWPORT))

        try:
            status_payload = ctx.request.get(f"{base}/api/status", timeout=30_000).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[blocked] 状态端点不可用：{exc}", file=sys.stderr)
            browser.close()
            return 2

        ready = status_payload.get("state") == "READY"
        checks["status_ready"] = ready
        checks["status_not_ready"] = not ready
        if args.prepare_only and ready:
            error_msg = "准备阶段要求 NOT_READY 开发模式，但 /api/status 返回 state=READY"
        if not args.prepare_only and not ready:
            print(f"[blocked] 真实阶段要求 READY，当前 {status_payload.get('state')}", file=sys.stderr)
            browser.close()
            return 2

        page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_function(
            "document.getElementById('modelDetail').textContent.indexOf('正在检查') === -1",
            timeout=30_000,
        )
        layout = page.evaluate(
            "(() => { const m = document.querySelector('main'); const r = m ? m.getBoundingClientRect() : null;"
            " return { title: document.title, scrollWidth: document.documentElement.scrollWidth,"
            " innerWidth: window.innerWidth, mainWidth: r ? r.width : 0, mainHeight: r ? r.height : 0,"
            " mainRight: r ? r.right : 0 }; })()"
        )
        badge_text = page.locator("#modelBadge").inner_text()
        detail_text = page.locator("#modelDetail").inner_text()
        note_text = page.locator("#notReadyNote").inner_text()
        checks["no_demo"] = "DEMO" not in badge_text + detail_text + note_text

        if args.prepare_only:
            checks["notready_badge"] = "NOT_READY" in badge_text
            checks["notready_note"] = "模型未就绪" in note_text and "NOT_READY" in note_text
            shot("01-notready.png")
        else:
            if not badge_text.startswith("MODEL · READY"):
                error_msg = f"页面徽标非 READY：{badge_text}"
            shot("01-ready.png")

        page.set_input_files("#file", str(input_path))
        page.wait_for_function(
            "document.getElementById('fileInfo').textContent.indexOf('已选择') !== -1",
            timeout=30_000,
        )
        page.wait_for_function(
            "(() => { const v = document.getElementById('vOrig');"
            " return v.querySelector('img') !== null; })()",
            timeout=30_000,
        )
        file_info = page.locator("#fileInfo").inner_text()
        checks["file_preview_visible"] = "已选择 1 个文件" in file_info

        if args.prepare_only:
            checks["runbutton_disabled_in_not_ready"] = page.locator("#runBtn").is_disabled()
            shot("02-preview-notready.png")

            # >2K 大图：页面预览必须完整解码（只预览，不触发推理）
            page.set_input_files("#file", str(big_path))
            page.wait_for_function(
                "document.getElementById('fileInfo').textContent.indexOf('已选择 1 个文件') !== -1",
                timeout=30_000,
            )
            page.wait_for_function(
                "(() => { const im = document.getElementById('vOrig').querySelector('img');"
                " return !!im && im.naturalWidth > 0; })()",
                timeout=30_000,
            )
            big_dims = page.evaluate(
                "(() => { const im = document.getElementById('vOrig').querySelector('img');"
                " return im ? [im.naturalWidth, im.naturalHeight] : [0, 0]; })()"
            )
            checks["big_image_preview"] = bool(
                big_dims[0] >= 2048
                and big_dims[1] > 0
                and abs(big_dims[0] / big_dims[1] - 1.6) < 0.02
                and not page.locator("#submitErr").inner_text().strip()
            )
            shot("03-big-preview-notready.png")

            # 批量准备：3 个文件同时选中，仍不得创建任务
            page.set_input_files("#file", [str(input_path), str(big_path), str(small_path)])
            page.wait_for_function(
                "document.getElementById('fileInfo').textContent.indexOf('已选择 3 个文件') !== -1",
                timeout=30_000,
            )
            checks["batch_preview"] = (
                page.locator("#runBtn").is_disabled()
                and not page.locator("#submitErr").inner_text().strip()
            )
            shot("04-batch-preview-notready.png")

            checks["progress_ui_idle"] = page.evaluate(
                "(() => { const el = document.getElementById('runState');"
                " return !!el && el.hidden === true && !!document.getElementById('runTimer'); })()"
            )
            checks["progress_hook_present"] = page.evaluate(
                "typeof beginRun === 'function' && typeof endRun === 'function'"
            )
            posts = [n for n in network if n["method"] == "POST"
                     and ("/api/matting/single" in n["url"] or "/api/batch" in n["url"])]
            checks["no_job_created"] = not posts and not page.locator("#submitErr").inner_text().strip()
        else:
            page.click("#runBtn")
            checks["real_job_started"] = True
            try:
                page.wait_for_selector("#runState", state="visible", timeout=30_000)
                running_label = page.locator("#runLabel").inner_text()
                checks["single_progress_visible"] = "RUNNING" in running_label
                shot("02-running.png")
            except Exception as exc:  # noqa: BLE001 - 记录为断言失败，不中断下载校验
                error_msg = error_msg or f"未观察到 RUNNING 进度态：{exc}"
            page.wait_for_selector("#dl", state="visible", timeout=int(args.timeout * 1000))
            checks["terminal_success"] = True
            for vid in ("#vMask", "#vResult"):
                page.wait_for_function(
                    f"document.querySelector('{vid} img') !== null", timeout=60_000
                )
            checks["result_visible"] = True
            done_text = page.locator("#doneNote").inner_text()
            checks["completion_feedback"] = (
                page.locator("#doneNote").is_visible() and "完成" in done_text
            )
            shot("03-result.png")
            with page.expect_download(timeout=60_000) as download_info:
                page.click("#dl")
            download = download_info.value
            target = out_dir / (download.suggested_filename or "cutout.png")
            download.save_as(str(target))
            size = target.stat().st_size
            checks["download_nonempty"] = size > 0
            artifacts = {"file": target.name, "bytes": size, "sha256": sha256_file(target)}
            try:
                from PIL import Image

                with Image.open(target) as img:
                    artifacts["width"], artifacts["height"] = img.size
                    if img.mode in ("RGBA", "LA"):
                        alpha = img.getchannel("A")
                        extrema = alpha.getextrema()
                        artifacts["alpha_extrema"] = list(extrema)
                        src_size = Image.open(input_path).size
                        artifacts["input_size"] = list(src_size)
                        checks["artifact_valid"] = (
                            img.size == src_size
                            and extrema[0] <= 5
                            and extrema[1] >= 250
                        )
            except Exception as exc:  # noqa: BLE001
                error_msg = error_msg or f"产物校验失败：{exc}"
            shot("04-downloaded.png")

        third_party = [n for n in network if not same_origin(n["url"])]
        checks["no_third_party_requests"] = not third_party
        checks["screenshots_valid"] = all(s["ok"] for s in screenshots) and bool(screenshots)
        if layout["scrollWidth"] > layout["innerWidth"] + 1:
            error_msg = error_msg or "页面存在横向溢出，截图右缘可能被裁切"
        if layout["mainWidth"] <= 0 or layout["mainHeight"] <= 0:
            error_msg = error_msg or "页面主体不可见"

        ctx.close()
        browser.close()

    required = (
        ["status_not_ready", "notready_badge", "notready_note",
         "runbutton_disabled_in_not_ready", "file_preview_visible",
         "big_sample_generated", "big_image_preview", "batch_preview",
         "progress_ui_idle", "progress_hook_present", "no_job_created"]
        if args.prepare_only
        else ["status_ready", "no_demo", "file_preview_visible", "real_job_started",
              "terminal_success", "result_visible", "download_nonempty", "artifact_valid"]
    )
    ok = (
        not error_msg
        and all(checks[key] for key in required)
        and checks["screenshots_valid"]
        and checks["no_third_party_requests"]
    )

    finished = now_iso()
    acceptance = {
        "project": PROJECT,
        "base_url": base,
        "started_at": started,
        "finished_at": finished,
        "prepare_only": bool(args.prepare_only),
        "browser": {"channel": "msedge", "headless": True, "viewport": VIEWPORT},
        "input": {"path": str(input_path), "bytes": input_path.stat().st_size,
                  "sha256": sha256_file(input_path)},
        "checks": checks,
        "artifacts": artifacts,
        "screenshots": screenshots,
        "service_log": "见 console.log 与服务端日志",
        "ok": ok,
        "error": error_msg,
    }
    metrics = {
        "project": PROJECT,
        "mode": "prepare-only" if args.prepare_only else "real",
        "base_url": base,
        "port": port,
        "duration_s": round(time.time() - t0, 2),
        "status_payload": status_payload,
        "badge": badge_text,
        "detail": detail_text,
        "layout": layout,
        "requests": len(network),
        "third_party_requests": len(third_party),
        "exit_code": 0 if ok else 1,
    }
    write_json(out_dir / "acceptance.json", acceptance)
    write_json(out_dir / "metrics.json", metrics)
    write_json(out_dir / "network.json", network)
    (out_dir / "console.log").write_text("\n".join(console_lines), encoding="utf-8")
    (out_dir / "pageerror.log").write_text("\n".join(page_errors), encoding="utf-8")

    print(f"[{PROJECT}] {'OK' if ok else 'FAIL'} -> {out_dir}")
    for key, value in checks.items():
        print(f"  {key}: {value}")
    if error_msg:
        print(f"  error: {error_msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
