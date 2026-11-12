from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .comfy import ComfyClient
from .config import Settings, settings
from .http_cache import cached_file_response
from .layout import LayoutError, normalize_layout
from .runner import JobRunner
from .store import TaskStore, iso, utc_now
from .task_metadata import write_task_metadata
from .thumbnails import display_path, thumbnail_path
from .workflow import SEED_MAX, WorkflowError, normalize_dimension
from .workflows import WorkflowRegistry


def configure_logging(config: Settings) -> logging.Logger:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("comfy_gateway")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    handler = TimedRotatingFileHandler(
        config.log_dir / "gateway.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(console)
    return logger


logger = configure_logging(settings)

store = TaskStore(settings.database_file)
comfy = ComfyClient(settings.comfy_url, settings.comfy_timeout_seconds)
workflows = WorkflowRegistry(settings)
_default_entry = workflows.default_entry()
DEFAULT_LORA = _default_entry.default_lora if _default_entry else None
runner = JobRunner(store, comfy, workflows, settings)


# ---------- 请求模型 ----------

class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = Field(default=None, max_length=64)
    prompt: str = Field(default="", max_length=8000)
    layout: dict[str, Any] | None = None
    negative_prompt: str | None = Field(default=None, max_length=8000)
    steps: int | None = Field(default=None, ge=3, le=70)
    seed: int | None = Field(default=None, ge=0, le=SEED_MAX)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    lora_filename: str | None = Field(default=None, max_length=300)
    lora_strength: float | None = Field(default=None, ge=0, le=2)
    disable_lora: bool = False


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)


# ---------- 工具 ----------

