"""版本一致性断言：主仓与桌面壳三件套必须同版本。

发版 bump 时任意一处漏改，本测试立刻红：
- 主仓 `pyproject.toml` / `app.__version__`
- 壳 `desktop/package.json` / `desktop/src-tauri/tauri.conf.json` / `desktop/src-tauri/Cargo.toml`
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parent.parent

# pyproject / Cargo.toml 的 [project]/[package] 版本行；避免引入 3.11+ 才有的 tomllib。
_VERSION_LINE = re.compile(r'(?m)^\s*version\s*=\s*"([^"]+)"')


def _manifest_version(path: Path) -> str:
    match = _VERSION_LINE.search(path.read_text(encoding="utf-8"))
    assert match, f"{path} 缺少 version 行"
    return match.group(1)


def test_package_version_matches_pyproject() -> None:
    assert __version__ == _manifest_version(ROOT / "pyproject.toml")


def test_desktop_version_triplet_matches_main() -> None:
    main_version = _manifest_version(ROOT / "pyproject.toml")

    desktop_pkg = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    tauri_conf = json.loads(
        (ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    cargo = _manifest_version(ROOT / "desktop" / "src-tauri" / "Cargo.toml")

    assert desktop_pkg["version"] == main_version, "desktop/package.json 版本与主仓不一致"
    assert tauri_conf["version"] == main_version, "tauri.conf.json 版本与主仓不一致"
    assert cargo == main_version, "src-tauri/Cargo.toml 版本与主仓不一致"
