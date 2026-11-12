from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from .comfy import ComfyClient, ComfyError
from .config import Settings
from .store import TaskStore, iso, parse_iso, utc_now
from .task_metadata import update_task_metadata
from .thumbnails import ensure_display, ensure_thumbnail
from .workflow import WorkflowError, WorkflowTemplate

logger = logging.getLogger("comfy_gateway.runner")

_AUTO_CLEANUP_INTERVAL_SECONDS = 60.0
_RESULT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

_LORA_ERROR_HINTS = ("lora", "loraloader", "lora key not loaded", "failed to load lora")
_UNRECOVERABLE_ERROR_HINTS = (
    "prompt outputs failed validation",
    "invalid prompt",
    "value not in list",
    "required input is missing",
    "invalid input",
    "rejected workflow nodes",
    "node_errors",
    "missing node",
    "model not found",
)


class JobRunner:
    """本地单人调度器：维护一个任务队列并轮询本地 ComfyUI。"""

    def __init__(
        self,
        store: TaskStore,
        comfy: ComfyClient,
        workflow: WorkflowTemplate,
        settings: Settings,
    ):
        self.store = store
        self.comfy = comfy
        self.workflow = workflow
        self.settings = settings
        self.jobs_dir = settings.jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_auto_cleanup = 0.0
        self._thread = threading.Thread(
            target=self._run, name="comfy-job-runner", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    # ---------- 主循环 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = utc_now()
                self._expire_due_tasks(now)
                self._auto_cleanup(now)
                self._submit_queued()
                self._poll_active()
            except Exception:
                logger.exception("job runner loop failed")
            self._wake.wait(self.settings.comfy_poll_seconds)
            self._wake.clear()

    # ---------- 过期与清理 ----------

    def _expire_due_tasks(self, now: Any) -> None:
        queue_timeout = timedelta(hours=max(1, self.settings.queue_timeout_hours))
        for task in self.store.active_tasks():
            created_at = parse_iso(task.get("created_at"))
            if created_at is None:
                continue
            if now - created_at > queue_timeout:
                self._mark_failed(
                    task["job_id"],
                    f"排队超时（超过 {self.settings.queue_timeout_hours} 小时），"
                    "请检查 ComfyUI 是否在线",
                )
                continue
            if task["status"] in {"submitted", "running"}:
                submitted_at = parse_iso(task.get("submitted_at"))
                if submitted_at and now - submitted_at > queue_timeout:
                    self._mark_failed(
                        task["job_id"], "等待 ComfyUI 完成超时，请检查 ComfyUI 状态"
                    )

    def _auto_cleanup(self, now: Any) -> None:
        if not self.settings.auto_delete_expired:
            return
        monotonic = time.monotonic()
        if monotonic - self._last_auto_cleanup < _AUTO_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_auto_cleanup = monotonic
        job_ids = self.store.expired_job_ids(now)
        if not job_ids:
            return
        removable: list[str] = []
        jobs_root = self.jobs_dir.resolve()
        for job_id in job_ids:
            job_dir = (self.jobs_dir / job_id).resolve()
            if jobs_root not in job_dir.parents:
                continue
            if job_dir.exists():
                try:
                    shutil.rmtree(job_dir)
                except OSError:
                    logger.exception("could not remove job directory %s", job_dir)
                    continue
            removable.append(job_id)
        deleted = self.store.delete_tasks(removable)
        if deleted:
            logger.info("auto cleanup deleted %s expired job(s)", deleted)

    # ---------- 提交 ----------

    def _submit_queued(self) -> None:
        for task in self.store.active_tasks():
            if task["status"] == "queued":
                self._submit_task(task)

    def _submit_task(self, task: dict[str, Any]) -> None:
        job_id = task["job_id"]
        try:
            built = self.workflow.build(
                task["prompt"],
                filename_prefix=f"offline-job-{job_id}",
                negative_prompt=task.get("negative_prompt"),
                steps=task.get("steps"),
                seed=task.get("seed"),
                width=task.get("width"),
                height=task.get("height"),
                layout_json=task.get("layout_json"),
                lora_filename=(
                    None if task.get("lora_disabled") else task.get("lora_filename")
                ),
                lora_strength=(
                    None if task.get("lora_disabled") else task.get("lora_strength")
                ),
                disable_lora=bool(task.get("lora_disabled")),
            )
            prompt_id = self.comfy.submit(built.payload)
        except ComfyError as exc:
            logger.warning("job %s submission failed: %s", job_id, exc)
            if not exc.retryable:
                self._mark_failed(job_id, self._submission_error_message(exc))
            return
        except WorkflowError as exc:
            logger.error("job %s workflow failed: %s", job_id, exc)
            self._mark_failed(job_id, f"工作流模板不可用：{exc}")
            return
        now = utc_now()
        self.store.update_task(
            job_id,
            status="submitted",
            comfy_prompt_id=prompt_id,
            submitted_at=iso(now),
            queue_position=None,
            last_checked_at=iso(now),
            error_message=None,
            negative_prompt=built.negative_prompt,
            steps=built.steps,
            seed=built.seed,
            width=built.width,
            height=built.height,
            lora_filename=None if task.get("lora_disabled") else built.lora_filename,
            lora_strength=None if task.get("lora_disabled") else built.lora_strength,
        )
        self._update_metadata(
            job_id,
            status="submitted",
            comfy_prompt_id=prompt_id,
            submitted_at=iso(now),
            negative_prompt=built.negative_prompt,
            steps=built.steps,
            seed=built.seed,
            width=built.width,
            height=built.height,
            lora_filename=None if task.get("lora_disabled") else built.lora_filename,
            lora_strength=None if task.get("lora_disabled") else built.lora_strength,
            error=None,
        )
        logger.info("job %s submitted to ComfyUI as %s", job_id, prompt_id)

    @staticmethod
    def _submission_error_message(exc: ComfyError) -> str:
        text = str(exc).lower()
        if any(hint in text for hint in _LORA_ERROR_HINTS):
            return f"LoRA 加载失败：{exc}"
        if any(hint in text for hint in _UNRECOVERABLE_ERROR_HINTS):
            return f"工作流模板不可用或参数被 ComfyUI 拒绝：{exc}"
        return f"ComfyUI 拒绝提交任务：{exc}"

    # ---------- 轮询 ----------

    def _poll_active(self) -> None:
        tasks = self.store.tasks_with_prompt_id()
        if not tasks:
            return
        queue_data: dict[str, Any] | None = None
        queue_failed = False
        for task in tasks:
            current = self.store.get_task(task["job_id"])
            if not current or current["status"] not in {"submitted", "running"}:
                continue
            if queue_data is None and not queue_failed:
                try:
                    queue_data = self.comfy.queue()
                except ComfyError as exc:
                    logger.warning("could not read ComfyUI queue: %s", exc)
                    queue_failed = True
                    queue_data = None
            try:
                history = self.comfy.history(current["comfy_prompt_id"])
            except ComfyError as exc:
                logger.warning(
                    "could not poll job %s: %s", current["job_id"], exc
                )
                continue
            if history is not None:
                result = self.comfy.history_result(history)
                if result.status == "completed":
                    self._complete_task(current, result.images[0])
                elif result.status == "failed":
                    message = result.error_message or "ComfyUI 执行失败"
                    if self._is_lora_text(message):
                        message = f"LoRA 加载失败：{message}"
                    self._mark_failed(current["job_id"], message)
                continue
            position: int | None = None
            if queue_data is not None:
                _status, position = self.comfy.prompt_position(
                    queue_data, current["comfy_prompt_id"]
                )
            self._check_missing_task(current, position)

    def _check_missing_task(
        self, task: dict[str, Any], position: int | None
    ) -> None:
        job_id = task["job_id"]
        now = utc_now()
        submitted_at = parse_iso(task.get("submitted_at"))
        missing_too_long = False
        if submitted_at is not None:
            missing_too_long = (
                now - submitted_at
            ).total_seconds() > self.settings.comfy_missing_grace_seconds
        if position is None and missing_too_long:
            requeues = int(task.get("auto_requeue_count") or 0)
            if requeues < max(0, self.settings.comfy_auto_requeue_max):
                self.store.update_task(
                    job_id,
                    status="queued",
                    comfy_prompt_id=None,
                    submitted_at=None,
                    queue_position=None,
                    last_checked_at=iso(now),
                    auto_requeue_count=requeues + 1,
                    error_message=None,
                )
                self._update_metadata(
                    job_id,
                    status="queued",
                    comfy_prompt_id=None,
                    submitted_at=None,
                    auto_requeue_count=requeues + 1,
                    error=None,
                )
                logger.warning(
                    "job %s disappeared from ComfyUI; re-queued (%s/%s)",
                    job_id,
                    requeues + 1,
                    self.settings.comfy_auto_requeue_max,
                )
            else:
                self._mark_failed(
                    job_id, "任务在 ComfyUI 中丢失，重试次数已用尽"
                )
            return
        if position is not None and position != task.get("queue_position"):
            self.store.update_task(
                job_id,
                status="running" if position == 0 else "submitted",
                queue_position=position,
                last_checked_at=iso(now),
            )

    # ---------- 完成 / 失败 ----------

    def _complete_task(self, task: dict[str, Any], image: dict[str, str]) -> None:
        job_id = task["job_id"]
        try:
            content = self.comfy.download_image(image)
        except ComfyError as exc:
            retries = int(task.get("download_retry_count") or 0)
            limit = max(0, self.settings.comfy_download_retry_max)
            if retries < limit:
                now = utc_now()
                self.store.update_task(
                    job_id,
                    download_retry_count=retries + 1,
                    last_checked_at=iso(now),
                    error_message=str(exc),
                )
                logger.warning(
                    "could not download image for job %s (%s/%s): %s",
                    job_id,
                    retries + 1,
                    limit,
                    exc,
                )
                return
            self._mark_failed(job_id, f"结果图片下载失败：{exc}")
            return
        suffix = Path(image.get("filename", "result.png")).suffix.lower()
        if suffix not in _RESULT_SUFFIXES:
            suffix = ".png"
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        result_path = job_dir / f"result{suffix}"
        result_path.write_bytes(content)
        result_sha256 = hashlib.sha256(content).hexdigest()
        media_type = (
            mimetypes.guess_type(result_path.name)[0] or "application/octet-stream"
        )
        now = utc_now()
        expires_at = now + timedelta(hours=self.settings.result_ttl_hours)
        self.store.update_task(
            job_id,
            status="completed",
            queue_position=None,
            result_path=result_path.name,
            result_media_type=media_type,
            completed_at=iso(now),
            expires_at=iso(expires_at),
            last_checked_at=iso(now),
            error_message=None,
        )
        self._update_metadata(
            job_id,
            status="completed",
            completed_at=iso(now),
            expires_at=iso(expires_at),
            result_file=result_path.name,
            result_media_type=media_type,
            result_size_bytes=result_path.stat().st_size,
            result_sha256=result_sha256,
            error=None,
        )
        try:
            ensure_thumbnail(result_path)
            ensure_display(result_path)
        except Exception:
            logger.exception("could not render thumbnails for job %s", job_id)
        logger.info("job %s completed: %s", job_id, result_path.name)

    def _mark_failed(self, job_id: str, message: str) -> None:
        now = utc_now()
        self.store.update_task(
            job_id,
            status="failed",
            error_message=message,
            queue_position=None,
            last_checked_at=iso(now),
        )
        self._update_metadata(
            job_id, status="failed", error=message, failed_at=iso(now)
        )
        logger.error("job %s failed: %s", job_id, message)

    @staticmethod
    def _is_lora_text(text: str | None) -> bool:
        lowered = (text or "").lower()
        return any(hint in lowered for hint in _LORA_ERROR_HINTS)

    def _update_metadata(self, job_id: str, **fields: Any) -> None:
        try:
            update_task_metadata(self.jobs_dir / job_id, **fields)
        except Exception:
            logger.exception("could not update task metadata for job %s", job_id)

    # ---------- 取消 / 删除 ----------

    def cancel(self, job_id: str) -> bool:
        task = self.store.get_task(job_id)
        if not task or task["status"] not in {
            "queued",
            "submitted",
            "running",
        }:
            return False
        prompt_id = task.get("comfy_prompt_id")
        if task["status"] in {"submitted", "running"} and prompt_id:
            try:
                self.comfy.delete_queue_item(prompt_id)
            except ComfyError as exc:
                logger.warning(
                    "could not remove prompt %s from ComfyUI queue: %s",
                    prompt_id,
                    exc,
                )
        now = utc_now()
        self.store.update_task(
            job_id,
            status="cancelled",
            queue_position=None,
            last_checked_at=iso(now),
            error_message="已取消",
        )
        self._update_metadata(
            job_id, status="cancelled", cancelled_at=iso(now)
        )
        logger.info("job %s cancelled", job_id)
        return True

    def delete(self, job_id: str) -> bool:
        job_dir = self.jobs_dir / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        deleted = self.store.delete_task(job_id)
        if deleted:
            logger.info("job %s deleted", job_id)
        return deleted

    # ---------- 状态 ----------

    def comfy_status(self) -> dict[str, Any]:
        try:
            self.comfy.status(timeout=self.settings.comfy_status_timeout_seconds)
            queue_data = self.comfy.queue()
        except ComfyError:
            return {
                "reachable": False,
                "comfy_running": 0,
                "comfy_pending": 0,
                "local_queued": len(
                    [t for t in self.store.active_tasks() if t["status"] == "queued"]
                ),
                "local_active": len(
                    [t for t in self.store.active_tasks() if t["status"] != "queued"]
                ),
            }
        return {
            "reachable": True,
            "comfy_running": len(queue_data.get("queue_running") or []),
            "comfy_pending": len(queue_data.get("queue_pending") or []),
            "local_queued": len(
                [t for t in self.store.active_tasks() if t["status"] == "queued"]
            ),
            "local_active": len(
                [t for t in self.store.active_tasks() if t["status"] != "queued"]
            ),
        }
