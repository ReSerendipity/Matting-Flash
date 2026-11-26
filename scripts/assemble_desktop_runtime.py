"""组装 Matting-Flash 桌面版独立 Python runtime。

组成：
- 基础解释器：`--BasePython` 指向的 CPython 3.12 安装根（python.exe + DLLs + 标准库），
  排除 `Lib` 下的 `site-packages`、测试/教学包，以及 `include` / `libs` / `tcl` / `Scripts` 等构建期目录。
- 第三方依赖：`--VenvSitePackages`（本项目 `.venv-real`）与基础解释器 `Lib\site-packages`
  中的依赖闭包（从 `--Roots` 出发按 METADATA `Requires-Dist` 求传递闭包），按
  `RECORD` 清单逐文件复制。不重新下载任何 wheel，不打包项目权重。

输出：`<OutDir>\python.exe`、`<OutDir>\Lib\site-packages\...`、`<OutDir>\runtime-manifest.json`。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import shutil
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:  # pragma: no cover - packaging 是 pip 自带依赖，构建机必有
    raise SystemExit("缺少 packaging（pip 自带）；请用完整 CPython 环境运行本脚本") from None

# 应用与真实推理路径需要的最小根集（与 requirements.txt / weights/requirements.txt 对齐）。
DEFAULT_ROOTS = [
    "torch",
    "torchvision",
    "transformers",
    "timm",
    "fastapi",
    "uvicorn",
    "python-multipart",
    "numpy",
    "pillow",
    "huggingface-hub",
    "safetensors",
    "tokenizers",
    "accelerate",
    "kornia",
    "einops",
    "opencv-python",
    "scipy",
    "requests",
    "tqdm",
    "pyyaml",
    "jinja2",
    "sympy",
    "networkx",
    "typing-extensions",
    "filelock",
    "fsspec",
    "packaging",
]

# 基础解释器根目录下不随包分发的目录（构建期头文件/导入库、Tcl、脚本、测试与教学包）。
SKIP_ROOT_DIRS = {"include", "libs", "tcl", "Scripts", "Doc", "Tools"}
# Lib 下不随包分发的子目录。
SKIP_LIB_DIRS = {"site-packages", "test", "tkinter", "idlelib", "turtledemo", "ensurepip", "lib2to3"}


def norm(name: str) -> str:
    """PEP 503 规范化：大小写、下划线/点统一为短横线。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def marker_env() -> dict[str, str]:
    return {
        "implementation_name": "cpython",
        "implementation_version": "3.12.10",
        "os_name": "nt",
        "platform_machine": "AMD64",
        "platform_release": "",
        "platform_system": "Windows",
        "platform_version": "",
        "python_full_version": "3.12.10",
        "platform_python_implementation": "CPython",
        "python_version": "3.12",
        "sys_platform": "win32",
        "extra": "",
    }


def _ignore_base(_dir: str, names: list[str]) -> set[str]:
    """基础解释器根目录：只跳过构建期目录与字节码缓存。"""
    skipped = {n for n in names if n in SKIP_ROOT_DIRS or n == "__pycache__"}
    skipped |= {n for n in names if n.endswith((".pyc", ".pyo", ".pdb"))}
    return skipped


def _ignore_lib(dir_path: str, names: list[str]) -> set[str]:
    """Lib 目录：顶层跳过测试/教学/ensurepip，其余仅清理缓存。"""
    rel = Path(dir_path)
    skipped = {n for n in names if n == "__pycache__"}
    skipped |= {n for n in names if n.endswith((".pyc", ".pyo"))}
    if rel.name == "Lib":
        skipped |= {n for n in names if n in SKIP_LIB_DIRS}
    return skipped


def copy_base_python(base: Path, out: Path) -> None:
    if not (base / "python.exe").is_file():
        raise SystemExit(f"BasePython 缺少 python.exe：{base}")
    if not (base / "Lib").is_dir():
        raise SystemExit(f"BasePython 缺少 Lib 标准库：{base}")
    out.mkdir(parents=True, exist_ok=True)
    for entry in sorted(base.iterdir()):
        if entry.is_file():
            shutil.copy2(entry, out / entry.name)
    for sub in ("DLLs", "Lib"):
        src = base / sub
        if not src.is_dir():
            continue
        shutil.copytree(
            src,
            out / sub,
            ignore=_ignore_base if sub == "DLLs" else _ignore_lib,
            dirs_exist_ok=True,
        )


