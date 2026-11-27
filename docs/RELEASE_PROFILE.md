# Matting-Flash 发版档案（RELEASE_PROFILE）

- 版本单一事实源：`pyproject.toml` 的 `version`（当前 `0.1.0`），与 `app.__version__` 及桌面壳三件套一致，由 `tests/test_desktop_version.py` 守护。
- 平台：Windows x64（Tauri v2 WebView2 + 当前用户 NSIS 安装器）。
- 产品名：`Matting-Flash`（安装显示名「Matting-Flash 抠图工作台」）。

## 1. 端口与启动入口

| 项 | 契约 |
| --- | --- |
| 默认开发端口 | `127.0.0.1:8765`（`uvicorn app.main:app` / `MATTING_PORT`） |
| 桌面壳端口 | 动态回环：壳用 `TcpListener::bind("127.0.0.1:0")` 预抓空闲端口，再以 `--host 127.0.0.1 --port N` 传给启动脚本；禁止固定端口 |
| 启动入口 | `desktop` 壳 → `app/scripts/desktop_launch.py` → `uvicorn.run(app.main:app)` |
| 端口日志锚点 | `desktop_launch.py` 首行输出 `监听端口: N`，落 `logs/desktop_python_*.log` |
| 浏览器 | 壳不调用 `webbrowser.open`；WebView 即界面。启动脚本不得打开浏览器 |

## 2. 健康与就绪

- 健康端点：`GET /api/status`，HTTP 200 且 JSON 可解析视为服务就绪（与模型是否就绪无关）。
- 壳关心的字段：`ready` / `state` / `detail` / `missing_weights` / `weights_dir` / `results_dir` / `version`。
  - `ready=true` 当且仅当 `state == "READY"`（权重与推理依赖均可用）。
  - 缺权重时 `state=NOT_READY`，`missing_weights` 列出候选权重文件名（`model.safetensors` 优先）。
- 壳轮询：500ms 间隔、单次 3s 超时、总超时 300s（可用 `MATTING_STARTUP_TIMEOUT_SECS` 覆盖，仅测试用）；拿到状态后导航主窗口到 `http://127.0.0.1:<port>/`。
- 缺权重：服务照常启动，页面徽章显示 `MODEL · NOT_READY` 与权重目录；提交返回 409 `MODEL_NOT_READY`，绝不伪造推理结果。

## 3. runtime 与模型边界

| 层 | 策略 |
| --- | --- |
| Python runtime | CPython 3.12.10（`C:\Python312` base）+ 项目 `.venv-real\Lib\site-packages` 依赖闭包；含 `torch 2.11.0+cu128` / `torchvision 0.26.0+cu128` / `transformers 5.23.0` / `timm 1.0.31` 等 57 个分发 |
| 依赖闭包 | 由 `scripts/assemble_desktop_runtime.py` 按 METADATA `Requires-Dist` 求传递闭包、按 `RECORD` 逐文件复制；不重新下载任何 wheel，也不整包复制宿主 7GB site-packages |
| 代码 | 仅归档 git 跟踪代码（`git archive HEAD`）+ 指定 runtime；排除 `.venv*`、`results`、缓存、日志 |
| 模型权重 | `weights/model.safetensors`（444 MB）**不入库、不入 staging、不入数据卷**；staging 内 `*.safetensors` / `*.onnx` / `*.ckpt` 零命中 |
| 权重点 | 安装态 `MATTING_WEIGHTS_DIR=%LOCALAPPDATA%\Matting-Flash\weights`（壳注入，可被外部环境变量覆盖） |
| 用户数据 | `MATTING_RESULTS_DIR=%LOCALAPPDATA%\Matting-Flash\results`；壳日志 `%LOCALAPPDATA%\Matting-Flash\logs`；三者均不写安装目录 |
| 开发态 | 权重回退 `<repo>\weights`、结果回退 `<repo>\results`（`.gitignore` 覆盖） |

放置指南：`docs/MODELS.md`（官方下载地址、SHA256、许可、显存口径）；安装后同一文档随包在 `app/docs/MODELS.md`。

## 4. staging / 安装 / 卸载契约

### staging（`scripts/assemble_desktop_staging.ps1`）

```
<staging>/
├─ Matting-Flash.exe           # cargo build --release 产物（随 Setup 内嵌，不入数据卷）
└─ app/                        # 应用根（数据卷内容）
   ├─ scripts/desktop_launch.py
   ├─ app/ static/ docs/
   ├─ runtime/python.exe ...   # 独立运行时（torch/transformers）
   └─ pyproject.toml requirements.txt README.md LICENSE NOTICE ...
```

- staging 根即「安装根」：壳的 `resolve_app_dir()` 认 `<exe_dir>\app\scripts\desktop_launch.py`；后端 `PROJECT_ROOT` 亦为 `app/` 这一级。
- 归档命令：Windows `System32\tar.exe` 解 `git archive` 的 tar；`pyproject.toml` 必须随包（版本来源）。
- 禁止归档：`weights/`、`results/`、`.venv*`、`__pycache__`、模型文件、日志、`.git`。

