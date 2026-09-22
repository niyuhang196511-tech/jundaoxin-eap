# vLLM multi-LoRA 部署指南（Windows + NVIDIA GPU，M42-A / L1）

> 平台侧工程（VllmProvider + LoRA 托管 API）已离线落地；本文覆盖真实 GPU 侧部署。
> 适用：本机 Windows 11 + NVIDIA GPU；vLLM 不支持 Windows 原生，走 WSL2 或 Docker。

## 1. 前提

- NVIDIA 驱动 ≥ 550（Windows 侧安装即可，WSL2/Docker 自动透传，`nvidia-smi` 验证 GPU 可见）
- 显存建议 ≥ 16GB（7B 基座 + 2 个 LoRA；紧张见 §5）；WSL2：`wsl --install`（Ubuntu 22.04+）

## 2. 路径①：WSL2 内 pip 安装 vLLM（推荐，CUDA 就绪后最简）

```bash
sudo apt update && sudo apt install -y python3.12-venv
python3.12 -m venv ~/vllm-venv && source ~/vllm-venv/bin/activate
pip install vllm  # ≥0.5 支持 LoRA 动态加载；CUDA 运行时随 wheel 附带
```

## 3. 路径②：Docker（免管 Python/CUDA 环境）

```bash
docker run -d --name vllm --gpus all --shm-size=2g \
  -v /mnt/c/adapters:/adapters -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct --enable-lora
```

## 4. 启动与 LoRA

```bash
# 路径① 启动：--enable-lora 开启动态加载/卸载管理端点；--lora-modules 预挂 adapter
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --enable-lora --max-lora-rank 64 --port 8000 \
  --lora-modules sql-lora=/mnt/c/adapters/sql-lora

# 动态加载 / 卸载（平台 LoRA API 即封装这两个端点）
curl -X POST http://localhost:8000/v1/load_lora_adapter \
  -H "Content-Type: application/json" \
  -d '{"lora_name": "sql-lora", "lora_path": "/mnt/c/adapters/sql-lora"}'
curl -X POST http://localhost:8000/v1/unload_lora_adapter \
  -H "Content-Type: application/json" -d '{"lora_name": "sql-lora"}'
```

adapter 目录：Windows 侧 `C:\adapters\sql-lora` → WSL2 内 `/mnt/c/adapters/sql-lora`
（路径①直接可用；路径②经 `-v` 挂入容器）。目录内须含 `adapter_config.json` + 权重，
基座需与 `--model` 兼容（平台侧注册时填 `base_model` 对应）。

## 5. 平台侧连接（模型中心 + LoRA API）

```bash
# ① 注册 vLLM 供应商模型（provider=vllm；base_url 为 OpenAI 兼容地址，含 /v1）
curl -X POST http://localhost:9000/api/v1/models -H "Authorization: Bearer <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name": "qwen-vllm", "capabilities": ["chat"], "provider": "vllm",
       "base_url": "http://<wsl-ip>:8000/v1", "remote_model": "Qwen/Qwen2.5-7B-Instruct"}'

# ② 注册 LoRA adapter（source_path 为 GPU 宿主机侧路径）
curl -X POST http://localhost:9000/api/v1/lora -H "Authorization: Bearer <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"name": "sql-lora", "base_model": "Qwen/Qwen2.5-7B-Instruct",
       "source_path": "/mnt/c/adapters/sql-lora"}'

# ③ 加载（vLLM 地址自动从 ① 的模型记录解析，也可 body 显式传 base_url）
curl -X POST http://localhost:9000/api/v1/lora/sql-lora/load \
  -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
  -d '{"base_url": "http://<wsl-ip>:8000/v1"}'
# ④ 健康检查：GET /api/v1/lora/sql-lora/health → served=true 即在服务
# ⑤ 调用：模型中心按 capability 路由；LoRA 模型名 = adapter 名（remote_model 可指 adapter 名）
```

## 6. 常见坑

| 现象 | 原因与处置 |
|---|---|
| Windows 访问不到 WSL2 服务 / 防火墙拦截 | WSL2 默认 localhost 转发仅单向（Windows→WSL 可用）；平台跑 Windows 侧用 `localhost:8000`，跨机用 `<wsl-ip>`（`wsl hostname -I`，Win11 可开 `networkingMode=mirrored`）；跨机访问需防火墙放行 8000 |
| CUDA OOM | 降 `--gpu-memory-utilization 0.85`、`--max-lora-rank 32`、少挂 adapter；或换量化基座 |
| load_lora 404 | vLLM 未以 `--enable-lora` 启动；或版本 <0.5 |
| 平台侧调用 401 | vLLM 启动带 `--api-key` 时，模型中心注册需配 api_key（管理端点无鉴权，仅限内网/本机） |

> 诚实说明：本指南在真实 GPU 环境验证前为文档交付；平台侧行为已由 mock transport 测试覆盖（`tests/test_vllm_lora.py`），端点形态以 vLLM 官方 OpenAI 兼容文档为准。