def _validate_text(value: str | None, max_length: int, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串")
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符")
    return text or None


def _task_public(task: dict[str, Any]) -> dict[str, Any]:
    status = task["status"]
    has_result = status == "completed" and bool(task.get("result_path"))
    workflow_id = task.get("workflow_id") or ""
    workflow_entry = workflows.get(workflow_id) or workflows.default_entry()
    return {
        "job_id": task["job_id"],
        "workflow_id": workflow_id,
        "workflow_name": workflow_entry.name if workflow_entry else "",
        "status": status,
        "queue_position": task.get("queue_position"),
        "prompt": task.get("prompt") or "",
        "negative_prompt": task.get("negative_prompt"),
        "steps": task.get("steps"),
        "seed": task.get("seed"),
        "width": task.get("width"),
        "height": task.get("height"),
        "layout_json": task.get("layout_json"),
        "lora_filename": task.get("lora_filename"),
        "lora_strength": task.get("lora_strength"),
        "lora_disabled": bool(task.get("lora_disabled")),
        "created_at": task.get("created_at"),
        "submitted_at": task.get("submitted_at"),
        "completed_at": task.get("completed_at"),
        "expires_at": task.get("expires_at"),
        "has_result": has_result,
        "error": task.get("error_message") if status == "failed" else None,
        "result_url": f"/api/result/{task['job_id']}" if has_result else None,
        "display_url": (
            f"/api/result/{task['job_id']}/display" if has_result else None
        ),
        "thumb_url": (
            f"/api/result/{task['job_id']}/thumbnail" if has_result else None
        ),
    }


def _resolve_job_file(job_id: str, filename: str | None) -> Path | None:
    if not filename:
        return None
    candidate = settings.jobs_dir / job_id / Path(filename).name
    candidate = candidate.resolve()
    jobs_root = settings.jobs_dir.resolve()
    if jobs_root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


# ---------- 应用 ----------

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    runner.start()
    logger.info(
        "offline gateway started on http://%s:%s (ComfyUI: %s)",
        settings.host,
        settings.port,
        settings.comfy_url,
    )
    try:
        yield
    finally:
        runner.stop()


app = FastAPI(
    title="ComfyUI Offline Generator",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


# ---------- API ----------

@app.get("/api/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/comfyui/status")
def comfyui_status() -> dict[str, Any]:
    return runner.comfy_status()


@app.get("/api/workflows")
def workflows_list() -> dict[str, Any]:
    return {
        "items": workflows.list_public(),
        "default_id": workflows.default_id,
    }


@app.get("/api/workflow/info")
def workflow_info() -> dict[str, Any]:
    entry = workflows.default_entry()
    return {
        "comfy_url": settings.comfy_url,
        "workflow_id": entry.id if entry else None,
        "workflow_name": entry.name if entry else None,
        "default_lora": entry.default_lora if entry else None,
        "workflow_file": str(entry.file) if entry else str(settings.workflow_file),
        "result_ttl_hours": settings.result_ttl_hours,
        "workflows": workflows.list_public(),
    }


@app.get("/api/tasks")
def list_tasks(limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    tasks = store.list_tasks(limit=limit)
    return {"items": [_task_public(task) for task in tasks], "total": len(tasks)}


@app.post("/api/submit")
def submit(payload: SubmitRequest) -> dict[str, Any]:
    if payload.workflow_id and payload.workflow_id not in workflows.entries:
        raise HTTPException(
            status_code=422, detail=f"工作流不存在: {payload.workflow_id}"
        )
    entry = workflows.get(payload.workflow_id)
    if entry is None:
        raise HTTPException(status_code=422, detail="没有可用的工作流模板")
    workflow_id = entry.id
    try:
        if payload.layout is not None:
            if not entry.supports_layout:
                raise LayoutError(
                    f"工作流「{entry.name}」不支持 layout JSON 模式"
                )
            if payload.prompt.strip():
                raise LayoutError(
                    "专家模式使用 layout JSON 时不能再填写正向提示词"
                )
            layout_result = normalize_layout(
                payload.layout,
                width=normalize_dimension(payload.width),
                height=normalize_dimension(payload.height),
            )
            prompt = layout_result.caption
            layout_json = layout_result.json_text
        else:
            prompt = _validate_text(
                payload.prompt, settings.prompt_max_length, "提示词"
            )
            layout_json = None
            if not prompt:
                raise ValueError("请填写提示词")
        negative_prompt = _validate_text(
            payload.negative_prompt, settings.prompt_max_length, "负面提示词"
        )
        lora_strength = None
        if payload.disable_lora:
            if payload.lora_filename or payload.lora_strength is not None:
                raise ValueError("不能同时指定 LoRA 和禁用 LoRA")
        elif payload.lora_strength is not None:
            lora_strength = float(payload.lora_strength)
            if abs(round(lora_strength, 2) - lora_strength) > 1e-9:
                raise ValueError("LoRA 强度最多保留两位小数")
        width = normalize_dimension(payload.width)
        height = normalize_dimension(payload.height)
    except (ValueError, WorkflowError) as exc:
        logger.warning("submit rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    created_at = utc_now()
    expires_at = created_at + timedelta(hours=settings.result_ttl_hours)
    task_fields = {
        "job_id": job_id,
        "workflow_id": workflow_id,
        "status": "queued",
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "steps": payload.steps,
        "seed": payload.seed,
        "width": width,
        "height": height,
        "layout_json": layout_json,
        "lora_filename": payload.lora_filename,
        "lora_strength": lora_strength,
        "lora_disabled": int(payload.disable_lora),
        "created_at": iso(created_at),
        "expires_at": iso(expires_at),
    }
    try:
        store.create_task(**task_fields)
    except Exception as exc:
        logger.exception("submit failed")
        raise HTTPException(
            status_code=500, detail="could not create generation task"
        ) from exc
    try:
        write_task_metadata(
            settings.jobs_dir / job_id,
            {
                "job_id": job_id,
                "workflow_id": workflow_id,
                "workflow_name": entry.name,
                "status": "queued",
                "created_at": iso(created_at),
                "expires_at": iso(expires_at),
                "submitted_at": None,
                "completed_at": None,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "steps": payload.steps,
                "seed": payload.seed,
                "width": width,
                "height": height,
                "layout_json": layout_json,
                "lora_filename": payload.lora_filename,
                "lora_strength": lora_strength,
                "lora_disabled": payload.disable_lora,
                "workflow_file": str(entry.file),
                "comfy_prompt_id": None,
                "result_file": None,
                "error": None,
            },
        )
    except Exception:
        logger.exception("could not write task metadata for job %s", job_id)
    runner.wake()
    logger.info(
        "submit response job_id=%s workflow_id=%s prompt_chars=%s layout=%s",
        job_id,
        workflow_id,
        len(prompt),
        layout_json is not None,
    )
    return {"task": _task_public(store.get_task(job_id) or {})}


@app.post("/api/query")
def query(payload: JobRequest) -> dict[str, Any]:
    task = store.get_task(payload.job_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": _task_public(task)}


@app.get("/api/result/{job_id}")
def result(job_id: str):
    task = store.get_task(job_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] != "completed" or not task.get("result_path"):
        raise HTTPException(status_code=409, detail="image is not ready")
    result_path = _resolve_job_file(job_id, task.get("result_path"))
    if result_path is None:
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(
        result_path,
        media_type=task.get("result_media_type") or "application/octet-stream",
        filename=result_path.name,
        headers={"Cache-Control": "no-store"},
    )


def _rendered_file(job_id: str, request: Request, path_factory):
    task = store.get_task(job_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] != "completed" or not task.get("result_path"):
        raise HTTPException(status_code=409, detail="image is not ready")
    result_path = _resolve_job_file(job_id, task.get("result_path"))
    if result_path is None:
        raise HTTPException(status_code=404, detail="image not found")
    rendered = path_factory(result_path)
    if not rendered.is_file():
        raise HTTPException(status_code=404, detail="rendered image not found")
    return cached_file_response(
        request,
        rendered,
        media_type="image/jpeg",
        cache_control="private, max-age=86400",
    )


@app.get("/api/result/{job_id}/display")
def result_display(job_id: str, request: Request):
    return _rendered_file(job_id, request, display_path)


@app.get("/api/result/{job_id}/thumbnail")
def result_thumbnail(job_id: str, request: Request):
    return _rendered_file(job_id, request, thumbnail_path)


@app.post("/api/cancel")
def cancel(payload: JobRequest) -> dict[str, Any]:
    if not runner.cancel(payload.job_id):
        task = store.get_task(payload.job_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        raise HTTPException(status_code=409, detail="task is not active")
    return {"cancelled": True, "job_id": payload.job_id}


@app.post("/api/delete")
def delete(payload: JobRequest) -> dict[str, Any]:
    if not runner.delete(payload.job_id):
        raise HTTPException(status_code=404, detail="task not found")
    return {"deleted": True, "job_id": payload.job_id}


@app.get("/download/image-prompt-skill.md")
def download_skill() -> FileResponse:
    skill_file = settings.skill_file.resolve()
    if not skill_file.is_file():
        logger.error("skill file is missing: %s", skill_file)
        raise HTTPException(status_code=404, detail="skill file not found")
    return FileResponse(
        skill_file,
        media_type="text/markdown; charset=utf-8",
        filename="image-prompt-skill.md",
    )


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    logger.exception("unhandled request error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
