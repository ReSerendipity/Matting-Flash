"""U 阶段 L1 静态契约：Rust 传参 ↔ NSIS 检测 ↔ 前端提示 ↔ CI 验收三处成对存在。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8-sig")


def test_rust_updater_uses_single_full_installer() -> None:
    src = read("desktop/src-tauri/src/updater.rs")
    assert 'const ASSET_TEMPLATE: &str = "Matting-Flash-Setup-v{version}.exe"' in src
    assert 'const SHA_TEMPLATE: &str = "Matting-Flash-Setup-v{version}.exe.sha256"' in src
    assert "只认精确完整 exe 名" in src
    assert ".zip" not in src.split("fn resolve_assets")[1].split("fn extract_hex64")[0]
    assert '"update-installing"' in src
    assert '"update-progress"' in src
    assert "std::process::exit(0)" in src
    assert "launch_installer(&exe_path)" in src


def test_installer_args_are_silent_and_relaunch() -> None:
    src = read("desktop/src-tauri/src/updater.rs")
    body = src.split("fn installer_args()")[1].split("}")[0]
    assert '"/S"' in body and '"/RELAUNCH"' in body


def test_cargo_has_updater_dependencies() -> None:
    cargo = read("desktop/src-tauri/Cargo.toml")
    for dep in ("reqwest", "futures", "semver", "sha2"):
        assert dep in cargo
    # 本仓无签名密钥：用自写更新器 + SHA256，不配置未签名的 Tauri updater 插件。
    assert "tauri-plugin-updater" not in cargo


def test_nsis_relaunches_when_flag_present() -> None:
    nsi = read("scripts/installer/setup.nsi")
    assert '${StrStr} $0 "$CMDLINE" "/RELAUNCH"' in nsi
    assert 'Exec \'"$INSTDIR\\${APP_EXE}"\'' in nsi
    write_uninstaller = nsi.index("WriteUninstaller")
    relaunch = nsi.index("/RELAUNCH")
    assert relaunch > write_uninstaller, "自动重启段必须在 WriteUninstaller 之后"
    # 安装段不得递归删除整个安装根（覆盖安装须保留用户安装的程序文件）
    assert "RMDir /r \"$INSTDIR\"\n" not in nsi.split("Section \"Uninstall\"")[0]


def test_frontend_bridges_update_events() -> None:
    bridge = read("desktop/src/desktop-bridge.js")
    assert 'listen("update-installing"' in bridge
    assert "mfdu-body" in bridge and "mfdu-actions" in bridge

    loading = read("desktop/src/index.html")
    for token in ('id="mfdu-body"', 'id="mfdu-actions"', "check_update", "start_update", "update-progress"):
        assert token in loading

    app = read("static/index.html")
    assert "check_update" in app
    assert "start_update" in app
    assert 'listen("update-installing"' in app
    assert 'listen("update-error"' in app


def test_ci_acceptance_workflow_is_present() -> None:
    ci = read(".github/workflows/verify-relaunch.yml")
    assert "'/S','/RELAUNCH'" in ci
    assert "$appPid" in ci and "$pid =" not in ci
    assert "gh release download" in ci and '--repo "${{ github.repository }}"' in ci
    assert "Matting-Flash-Setup-v$ver.exe" in ci
    assert "pyproject.toml" in ci


def test_no_legacy_increment_route() -> None:
    assert not (ROOT / ".github/workflows/shell-update.yml").exists()
    updater = read("desktop/src-tauri/src/updater.rs")
    # 唯一的 zip 出现处是单测 fixture，用来证明旧增量资产被拒绝；
    # 生产解析函数只认 ASSET_TEMPLATE（由 test_rust_updater_uses_single_full_installer 钉住）。
    assert updater.count(".zip") == 2
    assert "delta" not in updater.lower()
