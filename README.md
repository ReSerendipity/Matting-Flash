# Matting-Flash

本地通用图片抠图与透明背景导出工作台。源码可运行、测试完整；真实 BiRefNet 推理已在本机 RTX 5070 Ti Laptop 12GB 跑通，状态 `MODEL_TESTED`（高分辨率/批量边界仍未实测）。

## 定位与边界

- 单图/批量 PNG、JPEG、WebP 输入；输出 alpha matte 与透明 PNG。
- 三联预览（原图 / 蒙版 / 结果）、缩放、背景切换。
- 轻量后处理：边缘羽化、阈值、二值化、腐蚀/膨胀；所有参数可一键复位。
- 单图提交后可见 `RUNNING` 进度条与计时，完成显示尺寸/耗时并可下载；批量队列：串行处理、失败重试（新建任务）、取消、结果下载。
- **不是**闪卡生成器；不做卡面、3D 查看或字体设计。与 Card-Studio（浏览器内 MODNet 闪卡工作流）无关，本项目不修改 Card-Studio。

## 当前验证状态（诚实口径）

- ✅ 56 个自动化测试通过（后处理数值边界、透明导出、批量状态、API 未就绪语义、>2K 大图契约、FP16 上卡顺序、推理期间状态端点响应）。
- ✅ 开发模式冒烟：服务仅监听 `127.0.0.1`，UI 明确显示 `NOT_READY`，提交返回 409 `MODEL_NOT_READY`，**不用假图冒充结果**。
- ✅ 真实 BiRefNet 推理已测（2026-11-24）：官方 `ZhengPeng7/BiRefNet` 通用权重（444,473,596 字节，SHA256 `9ab37426…a154`），FP16、batch=1、输入 1024×1024（官方 handler 口径），加载 3.8s / 前向 5.2s / 峰值显存 1.58GB；`/api/status` 为 `READY`，`/api/matting/single` 返回与输入同尺寸的透明 PNG（alpha 非空）。证据见 `results/real-20261124-214136/`。
- ✅ 真实浏览器闭环已测（2026-11-25）：系统 Edge + Playwright 经页面控件上传 → `POST /api/matting/single` 200 → 三联预览 → 页面下载透明 PNG（512×600，alpha 0–255）；产物与 CLI 真实推理逐字节一致（SHA256 `64d3e680…b3d00`）。证据见 `results/browser-real-20261125-193219/`。**注**：该证据基于 `3473a0a` 旧实现；R5-A 改动推理输入尺寸、alpha 浮点还原与 FP16 上卡顺序后未重跑真实推理，最终口径以真实回归（R5-B）为准。
- 未测边界：>2K 与批量只有 fake/准备阶段契约（2026-11-25 加固），真实 GPU 下的 >2K 显存/耗时、批量链路与新的 FP16 上卡顺序峰值仍待真实回归（R5-B）。详见 `docs/MODELS.md` 与阶段报告。

## 安装（需要 Python 3.10+）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 模型下载（可选；不自动执行）

无权重时 UI 显示 `NOT_READY`，不会伪造推理。下载官方通用权重（约 444MB）：

```powershell
pip install "huggingface-hub>0.25"
python -c "from huggingface_hub import snapshot_download; snapshot_download('ZhengPeng7/BiRefNet', local_dir='weights')"
```

如需真实推理还需安装较重依赖（torch/transformers 等，见 requirements.txt 注释）。权重放在 `weights/` 即可被识别；可用环境变量 `MATTING_WEIGHTS_DIR` 改路径。

## 最小使用

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

浏览器打开 http://127.0.0.1:8765 ，拖入图片即可（模型就绪时）。头部徽章实时显示 `NOT_READY`/`READY`。

## CLI（人 / AI agent 可直接调用）

无需 Web 页面即可抠图、查任务与下载结果；契约见 `docs/ADR-CLI.md`。

```powershell
# 源码环境
.\.venv\Scripts\python.exe -m app.integrated_app.cli --help
.\.venv\Scripts\python.exe -m app.integrated_app.cli single -i photo.jpg -o results --json
.\.venv\Scripts\python.exe -m app.integrated_app.cli batch --input-dir photos -o results
.\.venv\Scripts\python.exe -m app.integrated_app.cli jobs list --json
.\.venv\Scripts\python.exe -m app.integrated_app.cli results download <name> -o out/
```

- 输出：`Result` 信封（`success/data/error/code/command/warnings`）；`--json` 可放任意位置，stdout 只有一行严格 JSON。
- 退出码：`0` 成功 / `1` 业务错误 / `2` 用法错误 / `3` 未就绪（缺权重或运行时）。
- 安装桌面版后，安装目录已加入当前用户 PATH，任意目录可直接敲 `matting ...`；开始菜单也有 CLI 快捷方式。
- 批量任务台账落 `results/cli_jobs.json`，`jobs` 子命令跨进程可查；产物统一落 `results/`（安装态为用户数据目录）。
- 桌面壳常驻服务可被 `single` / `batch` 自动复用：显式 `--server` > 环境变量 `MATTING_CLI_SERVER` > 端口文件 `.server_port`；`--no-remote` 强制直调；服务不可用时自动回落，不退化。实测契约与 KPI 见 `docs/ADR-CLI.md` §4。

## 桌面版（Windows，Tauri 2 + NSIS 当前用户安装器）

- 安装包与数据分卷需同目录：`Matting-Flash-Setup-v<version>.exe` + `Matting-Flash-Data.7z.001..NNN`（SHA256 见 `SHA256SUMS`）。
- 安装落 `%LOCALAPPDATA%\Programs\Matting-Flash`；权重/结果/日志落 `%LOCALAPPDATA%\Matting-Flash\{weights,results,logs}`，卸载默认保留。
- 缺权重也可启动：加载页与页头显示 `NOT_READY`，提交返回 409 `MODEL_NOT_READY`；把 `model.safetensors` 放进权重目录即可（见 `docs/MODELS.md`）。
- 构建（所有 target/分卷落 D 盘，产物不入 git）：

```powershell
.\scripts\build_desktop_bundle.ps1 -ReuseRuntime
```

完整端口/健康/安装卸载契约见 `docs/RELEASE_PROFILE.md`。

## 显存与许可

- 建议 8GB 起步；6GB 需 FP16 + 控制分辨率（官方实测 FP16 1024×1024 ≈ 3.45GB）。依据与口径见 `docs/MODELS.md`。
- 显存路径（2026-11-25 起）：权重先在半精度（宿主内存）转换，再整模型上卡，避免 FP32 全份权重占用 GPU 造成双份峰值；真实峰值待 R5-B 复测。
- 外壳代码 Apache-2.0；BiRefNet 代码与权重 MIT。权重不入库、不分发。详见 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES`、`docs/MODELS.md`。

## 已知限制

- 真实推理仅实测单图 512×600（FP16、输入 1024²）。>2K 大图走「长边 1024 等比缩小 + 32 对齐、alpha 浮点还原原尺寸」的代码路径，目前只有 fake 模型契约测试；真实显存与耗时待实测。
- 批量并发为串行执行；单任务超时保护依赖客户端，服务端不设硬超时。重试会新建一条任务记录（原失败记录保留对照）。
- 单图推理放在线程池执行，避免阻塞事件循环；进度为客户端计时 + 不确定进度条，服务端不回报百分比。