### 安装（`scripts/installer/setup.nsi`）

- MUI2、`RequestExecutionLevel user`、`InstallDir "$LOCALAPPDATA\Programs\Matting-Flash"`，无需管理员。
- **分发形态：`Matting-Flash-Setup-v<version>.exe` 与 `Matting-Flash-Data.7z.001..NNN` 必须位于同一目录**。
  安装器从 `$EXEDIR`（或 `$EXEDIR\data`）读取首卷，用内嵌的独立 `7za.exe` 解压到 `$INSTDIR`；缺卷时明确报错并中止（不静默装半套）。
  这样避免把 2GB+ 运行时二次嵌入 32 位 NSIS 壳，也避免解压前复制一份到 `%TEMP%`。
- 快捷方式：桌面 + 开始菜单（另含「Matting-Flash CLI」，`$SYSDIR\cmd.exe /k "<INSTDIR>\matting.cmd" --help`）；卸载信息写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\Matting-Flash`（DisplayName/DisplayVersion/UninstallString/QuietUninstallString/EstimatedSize/InstallLocation）。
- 安装时创建 `%LOCALAPPDATA%\Matting-Flash\{weights,results,logs}`，覆盖安装不清理。
- 所有 MessageBox 均有 `IfSilent` 守卫：`/S` 静默安装不弹窗、不挂起。

### CLI 全局命令（S1）

- 安装器把 `scripts/installer/matting.cmd`（ASCII-only）复制到 `$INSTDIR` 根目录，并幂等追加 `$INSTDIR` 到 HKCU `Environment\Path`，广播 `WM_SETTINGCHANGE`；卸载时先精确移除条目再删注册表键。
- PATH 匹配用 `";...;"` 包裹后查 `;$INSTDIR;`，避免 `Matting-Flash-Extra` 之类前缀同名目录被误判；重复条目循环移除、分号折叠，安装前后 PATH 除本仓条目外不变。等价语言边界模拟见 `tests/test_installer_cli.py`（8+8 组）。
- shim 定位 `<InstallDir>\app\runtime\python.exe` 与 `<InstallDir>\app\app\integrated_app\cli.py`，设置离线缓存环境变量与用户数据目录，`%*` 透传、`exit /b %ERRORLEVEL%`；缺运行时/CLI 入口返回 3（NOT_READY）。

### 卸载

- 静默 `/S`：默认**保留**用户数据（权重/结果/日志），只删程序目录与快捷方式/注册表。
- 交互卸载：询问「是否保留用户数据」，默认保留；选择删除才移除 `%LOCALAPPDATA%\Matting-Flash`。绝不静默删权重。

## 5. 产物

| 文件 | 说明 |
| --- | --- |
| `Matting-Flash-Setup-v<version>.exe` | NSIS 当前用户安装器（内嵌壳 exe 与 7za，数据分卷从同目录读取） |
| `Matting-Flash-Data.7z.001..NNN` | 数据卷（`app/` 全量，单卷 ≤1900MB） |
| `SHA256SUMS` | 覆盖 Setup 与全部分卷，LF 行尾（Git Bash `sha256sum -c` 可直接校验） |
| `upload-list.json` | 待上传清单（文件名/字节/SHA256） |

构建根：`D:/R5Build/Matting-Flash/`（`cargo-target`、`runtime`、`staging`、`dist`、`tmp` 全在 D 盘）。

## 6. 更新路线（单一完整包）

- 资产：`Matting-Flash-Setup-v<version>.exe` + 同名 `.sha256`（或 Release 正文里的 64 位 hash）；官网与「检查更新」共用。`desktop/src-tauri/src/updater.rs` 只认精确资产名，遇到旧增量 zip 直接报「更新包不存在」。
- 客户端流程：`check_update` → `start_update` → 下载 → SHA256 → `update-installing` → `/S /RELAUNCH` 启动安装器 → 壳有序退出 → NSIS 装完 `Exec` 拉起 `Matting-Flash.exe`（第 7 步自动重启）。
- 前端入口：加载页 `mfdu-*` 面板；主页面（`static/index.html`）在 Tauri WebView 内显示浮动「检查更新」按钮，浏览器直开时自动隐藏。
- 覆盖语义：安装段没有 `RMDir /r "$INSTDIR"`，用户数据（权重/结果/日志）在 `%LOCALAPPDATA%\Matting-Flash` 且卸载默认保留；新用户是「全新安装」，老用户重复运行安装包才是「覆盖安装」。
- 验收：L1 `tests/test_update_contract.py`；L2 NSIS `/RELAUNCH` 最小探针（`/S /RELAUNCH` 生成标记，单独 `/S` 不生成）；L3 `.github/workflows/verify-relaunch.yml`（需 Release 完整 exe，当前无远端资产，待发布后手工触发）。
- 版本权威：`pyproject.toml` 与 `app.__version__` / `Cargo.toml` / `tauri.conf.json` 一致，由 `tests/test_desktop_version.py` 守护；CI 验收从安装后 `app/pyproject.toml` 读取版本号。
