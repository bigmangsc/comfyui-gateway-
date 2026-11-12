from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


_load_dotenv(PROJECT_ROOT / ".env")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # 服务（本地工具，默认只监听 127.0.0.1）
    host: str = os.getenv("APP_HOST", "127.0.0.1")
    port: int = int(os.getenv("APP_PORT", "19613"))
    # 本地 ComfyUI
    comfy_url: str = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
    # 路径
    workflow_file: Path = _env_path("WORKFLOW_FILE", PROJECT_ROOT / "config" / "workflow.json")
    workflows_manifest: Path = _env_path(
        "WORKFLOWS_MANIFEST", PROJECT_ROOT / "config" / "workflows.json"
    )
    database_file: Path = _env_path("DATABASE_FILE", PROJECT_ROOT / "data" / "app.sqlite3")
    jobs_dir: Path = _env_path("JOBS_DIR", PROJECT_ROOT / "data" / "jobs")
    log_dir: Path = _env_path("LOG_DIR", PROJECT_ROOT / "data" / "logs")
    skill_file: Path = _env_path(
        "SKILL_FILE", PROJECT_ROOT / "data" / "image-prompt-skill.md"
    )
    # 工作流节点 ID
    prompt_node_id: str = os.getenv("PROMPT_NODE_ID", "2")
    negative_prompt_node_id: str = os.getenv("NEGATIVE_PROMPT_NODE_ID", "3")
    # 提示词
    default_prompt: str = os.getenv(
        "DEFAULT_PROMPT",
        "a cinematic illustration of a quiet mountain lake at sunrise, soft mist over the water, warm golden light, detailed textures, no text, no watermark.",
    )
    prompt_max_length: int = int(os.getenv("PROMPT_MAX_LENGTH", "8000"))
    # 结果保留（本地工具默认保留 7 天）
    result_ttl_hours: int = int(os.getenv("RESULT_TTL_HOURS", "168"))
    auto_delete_expired: bool = _env_bool("AUTO_DELETE_EXPIRED", True)
    # 队列与轮询
    queue_timeout_hours: int = int(os.getenv("QUEUE_TIMEOUT_HOURS", "24"))
    comfy_poll_seconds: float = float(os.getenv("COMFY_POLL_SECONDS", "2"))
    comfy_timeout_seconds: float = float(os.getenv("COMFY_REQUEST_TIMEOUT", "15"))
    comfy_status_timeout_seconds: float = float(os.getenv("COMFY_STATUS_TIMEOUT", "3"))
    comfy_missing_grace_seconds: int = int(
        os.getenv("COMFY_MISSING_GRACE_SECONDS", "1200")
    )
    comfy_auto_requeue_max: int = int(os.getenv("COMFY_AUTO_REQUEUE_MAX", "3"))
    comfy_download_retry_max: int = int(os.getenv("COMFY_DOWNLOAD_RETRY_MAX", "3"))


settings = Settings()
