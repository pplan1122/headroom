# pplan1122/headroom — Levelup Project Fork

## 概况

基于 [chopratejas/headroom](https://github.com/chopratejas/headroom) 的分支，
为 Levelup 项目定制。包含 GPU 加速、CJK 中文保护、管线优化等补丁。

## 与上游的差异

| # | 改动 | 文件/模块 | 说明 |
|---|------|----------|------|
| 1 | CJK 中文保护 | `sitecustomize.py` | Kompress (ModernBERT 仅英文) 前用占位符替换 CJK，压缩后恢复 |
| 2 | 单实例双路由 | `sitecustomize.py` | 通过 `x-headroom-base-url` 请求头路由到多个上游 API |
| 3 | Kompress GPU 加速 | systemd env | `HEADROOM_KOMPRESS_BACKEND=pytorch` + PyTorch 2.5.1+cu124 |
| 4 | 压缩缓存内存泄漏修复 (P0) | `sitecustomize.py` | `_stable_hashes`/`_first_seen` 上限 5k/10k，`ContentRouter._results` 上限 10k |
| 5 | 移除 `compute_frozen_count=0` (P0a) | `sitecustomize.py` | 恢复 prefix cache 保护；原补丁导致上游 cache 每轮全 bust |
| 6 | 管线 worker 调优 (P1) | systemd env | `HEADROOM_COMPRESS_WORKERS=8` → `=4` |
| 7 | 检测缓存 (P0) | `content_router.py` | `_detect_content_cached()` blake2b LRU 2000 |
| 8 | 去双重检测 (P1) | `content_router.py` | Pass 1 检测结果传入 `compress()` |
| 9 | Mixed 段级并行 (P2) | `content_router.py` | `_compress_mixed()` 串行循环 → 线程池 |

## GPU 配置

```ini
# systemd service (~/.config/systemd/user/headroom-proxy.service)
Environment=LD_LIBRARY_PATH=/usr/local/cuda-12.6/lib64
Environment=HEADROOM_KOMPRESS_BACKEND=pytorch
Environment=HEADROOM_EMBEDDER_RUNTIME=pytorch_cuda
Environment=HEADROOM_COMPRESS_WORKERS=8
```

- GPU: NVIDIA GeForce GTX 1070 Ti (SM 6.1)
- CUDA: 12.6
- PyTorch: 2.5.1+cu124（与 CUDA 12.6 兼容的最新版本）
- cuDNN: pip `nvidia-cudnn-cu12==9.6.0.74`（SM 6.1 最后支持的系列）

## 部署注意

```bash
# ❌ 禁止！这会覆盖所有 GPU 配置
uv tool install --force "headroom-ai[all]"

# ✅ 如需要重建，重建后必须重新执行 PyTorch 降级：
uv pip install torch==2.5.1+cu124 --index-url https://download.pytorch.org/whl/cu124
uv pip install nvidia-cudnn-cu12==9.6.0.74
```

## 补丁文件

所有自定义补丁集中管理：

| 文件 | 说明 |
|------|------|
| `~/.headroom_patches/sitecustomize.py` | CJK 保护 + x-headroom-base-url 双路由 |
| `headroom/transforms/content_router.py` (本 fork) | P0/P1/P2 管线优化 |
| `~/.local/bin/headroom-opencode-proxy` | 包装脚本，从 auth.json 读 key |

## 上游文档

→ [原始 README（chopratejas/headroom）](https://github.com/chopratejas/headroom#readme)
→ [官方文档](https://headroom-docs.vercel.app/docs)
→ [Kompress 模型](https://huggingface.co/chopratejas/kompress-v2-base)
