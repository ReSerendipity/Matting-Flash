#!/usr/bin/env python3
"""Matting-Flash 统一命令行入口（人 / AI agent 均可调用）。

用法：
    python -m app.integrated_app.cli --help
    python -m app.integrated_app.cli single -i photo.jpg -o results --json
    python -m app.integrated_app.cli batch --input-dir photos -o results
    python -m app.integrated_app.cli jobs list --json
    python -m app.integrated_app.cli results download <name> -o out/
    python -m app.integrated_app.cli system status --json

契约见 docs/ADR-CLI.md：Result 信封 / 双输出 / 0-1-2-3 退出码 / --json 任意位置。
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys

# 脚本直跑（安装包 shim）无包上下文时把项目根注入 sys.path。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.adapter import ModelNotReady  # noqa: E402
from app.integrated_app.cli_output import (  # noqa: E402
    EXIT_OK,
    Result,
    begin_json_mode,
    emit,
)


def _add_params(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--feather-radius", type=int, default=0, help="边缘羽化半径")
    parser.add_argument("--threshold", type=float, default=None, help="alpha 阈值（0.0-1.0）")
    parser.add_argument("--binarize", action="store_true", help="alpha 二值化")
    parser.add_argument("--erode", type=int, default=0, help="腐蚀迭代次数")
    parser.add_argument("--dilate", type=int, default=0, help="膨胀迭代次数")


def _add_local(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=None, help="复用常驻服务地址（S3 启用）")
    parser.add_argument("--no-remote", action="store_true", help="强制直调，不复用常驻服务")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="matting", description="Matting-Flash CLI（本地抠图工作台）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_single = sub.add_parser("single", help="单图抠图")
    p_single.add_argument("-i", "--input", required=True, help="输入图片路径")
    p_single.add_argument("-o", "--output-dir", default="results", help="产物目录（默认 results/）")
    _add_params(p_single)
    _add_local(p_single)

    p_batch = sub.add_parser("batch", help="批量抠图")
    p_batch.add_argument("-i", "--input", nargs="*", default=[], help="输入图片（可多张）")
    p_batch.add_argument("--input-dir", default=None, help="输入目录（扩展名受支持的文件）")
    p_batch.add_argument("-o", "--output-dir", default="results", help="产物目录（默认 results/）")
    _add_params(p_batch)
    _add_local(p_batch)

    p_batch_resume = sub.add_parser("resume", aliases=["batch-resume"], help="重跑失败的批量任务")
    p_batch_resume.add_argument("-o", "--output-dir", default="results")

    p_jobs = sub.add_parser("jobs", help="任务查询与操作")
    jobs_sub = p_jobs.add_subparsers(dest="action", required=True)
    jobs_list = jobs_sub.add_parser("list", help="列出任务")
    jobs_list.add_argument("--state", default=None, help="按状态过滤")
    jobs_show = jobs_sub.add_parser("show", help="查看单个任务")
    jobs_show.add_argument("job_id")
    jobs_cancel = jobs_sub.add_parser("cancel", help="取消任务")
    jobs_cancel.add_argument("job_id")
    jobs_retry = jobs_sub.add_parser("retry", help="重试失败任务")
    jobs_retry.add_argument("job_id")
    jobs_retry.add_argument("-o", "--output-dir", default="results")

    p_results = sub.add_parser("results", help="结果产物管理")
    results_sub = p_results.add_subparsers(dest="action", required=True)
    results_sub.add_parser("list", help="列出固定 results/ 目录中的产物")
    dl = results_sub.add_parser("download", aliases=["copy"], help="下载（复制）一个产物")
    dl.add_argument("name", help="results/ 下的纯文件名")
    dl.add_argument("-o", "--output-dir", default=".", help="目标目录或文件")
    _add_local(dl)

    p_presets = sub.add_parser("presets", help="后处理预设与限额")
    p_presets.add_subparsers(dest="action", required=True).add_parser("list")

    p_models = sub.add_parser("models", help="模型与权重盘查")
    models_sub = p_models.add_subparsers(dest="action", required=True)
    models_sub.add_parser("list")
    models_sub.add_parser("show")

    p_model = sub.add_parser("model", help="模型运维")
    model_sub = p_model.add_subparsers(dest="action", required=True)
    model_sub.add_parser("check")
    model_sub.add_parser("download")

    p_engines = sub.add_parser("engines", help="引擎列表")
    p_engines.add_subparsers(dest="action", required=True).add_parser("list")

    p_system = sub.add_parser("system", help="环境与配置")
    system_sub = p_system.add_subparsers(dest="action", required=True)
    system_sub.add_parser("status")
    system_sub.add_parser("gpu")
    system_sub.add_parser("config")

    p_serve = sub.add_parser("serve", help="兜底：独立拉起 FastAPI 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    return parser


def _action(args: argparse.Namespace, as_json: bool) -> str:
    return f"{args.command} {args.action}" if getattr(args, "action", None) else str(args.command)


def _remote_for(args: argparse.Namespace, warnings: list[str]) -> str | None:
    """服务发现：--no-remote 强制直调；显式 URL > 环境变量 > 端口文件。"""
    from app.integrated_app import cli_remote

    if getattr(args, "no_remote", False):
        warnings.append("已指定 --no-remote，强制直调")
        return None
    base, note = cli_remote.discover(explicit=getattr(args, "server", None))
    warnings.append(note)
    return base


def _dispatch(args: argparse.Namespace, as_json: bool) -> Result:
    from app.integrated_app import cli_service as svc

    cmd = args.command
    action = getattr(args, "action", None)
    warnings: list[str] = []
    try:
        if cmd == "single":
            params = svc.build_params(args)
            base = _remote_for(args, warnings)
            if base:
                from app.integrated_app import cli_remote

                try:
                    data = cli_remote.single(base, args.input, args.output_dir, params)
                except cli_remote.RemoteError as exc:
                    warnings.append(f"远端单图失败，回落直调: {exc}")
                    data = svc.single(args.input, args.output_dir, params, warnings=warnings)
            else:
                data = svc.single(args.input, args.output_dir, params, warnings=warnings)
            return Result.ok(data, command=cmd, warnings=warnings)
        if cmd == "batch":
            params = svc.build_params(args)
            base = _remote_for(args, warnings)
            if base:
                from app.integrated_app import cli_remote

                data = cli_remote.batch(
                    base, [str(p) for p in svc._inputs(args.input, args.input_dir)],
                    args.output_dir, params,
                )
            else:
                data = svc.batch(
                    args.input, args.input_dir, args.output_dir, params, warnings=warnings
                )
            result = Result.ok(data, command=cmd, warnings=warnings)
            if data["failed"]:
                result.success = False
                result.code = "BUSINESS_ERROR"
                result.error = f"{data['failed']} 张失败"
            return result
        if cmd in ("resume", "batch-resume"):
            return Result.ok(svc.batch_resume(args.output_dir), command="batch resume")
        if cmd == "jobs" and action == "list":
            return Result.ok(svc.jobs_list(args.state), command="jobs list")
        if cmd == "jobs" and action == "show":
            return Result.ok(svc.jobs_show(args.job_id), command="jobs show")
        if cmd == "jobs" and action == "cancel":
            return Result.ok(svc.jobs_cancel(args.job_id), command="jobs cancel")
        if cmd == "jobs" and action == "retry":
            return Result.ok(svc.jobs_retry(args.job_id, args.output_dir), command="jobs retry")
        if cmd == "results" and action == "list":
            return Result.ok(svc.results_list(), command="results list")
        if cmd == "results" and action in ("download", "copy"):
            base = _remote_for(args, warnings)
            if base:
                from app.integrated_app import cli_remote

                try:
                    data = cli_remote.results_download(base, args.name, args.output_dir)
                except cli_remote.RemoteError as exc:
                    warnings.append(f"远端下载失败，回落本地: {exc}")
                    data = svc.results_download(args.name, args.output_dir)
            else:
                data = svc.results_download(args.name, args.output_dir)
            return Result.ok(data, command="results download", warnings=warnings)
        if cmd == "presets" and action == "list":
            return Result.ok(svc.presets(), command="presets")
        if cmd == "models" and action == "list":
            return Result.ok(svc.models_list(), command="models list")
        if cmd == "models" and action == "show":
            return Result.ok(svc.models_show(), command="models show")
        if cmd == "model" and action == "check":
            return Result.ok(svc.model_check(), command="model check")
        if cmd == "model" and action == "download":
            return Result.ok(svc.model_download(), command="model download")
        if cmd == "engines" and action == "list":
            return Result.ok(svc.engines_list(), command="engines list")
        if cmd == "system" and action == "status":
            return Result.ok(svc.system_status(), command="system status")
        if cmd == "system" and action == "gpu":
            return Result.ok(svc.system_gpu(), command="system gpu")
        if cmd == "system" and action == "config":
            return Result.ok(svc.system_config(), command="system config")
        if cmd == "serve":
            return Result.ok(svc.serve(args.host, args.port), command="serve")
    except ModelNotReady as exc:
        return Result.fail(str(exc), "NOT_READY", _action(args, as_json))
    except FileNotFoundError as exc:
        return Result.fail(str(exc), "NOT_READY", _action(args, as_json))
    except ValueError as exc:
        return Result.fail(str(exc), "USAGE_ERROR", _action(args, as_json))
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一收口，异常不再静默
        return Result.fail(str(exc), "BUSINESS_ERROR", _action(args, as_json))
    return Result.fail(f"未知命令: {_action(args, as_json)}", "USAGE_ERROR")


def _main_impl(raw: list[str], as_json: bool) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:
        if not exc.code:
            return EXIT_OK  # --help 已由 argparse 输出到 stdout
        return emit(
            Result.fail("参数解析失败（见 stderr 的 usage）", "USAGE_ERROR"), as_json
        )
    return emit(_dispatch(args, as_json), as_json)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw
    if as_json:
        raw = [a for a in raw if a != "--json"]
    # 必须在导入 cli_service / 引擎之前隔离 stdout，避免第三方 print 污染 JSON。
    with begin_json_mode() if as_json else contextlib.nullcontext():
        return _main_impl(raw, as_json)


if __name__ == "__main__":
    sys.exit(main())
