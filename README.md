# Matting-Flash

本地通用图片抠图与透明背景导出工作台。源码可运行、测试完整；真实 BiRefNet 推理已在本机 RTX 5070 Ti Laptop 12GB 跑通，状态 `MODEL_TESTED`（高分辨率/批量边界仍未测）。

## 定位与边界

- 单图/批量 PNG、JPEG、WebP 输入；输出 alpha matte 与透明 PNG。
- 三联预览（原图 / 蒙版 / 结果）、缩放、背景切换。
- 轻量后处理：边缘羽化、阈值、二值化、腐蚀/膨胀；所有参数可一键复位。
- 批量队列：失败重试、取消、结果下载。
- **不是**闪卡生成器；不做卡面、3D 查看或字体设计。与 Card-Studio（浏览器内 MODNet 闪卡工作流）无关，本项目不修改 Card-Studio。

## 当前验证状态（诚实口径）

- ✅ 29 个自动化测试通过（后处理数值边界、透明导出、批量状态、API 未就绪语义）。
- ✅ 开发模式冒烟：服务仅监听 `127.0.0.1`，UI 明确显示 `NOT_READY`，提交返回 409 `MODEL_NOT_READY`，**不用假图冒充结果**。
- ✅ 真实 BiRefNet 推理已测（2026-11-24）：官方 `ZhengPeng7/BiRefNet` 通用权重（444,473,596 字节，SHA256 `9ab37426…a154`），FP16、batch=1、输入 1024×1024（官方 handler 口径），加载 3.8s / 前向 5.2s / 峰值显存 1.58GB；`/api/status` 为 `READY`，`/api/matting/single` 返回与输入同尺寸的透明 PNG（alpha 非空）。证据见 `results/real-20261124-214136/`。
- ✅ 真实浏览器闭环已测（2026-11-25）：系统 Edge + Playwright 经页面控件上传 → `POST /api/matting/single` 200 → 三联预览 → 页面下载透明 PNG（512×600，alpha 0–255）；产物与 CLI 真实推理逐字节一致（SHA256 `64d3e680…b3d00`）。证据见 `results/browser-real-20261125-193219/`。
- 未测边界：>2K 高分辨率的显存/耗时、批量并发链路。详见 `docs/MODELS.md` 与阶段报告 `phase-c.md`。

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

## 显存与许可

- 建议 8GB 起步；6GB 需 FP16 + 控制分辨率（官方实测 FP16 1024×1024 ≈ 3.45GB）。依据与口径见 `docs/MODELS.md`。
- 外壳代码 Apache-2.0；BiRefNet 代码与权重 MIT。权重不入库、不分发。详见 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES`、`docs/MODELS.md`。

## 已知限制

- 真实推理仅覆盖单图 1024×1024 FP16 一路；高分辨率（>2K）显存与耗时未测。
- 批量并发为串行执行；单任务超时保护依赖客户端，服务端不设硬超时。
