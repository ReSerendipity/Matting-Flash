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

真实 BiRefNet 推理**未测**：本轮未下载任何权重、未安装 torch/transformers。
状态为 `CODE_READY_MODEL_UNTESTED`。
