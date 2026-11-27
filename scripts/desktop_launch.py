"""桌面壳启动入口：接收 --host/--port，启动现有 FastAPI 服务。

与 Tauri 壳的双向契约：
- `--host` / `--port` 必填（端口由壳先绑定 127.0.0.1:0 抓空闲端口后传入，脚本不再探测）。
- 开头打印一行 `监听端口: N`，作为装后动态端口的唯一日志锚点。
- 不调用 `webbrowser.open`（WebView 即浏览器），也不做 pip 安装（runtime 随包）。
- stdout/stderr 由壳重定向到 `logs/desktop_python_*.log`。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Matting-Flash 桌面壳后端启动器")
    parser.add_argument("--host", required=True, help="监听地址（壳固定传 127.0.0.1）")
    parser.add_argument("--port", required=True, type=int, help="监听端口（由壳预分配）")
    args = parser.parse_args(argv)

    app_root = Path(__file__).resolve().parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    # 让 app.adapter 的「自动加载模型」KPI 锚点在服务日志里可见。
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # 模型与结果目录：优先采纳壳注入的环境变量，回退仓内目录（开发态）。
    weights_dir = os.environ.get("MATTING_WEIGHTS_DIR", str(app_root / "weights"))
    results_dir = os.environ.get("MATTING_RESULTS_DIR", str(app_root / "results"))

    # 先打印端口锚点：即使后续导入/加载模型失败，壳也能在日志里定位本次端口。
    print(f"监听端口: {args.port}", flush=True)
    print(f"[Matting-Flash] 应用目录：{app_root}", flush=True)
    print(f"[Matting-Flash] 权重目录：{weights_dir}", flush=True)
    print(f"[Matting-Flash] 结果目录：{results_dir}", flush=True)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[Matting-Flash] 警告：正在监听 {args.host}，服务将对网络可达；桌面壳只会传 127.0.0.1。",
            file=sys.stderr,
            flush=True,
        )

    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[Matting-Flash] 警告：结果目录不可写（{exc}），推理产物将无法保存。", flush=True)

    try:
        import uvicorn

        from app.main import app
    except Exception as exc:  # noqa: BLE001 - 打包 runtime 缺依赖时必须明确失败
        print(f"[Matting-Flash] 启动失败：导入依赖异常（{exc}）", file=sys.stderr, flush=True)
        return 2

    # 端口文件：供 CLI 发现常驻服务（显式 URL > 环境变量 > 本文件）。
    # 安装态可用 MATTING_PORT_FILE 指到用户数据目录，默认写应用根（.gitignore 已忽略）。
    port_file = Path(os.environ.get("MATTING_PORT_FILE", str(app_root / ".server_port")))
    try:
        port_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = port_file.with_name(f".{port_file.name}.{os.getpid()}.tmp")
        tmp.write_text(str(args.port), encoding="ascii")
        os.replace(tmp, port_file)
        print(f"[Matting-Flash] 端口文件：{port_file}", flush=True)
    except OSError as exc:
        print(f"[Matting-Flash] 警告：端口文件写入失败（{exc}），CLI 自动发现将不可用。", flush=True)

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    finally:
        # 服务正常退出时清理陈旧端口；被强杀时 discover() 的 2s 探测会忽略它。
        try:
            if port_file.is_file() and port_file.read_text(encoding="ascii").strip() == str(args.port):
                port_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
