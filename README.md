# ComfyUI 离线生图

一个本地单人使用的 ComfyUI 生图工具：在浏览器里填提示词、排队生成、查看与下载图片，全部数据只保存在本机。

> 这是从多人生图网关（[comfyui-gateway](https://github.com/DYS763/comfyui-gateway)）改造而来的离线单机版，去掉了密钥、账号、额度、管理后台、公开画廊等多用户功能，仅保留本地生图核心。

## 功能

- 本地 Web 界面：提示词 / 负面提示词 / 步数 / 种子 / 分辨率 / LoRA
- 支持 MOSS-Image 工作流的 layout JSON 专家模式
- 任务队列：排队、轮询、取图、取消、删除
- 历史任务列表：缩略图、查看大图、下载原图、复制参数重新生成
- 自动清理过期任务图片；应用重启后未完成任务自动恢复轮询
- 完全离线运行：无任何外部脚本、字体、统计服务

## 环境要求

- Python 3.10+
- 本机可用的 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 服务
- ComfyUI 中已安装工作流需要的自定义节点和模型

## 快速开始

### 1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

### 2. 准备配置

```bash
copy .env.example .env   # Windows
cp .env.example .env     # Linux/macOS
```

编辑 `.env`，至少确认：

```dotenv
APP_HOST=127.0.0.1
APP_PORT=19613
COMFYUI_URL=http://127.0.0.1:8188
```

### 3. 配置工作流

编辑 `config/workflow.json`，按你的 ComfyUI 工作流修改：

- `PROMPT_NODE_ID` 默认 `2`，对应正向提示词 `CLIPTextEncode` 节点
- `NEGATIVE_PROMPT_NODE_ID` 默认 `3`，对应负面提示词 `CLIPTextEncode` 节点
- 至少包含 `KSampler`、带 `width/height` 的 Latent 节点、`SaveImage`
- 可选包含一个 `LoraLoader` 或 `LoraLoaderModelOnly`，用于 LoRA 选择

默认模板使用 MOSS-Image 工作流，依赖：

- `moss_image_sft_int8_convrot.safetensors`
- `qwen3.5_9b_moss_image_sft_w4a8_gptq.safetensors`
- `flux2-vae_small.safetensors`
- `example_lora.safetensors`（占位文件名，不存在时会生成失败；不需要 LoRA 时请在提交时勾选「禁用 LoRA」，或删除工作流中的 LoRA 节点）

如果你的节点 ID 或模型不同，直接修改 `config/workflow.json` 和 `.env` 中的 `PROMPT_NODE_ID` / `NEGATIVE_PROMPT_NODE_ID`。

### 4. 启动

```bash
python -m app
```

或：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 19613
```

### 5. 打开页面

浏览器访问 `http://127.0.0.1:19613/`，填写提示词点击「开始生成」即可。

## 配置说明

完整配置项见 [`.env.example`](.env.example)。常用项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `127.0.0.1` | 监听地址；本机使用建议保持默认 |
| `APP_PORT` | `19613` | 监听端口 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | 本地 ComfyUI 地址 |
| `WORKFLOW_FILE` | `config/workflow.json` | 工作流模板路径 |
| `DATABASE_FILE` | `data/app.sqlite3` | SQLite 任务记录 |
| `JOBS_DIR` | `data/jobs` | 生成图片目录 |
| `PROMPT_NODE_ID` | `2` | 正向提示词节点 ID |
| `NEGATIVE_PROMPT_NODE_ID` | `3` | 负面提示词节点 ID |
| `PROMPT_MAX_LENGTH` | `8000` | 提示词最大字符数 |
| `RESULT_TTL_HOURS` | `168` | 结果保留时长（小时），过期自动清理 |
| `AUTO_DELETE_EXPIRED` | `1` | 是否自动删除过期任务图片与记录 |
| `QUEUE_TIMEOUT_HOURS` | `24` | 排队/等待节点超时（小时） |
| `COMFY_POLL_SECONDS` | `2` | 轮询间隔（秒） |
| `COMFY_REQUEST_TIMEOUT` | `15` | 提交请求超时（秒） |
| `COMFY_MISSING_GRACE_SECONDS` | `1200` | 任务在 ComfyUI 中丢失的宽限时间 |
| `COMFY_AUTO_REQUEUE_MAX` | `3` | 任务丢失自动重新排队上限 |
| `COMFY_DOWNLOAD_RETRY_MAX` | `3` | 结果下载重试上限 |

## API

所有接口均为 JSON，除取图接口返回图片文件外。

### 提交任务

```bash
curl -X POST http://127.0.0.1:19613/api/submit \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "a cinematic mountain lake at sunrise",
    "steps": 50,
    "width": 1024,
    "height": 1024
  }'
```

主要字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `prompt` | 二选一 | 正向提示词；使用 `layout` 时留空 |
| `layout` | 二选一 | MOSS-Image layout JSON，不能与 `prompt` 同时使用 |
| `negative_prompt` | 否 | 负面提示词 |
| `steps` | 否 | 3~70 |
| `seed` | 否 | 随机种子，不填则随机 |
| `width` / `height` | 否 | 分辨率，按 16 像素对齐 |
| `lora_filename` | 否 | LoRA 文件名，留空使用工作流默认值 |
| `lora_strength` | 否 | 0~2，最多两位小数 |
| `disable_lora` | 否 | 本次不使用 LoRA |

响应示例：

```json
{
  "task": {
    "job_id": "xxx",
    "status": "queued",
    "queue_position": 1
  }
}
```

### 查询任务

```bash
curl -X POST http://127.0.0.1:19613/api/query \
  -H "Content-Type: application/json" \
  -d '{"job_id": "xxx"}'
```

### 任务列表

```bash
curl http://127.0.0.1:19613/api/tasks
```

### 取图

```bash
curl http://127.0.0.1:19613/api/result/xxx --output result.png
```

另有 `/api/result/{job_id}/display`（大图预览）和 `/api/result/{job_id}/thumbnail`（缩略图）。

### 取消 / 删除

```bash
curl -X POST http://127.0.0.1:19613/api/cancel \
  -H "Content-Type: application/json" \
  -d '{"job_id": "xxx"}'

curl -X POST http://127.0.0.1:19613/api/delete \
  -H "Content-Type: application/json" \
  -d '{"job_id": "xxx"}'
```

### ComfyUI 状态

```bash
curl http://127.0.0.1:19613/api/comfyui/status
```

## 数据与隐私

- 所有数据只保存在本机：任务记录在 `data/app.sqlite3`，图片在 `data/jobs/<job_id>/`，日志在 `data/logs/`。
- 每个任务目录中有 `metadata.json`，记录提示词、参数、时间等信息。
- 默认只监听 `127.0.0.1`，请勿在无保护的情况下改成 `0.0.0.0` 暴露到公网。
- 过期任务（超过 `RESULT_TTL_HOURS`）会在后台被自动删除，请及时保存需要的图片。
- 页面不加载任何外部资源，可在无外网环境下使用。

## 目录结构

```text
app/
  main.py              FastAPI 入口与 API
  config.py            环境变量与默认配置
  store.py             本地任务存储（SQLite）
  runner.py            队列调度与 ComfyUI 轮询
  comfy.py             ComfyUI HTTP 客户端
  workflow.py          工作流模板解析与参数注入
  layout.py            MOSS-Image layout JSON 处理
  task_metadata.py     任务元数据文件
  thumbnails.py        缩略图与预览图生成
  http_cache.py        静态文件缓存响应
  static/              本地前端页面
config/
  workflow.json        ComfyUI 工作流模板
data/
  image-prompt-skill.md
tools/
  png_prompt.py        从 PNG 中读取提示词信息
```

## License

[MIT](LICENSE)
