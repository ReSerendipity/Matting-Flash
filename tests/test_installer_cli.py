"""S1 验收：CLI shim 与 HKCU PATH 注册/卸载契约（等价语言模拟 + 静态钉住）。

Windows 工具环境禁止直接调 cmd.exe / 改注册表，因此 .cmd 与 NSIS 逻辑用
与本 NSIS 脚本逐行等价的 Python 函数模拟；文件层面的契约用静态断言钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRY = r"C:\Users\Doro\AppData\Local\Programs\Matting-Flash"


def nsis_add_to_user_path(path: str, entry: str = ENTRY) -> str:
    """等价模拟 setup.nsi::AddToUserPath（先归一化尾部分隔符 + ";...;" 边界匹配）。"""
    path = path.rstrip(";")
    wrapped = f";{path};"
    if f";{entry};" in wrapped:
        return path
    if not path:
        return entry
    return path + ";" + entry


def nsis_remove_from_user_path(path: str, entry: str = ENTRY) -> str:
    """等价模拟 setup.nsi::un.RemoveFromUserPath（循环精确移除 + 折叠分号）。"""
    original = path
    path = path.rstrip(";")
    wrapped = f";{path};"
    if f";{entry};" not in wrapped:
        return original
    while f";{entry};" in wrapped:
        wrapped = wrapped.replace(f";{entry};", ";")
    while ";;" in wrapped:
        wrapped = wrapped.replace(";;", ";")
    return wrapped[1:-1]


@pytest.mark.parametrize(
    ("original", "expected_after_add"),
    [
        ("", ENTRY),
        (r"C:\Windows", rf"C:\Windows;{ENTRY}"),
        (r"C:\Windows;", rf"C:\Windows;{ENTRY}"),
        (ENTRY, ENTRY),
        (rf"{ENTRY};", ENTRY),
        (rf"C:\Tools;{ENTRY};C:\Windows", rf"C:\Tools;{ENTRY};C:\Windows"),
        (rf"{ENTRY}-Extra", rf"{ENTRY}-Extra;{ENTRY}"),
        (r"C:\A;C:\B", rf"C:\A;C:\B;{ENTRY}"),
    ],
)
def test_add_to_user_path_boundaries(original: str, expected_after_add: str) -> None:
    assert nsis_add_to_user_path(original) == expected_after_add
    # 原本不含本仓条目时，安装后卸载应精确复原原始 PATH；
    # 原本已含时，卸载会一并移除该条目（CLI 已不存在，符合预期）。
    if ENTRY not in original.split(";"):
        assert nsis_remove_from_user_path(expected_after_add) == original.rstrip(";")


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (ENTRY, ""),
        (rf"C:\Windows;{ENTRY}", r"C:\Windows"),
        (rf"{ENTRY};C:\Windows", r"C:\Windows"),
        (rf"C:\A;{ENTRY};C:\B", r"C:\A;C:\B"),
        (rf"{ENTRY};{ENTRY};C:\B", r"C:\B"),
        (rf"{ENTRY}-Extra;C:\B", rf"{ENTRY}-Extra;C:\B"),
        (rf"C:\Windows\{ENTRY}\child", rf"C:\Windows\{ENTRY}\child"),
        ("", ""),
    ],
)
def test_remove_from_user_path_exact(original: str, expected: str) -> None:
    assert nsis_remove_from_user_path(original) == expected


def test_setup_nsi_registers_cli_path_and_shim() -> None:
    text = (ROOT / "scripts/installer/setup.nsi").read_bytes().decode("utf-8-sig")
    assert '!include "StrFunc.nsh"' in text
    assert "${Using:StrFunc} StrStr" in text
    assert "${Using:StrFunc} UnStrStr" in text
    assert "${Using:StrFunc} UnStrRep" in text
    assert "Function AddToUserPath" in text
    assert "Function un.RemoveFromUserPath" in text
    assert '  File "matting.cmd"' in text
    assert "  Call AddToUserPath" in text
    assert "  Call un.RemoveFromUserPath" in text
    assert '"$SYSDIR\\cmd.exe"' in text
    assert "Matting-Flash CLI.lnk" in text
    # 用户数据删除只在交互卸载明确选择「否」时发生，不得无条件删除
    assert 'RMDir /r "${USER_DATA_DIR}"' in text
    assert '${If} $KEEP_DATA == "0"' in text


def test_shim_is_ascii_only_and_preserves_exit_code() -> None:
    raw = (ROOT / "scripts/installer/matting.cmd").read_bytes()
    assert raw.isascii(), "cmd 必须 ASCII-only，否则活动代码页会乱码"
    text = raw.decode("ascii")
    assert 'set "APPROOT=%~dp0app"' in text
    assert 'set "PYEXE=%APPROOT%\\runtime\\python.exe"' in text
    assert 'set "CLIPY=%APPROOT%\\app\\integrated_app\\cli.py"' in text
    assert "HF_HUB_OFFLINE=1" in text
    assert '"%PYEXE%" "%CLIPY%" %*' in text
    assert "exit /b %ERRORLEVEL%" in text
