# ADR：Matting-Flash CLI 适配与更新路线决策

状态：已接受（2026-11-26）。适用范围：本仓，其他仓库只读参照。

## 1. 功能盘点（动手前复核真实代码）

| 项 | 实际代码结论 |
|---|---|
| HTTP 端点 | **9 个**（任务书第 2 节记为 8）：`GET /api/status`、`POST /api/matting/single`、`POST /api/batch`、`GET /api/jobs`、`GET /api/jobs/{id}`、`POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/retry`、`GET /results/{name}`、`GET /`。无 `/v1`、无 `/health` |
| 引擎 | 1 个：BiRefNet（`ZhengPeng7/BiRefNet`），`app/adapter.py` 模块级单例、进程内懒加载常驻 |
| 可暴露动作 | 单图抠图、批量抠图、任务列表/详情/取消/重试、结果文件下载、状态/模型/后处理预设 |
| 既有脚本 | `scripts/real_infer.py` / `browser_real.py` 是 argparse 验收脚本（exit 0/1/2，输出 metrics.json 过程记录），**不是产品 CLI**，不复用其输出契约 |
| 桌面壳 | 本次开工前已有提交 `0cec0e2`（Tauri 2 + NSIS + 动态端口 + `/api/status` 健康轮询），但**缺全局 `matting` 命令**；S1 仅补缺失的 shim/PATH/快捷方式，不重复造壳 |

## 2. 关键决策

1. **CLI 形态**：双模式混合。默认直调 service layer（`app/adapter.py` + `app/queue.py`），显式/自动发现常驻服务时 `single` / `batch` 走 HTTP（S3）；其余子命令始终直调，避免服务版本差异导致行为漂移。
2. **输出契约**：`Result(success/data/error/code/command/warnings)` 信封 + Human/JSON 双输出。`--json` 在 `main()` 里做 argv 预提取，**任意位置生效**；进入业务模块前用 `begin_json_mode()` 把第三方 stdout print 重定向到 stderr，保证 `json.loads` 严格通过。
3. **退出码**：0 成功 / 1 运行时业务错误 / 2 用法错误 / 3 未就绪（缺权重、缺依赖、缺运行时）。`sys.exit(main())` 保证退出码传到 shell。
4. **任务状态跨进程**：CLI 每命令独立进程，`MattingService` 的 `_jobs` 只在本进程有效。为让 `jobs list/show/cancel/retry` 有意义，CLI 在 `results/cli_jobs.json` 写轻量任务台账（只含 id/文件/状态/结果名，不含图片字节与参数）；`cancel` 对本地 CLI 是「标记为取消」，`retry` 重新读取输入文件执行。常驻服务模式下的任务查询仍以服务端为准（在 ADR-S3 补充）。
5. **产物目录**：固定复用 `config.RESULTS_DIR`（`results/`；安装态为用户数据目录），`--output-dir` 只做最终拷贝，避免推理中间结果散落。
6. **端口**：桌面壳从零建设时选**动态预抓端口**（`TcpListener::bind("127.0.0.1:0")`），对齐其他五仓、避免与用户已有 8765 冲突；开发态 `MATTING_PORT=8765` 保持不变。S3 由启动脚本写 `.server_port` 端口文件供 CLI 发现，不改 Rust 启动协议。
7. **更新路线**：权重 444MB + bootstrap runtime 属「自带大文件」，但维护成本 > 流量成本，收敛为**单一完整包**；不引入差分/增量双路线。Tauri updater 只认精确完整 exe 名；自写 NSIS 用 `/RELAUNCH` 补第 7 步自动重启（详见 U 阶段实现）。
8. **门禁**：最小 `pyproject.toml` 工具段 + pre-commit + CI；仅工具配置，不改运行时依赖。ruff/mypy 指向 `app/` 与 `scripts/`，测试文件允许宽松断言。

## 3. 分阶段决策记录

| 阶段 | 决策 | 验证方式 |
|---|---|---|
| S1 | 已有壳不改；只补 shim、PATH 注册、CLI 快捷方式 | NSIS 编译零警告；PATH 16 组边界模拟；cargo test/clippy |
| S2 | 直调 service layer；CLI 台账解决进程隔离 | pytest + `json.loads` + 退出码实测 |
| S3 | 服务发现：显式 URL > 环境变量 > 端口文件；6 种降级回落直调 | 真实 HTTP 端点契约实测 + 墙钟/KPI |
| S4 | 前置校验（缺权重/显存）在模型加载前完成；三类真实冒烟先人眼后数值 | 真实图片三组 + 显存/耗时记录 |
| U | 单一完整 exe + `/S /RELAUNCH` 第 7 步 | 静态契约 + NSIS 探针 + CI 端到端（本机无 Release 资产时标注 blocking） |

## 4. S3 实测端点契约与 KPI（2026-11-26，httpx 真实请求）

| 请求 | 实测结果 |
|---|---|
| `GET /api/status` | 200 `application/json`，含 `state/version/ready/missing_weights`；无 `X-CSRF-Token` / `Set-Cookie`，即无 CSRF 握手 |
| `GET /health` | 404 JSON；**不能**作为健康端点 |
| `POST /api/matting/single` | multipart `file` + form `params`（JSON 字符串）；成功返回 `{state:DONE, preview:{result:"data:image/png;base64,..."}, cutout_name, mask_name, width, height}`；缺依赖 409 `{detail:{code:"MODEL_NOT_READY",...}}` |
| `POST /api/batch` | multipart 重复 `files` + form `params`；返回 `{jobs:[{job_id,state:"queued",...}]}`，需轮询 `/api/jobs/{id}` |
| `GET /api/jobs` | 200 `{jobs:[...]}` |
| `GET /results/{name}` | 成功 200 `image/png`；不存在 404 JSON |

服务发现：`--server` > `MATTING_CLI_SERVER` > 端口文件（启动脚本写 `<app>/.server_port`，纯数字，正常退出清理）；2s 探测；6 条降级路径（显式不可达 / 版本不符 / 环境变量不可达 / 无端口文件 / 陈旧端口 / 非法状态响应）全部回落直调，`--no-remote` 强制直调。

墙钟实测（RTX 5070 Ti Laptop 12GB，512×600 公有领域肖像，同一张图）：
- 直调冷启动（含 torch import + 权重加载 + 前向）：**73.36s**，EXIT=0。
- 常驻服务已加载后的第 2 次 HTTP 调用：**22.47s**，EXIT=0，约 **3.3×**。
- 服务日志中「自动加载模型」在两次 HTTP 调用期间新增 **0** 行（首次预热加载 1 行），证明复用的是常驻模型而非重新加载。
- 条件说明：本机 GPU 同时承载其它本地应用，绝对耗时被抬高；相对收益与本仓「每命令独立进程 import torch 极贵」的架构一致。`remote-warm` 首轮曾因 30s 客户端超时暴露未捕获的传输异常，已修为 `RemoteError` 并回落直调（测试钉住）。

