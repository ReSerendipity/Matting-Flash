"""Result 信封 / Human-JSON 双输出 / 语义退出码 / stdout 隔离。

契约（供 AI agent 自动化调用）：
- JSON 模式只向真实 stdout 写一行 JSON，第三方 import 的 print 全部转到 stderr；
- 退出码：0 成功 / 1 运行时错误 / 2 用法错误 / 3 未就绪；
- `--json` 由 cli.main 预提取，任意位置生效。
"""
from __future__ import annotations

import contextlib
import enum
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_NOT_READY = 3

_ERROR_EXIT = {
    "USAGE_ERROR": EXIT_USAGE_ERROR,
    "NOT_READY": EXIT_NOT_READY,
    "INVALID_FILE": EXIT_USAGE_ERROR,
}


@dataclass
class Result:
    success: bool
    data: Any = None
    error: str = ""
    code: str = ""
    command: str = ""
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, data: Any, command: str = "", warnings: list[str] | None = None) -> Result:
        return cls(success=True, data=data, command=command, warnings=warnings or [])

    @classmethod
    def fail(
        cls, error: str, code: str = "BUSINESS_ERROR", command: str = ""
    ) -> Result:
        return cls(success=False, error=error, code=code, command=command)

    @property
    def exit_code(self) -> int:
        if self.success:
            return EXIT_OK
        return _ERROR_EXIT.get(self.code, EXIT_RUNTIME_ERROR)


_REAL_STDOUT = sys.stdout
_JSON_MODE = False


@contextlib.contextmanager
def begin_json_mode() -> Iterator[None]:
    """隔离第三方 stdout print：JSON 输出改用进入本函数前的真实 stdout。"""
    global _JSON_MODE, _REAL_STDOUT
    if _JSON_MODE:
        yield
        return
    _REAL_STDOUT = sys.stdout
    _JSON_MODE = True
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        _JSON_MODE = False


def end_json_mode() -> None:
    """兼容旧调用：上下文管理器已负责恢复，这里只清空标志。"""
    global _JSON_MODE
    _JSON_MODE = False


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (Path, datetime, date)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


def emit(result: Result, as_json: bool) -> int:
    """渲染并退出；返回语义退出码。JSON 模式只写一行严格 JSON。"""
    payload = {
        "success": result.success,
        "data": _jsonable(result.data),
        "error": result.error,
        "code": result.code,
        "command": result.command,
        "warnings": result.warnings,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=_REAL_STDOUT)
        return result.exit_code

    out = _REAL_STDOUT if _JSON_MODE else sys.stdout
    if result.success:
        data = result.data
        if isinstance(data, dict):
            for key in ("message", "state", "path", "output_dir", "server_url"):
                if key in data:
                    print(f"{key}: {data[key]}", file=out)
            if "results" in data and isinstance(data["results"], list):
                for item in data["results"]:
                    print(json.dumps(_jsonable(item), ensure_ascii=False), file=out)
            if "jobs" in data and isinstance(data["jobs"], list):
                for item in data["jobs"]:
                    print(json.dumps(_jsonable(item), ensure_ascii=False), file=out)
        elif data is not None:
            print(data, file=out)
        for warning in result.warnings:
            print(f"[warn] {warning}", file=sys.stderr)
    else:
        print(f"[{result.code or 'ERROR'}] {result.error}", file=sys.stderr)
    return result.exit_code


__all__ = [
    "EXIT_NOT_READY",
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "EXIT_USAGE_ERROR",
    "Result",
    "begin_json_mode",
    "emit",
    "end_json_mode",
]