def collect_dists(roots: list[Path]) -> dict[str, tuple[md.Distribution, Path]]:
    """按根列表收集已安装分发；同名分发按根顺序优先（系统根在前）。"""
    result: dict[str, tuple[md.Distribution, Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for dist in md.distributions(path=[str(root)]):
            name = dist.metadata["Name"]
            if not name:
                continue
            key = norm(name)
            if key not in result:
                result[key] = (dist, root)
    return result


def dist_requires(dist: md.Distribution, env: dict[str, str]) -> list[str]:
    out: list[str] = []
    for raw in dist.requires or []:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        out.append(norm(req.name))
    return out


def resolve_closure(
    dists: dict[str, tuple[md.Distribution, Path]], roots: list[str], env: dict[str, str]
) -> list[str]:
    closure: set[str] = set()
    queue = [norm(r) for r in roots]
    missing: list[str] = []
    while queue:
        name = queue.pop()
        if name in closure:
            continue
        if name not in dists:
            missing.append(name)
            continue
        closure.add(name)
        queue.extend(d for d in dist_requires(dists[name][0], env) if d not in closure)
    if missing:
        print(f"[warn] 未安装的依赖（闭包跳过）：{sorted(set(missing))}", flush=True)
    return sorted(closure)


def copy_dist(dist: md.Distribution, site_root: Path, dest: Path) -> tuple[int, int]:
    files = 0
    copied = 0
    for rel in dist.files or []:
        parts = rel.parts
        if parts and parts[0] == "..":
            continue  # console script（Scripts/），桌面壳不依赖 CLI 入口
        src = site_root / rel
        if src.suffix.lower() == ".whl" or src.suffix.lower() in {".pyc", ".pyo"}:
            continue
        if not src.is_file():
            continue
        files += 1
        dst = dest / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return files, copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组装 Matting-Flash 桌面版独立 runtime")
    parser.add_argument("--BasePython", required=True, help="基础 CPython 安装根（含 python.exe）")
    parser.add_argument("--VenvSitePackages", required=True, help="项目 .venv-real\\Lib\\site-packages")
    parser.add_argument("--OutDir", required=True, help="输出 runtime 目录")
    parser.add_argument("--Manifest", default="", help="runtime-manifest.json 输出路径")
    parser.add_argument("--Roots", default="", help="逗号分隔的根依赖（默认内置清单）")
    args = parser.parse_args(argv)

    base = Path(args.BasePython).resolve()
    venv_site = Path(args.VenvSitePackages).resolve()
    out = Path(args.OutDir).resolve()
    roots = [r.strip() for r in args.Roots.split(",") if r.strip()] or list(DEFAULT_ROOTS)

    if out.exists():
        print(f"[1/4] 清理旧 runtime：{out}", flush=True)
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[2/4] 复制基础解释器：{base} → {out}", flush=True)
    copy_base_python(base, out)

    site_roots = [base / "Lib" / "site-packages", venv_site]
    print(f"[3/4] 解析依赖闭包（根 {len(roots)} 个，site 根 {len(site_roots)} 个）", flush=True)
    dists = collect_dists(site_roots)
    env = marker_env()
    closure = resolve_closure(dists, roots, env)

    dest_site = out / "Lib" / "site-packages"
    dest_site.mkdir(parents=True, exist_ok=True)
    entries = []
    total_bytes = 0
    for i, name in enumerate(closure, 1):
        dist, root = dists[name]
        files, copied = copy_dist(dist, root, dest_site)
        bytes_ = 0
        dist_dir = dest_site
        for rel in dist.files or []:
            if rel.parts and rel.parts[0] != "..":
                p = dist_dir / rel
                if p.is_file():
                    bytes_ += p.stat().st_size
        total_bytes += bytes_
        entries.append(
            {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "site_root": str(root),
                "record_files": files,
                "copied_files": copied,
                "bytes": bytes_,
            }
        )
        if i % 10 == 0 or i == len(closure):
            print(f"    · {i}/{len(closure)} 分发，累计 {total_bytes / (1024 ** 2):.0f} MB", flush=True)

    manifest = {
        "schema": 1,
        "product": "Matting-Flash-Desktop",
        "base_python": str(base),
        "base_python_version": "3.12.10",
        "site_roots": [str(p) for p in site_roots],
        "roots": roots,
        "distributions": entries,
        "total_bytes": total_bytes,
        "weights_policy": "项目权重 weights/model.safetensors 不入包；安装后 MATTING_WEIGHTS_DIR 指向 %LOCALAPPDATA%\\Matting-Flash\\weights",
    }
    manifest_path = Path(args.Manifest).resolve() if args.Manifest else out / "runtime-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[4/4] 完成：{len(closure)} 个分发 / {total_bytes / (1024 ** 3):.2f} GB", flush=True)
    print(f"    · 清单：{manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
