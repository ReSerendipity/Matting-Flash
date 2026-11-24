# Models

本文件区分**代码许可**与**每个模型权重许可**。权重不入库、不随安装包分发。

## 首版默认模型

| 项 | 值 |
|---|---|
| 模型 ID | `ZhengPeng7/BiRefNet`（通用背景移除） |
| 代码许可 | MIT — https://github.com/ZhengPeng7/BiRefNet |
| 权重许可 | MIT — https://huggingface.co/ZhengPeng7/BiRefNet |
| 权重文件 | `model.safetensors`（444 MB，FP16；2026-11-24 经 hf-mirror 核实） |
| 备选权重 | `ZhengPeng7/BiRefNet-matting`（885 MB，trimap-free 抠图专用，MIT） |
| 默认档位建议 | 8GB 入门；6GB 紧（FP16、控制分辨率 ≤1024²）。依据：官方实测 FP16 1024×1024 / RTX 4090 ≈ 3.45GB 显存、17 FPS |
| 加载方式 | `AutoModelForImageSegmentation.from_pretrained(<本地目录>, trust_remote_code=True)`，本地目录即 `weights/` |

## 下载说明（需联网，由用户自行执行）

```powershell
# 示例：下载官方通用权重到项目 weights 目录（约 444MB）
pip install "huggingface-hub>0.25"
python -c "from huggingface_hub import snapshot_download; snapshot_download('ZhengPeng7/BiRefNet', local_dir='weights')"
```

或在 README 记录的同源地址手动下载 `model.safetensors` 放入 `weights/`。

## 权重许可口径

- HF 各模型卡标注 MIT；GitHub 仓库 LICENSE 为 MIT。
- HF 讨论区 #23（2026-09）：作者确认权重可在 MIT 下使用；同时提示训练数据
  （DIS5K 等）条款存在理论上的商用争议，如需 100% 确定性应另行咨询数据集方。
- GitHub Release v1 另提供 `.pth` 与 `.onnx`（general pth ≈ 885MB），同为官方
  MIT 口径。
- 工单/品类全景中 "3.07GB" 的口径来自 modelscope 镜像按目录求和，与官方 HF
  单文件体积不一致；本机以官方一手数据为准，已在 phase-a.md 记录差异。

## BiRefNet-V2

官方论文 arXiv:2611.11272（2026-11 上旬）。截至 2026-11-24，HF collection
"BiRefNet-V2" 仅含论文与旧版模型，未见 V2 权重发布，首版不采用。

## 验证状态

真实 BiRefNet 推理**已测**（2026-11-24，状态 `MODEL_TESTED`）：

- 权重：官方 `ZhengPeng7/BiRefNet` 通用权重，`weights/model.safetensors`，444,473,596 字节，SHA256 `9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154`；2026-11-24 经 hf-mirror 直连下载，工具 `huggingface_hub.snapshot_download`。
- 环境：项目内 `.venv-real`，以 `--system-site-packages` 复用本机 `torch 2.11.0+cu128`（未重复下载 torch）；仅新装 `timm`，其余（torchvision/transformers/kornia/einops/huggingface-hub 等）继承宿主；`pip check` 通过。
- 推理：FP16、batch=1、输入固定 Resize 1024×1024（官方 handler 口径，最长边 ≤1024），样本为公有领域肖像（Grace Hopper，512×600）；加载 3.8s、前向 5.2s、峰值显存 1.58GB；脚本输出 alpha 对齐回原图，导出透明 PNG 512×600。
- 链路：服务 `/api/status` = `READY`；`/api/matting/single` 返回 RGBA 512×600 透明 PNG，alpha 非空（180,463 个非全透明像素）。证据目录 `results/real-20261124-214136/`，阶段报告见 `phase-c.md`。
- **未测边界**：>2K 高分辨率输入的显存/耗时；批量并发链路（本次仅单图）；HR/Matting 等其他官方变体未下未测。
