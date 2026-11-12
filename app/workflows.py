from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings
from .workflow import WorkflowError, WorkflowTemplate

logger = logging.getLogger("comfy_gateway.workflows")


@dataclass(frozen=True)
class WorkflowEntry:
    id: str
    name: str
    file: Path
    default: bool
    template: WorkflowTemplate
    default_lora: str | None
    supports_layout: bool = False
    has_lora_node: bool = False

    @property
    def has_lora(self) -> bool:
        return bool(self.default_lora)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "default": self.default,
            "default_lora": self.default_lora,
            "has_lora": self.has_lora,
            "has_lora_node": self.has_lora_node,
            "supports_layout": self.supports_layout,
        }


class WorkflowRegistry:
    """从 config/workflows.json 清单加载多个工作流模板。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.entries: dict[str, WorkflowEntry] = {}
        self.default_id: str | None = None
        self._load()

    def _load(self) -> None:
        manifest = self.settings.workflows_manifest
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("could not load workflows manifest %s: %s", manifest, exc)
                data = {}
            items = data.get("workflows") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                workflow_id = str(item.get("id") or "").strip()
                if not workflow_id or workflow_id in self.entries:
                    continue
                file_value = str(item.get("file") or "").strip()
                if not file_value:
                    continue
                path = Path(file_value)
                if not path.is_absolute():
                    path = PROJECT_ROOT / path
                entry = self._make_entry(
                    workflow_id,
                    str(item.get("name") or workflow_id),
                    path,
                    bool(item.get("default")),
                    bool(item.get("supports_layout")),
                )
                if entry is not None:
                    self.entries[workflow_id] = entry
                    if entry.default and self.default_id is None:
                        self.default_id = workflow_id
        if not self.entries:
            # 没有清单时回退到单一工作流（旧版 WORKFLOW_FILE 配置）
            entry = self._make_entry(
                "default", "默认工作流", self.settings.workflow_file, True, True
            )
            if entry is not None:
                self.entries["default"] = entry
        if self.default_id is None or self.default_id not in self.entries:
            self.default_id = next(iter(self.entries), None)
        logger.info(
            "loaded %s workflow(s), default=%s",
            len(self.entries),
            self.default_id,
        )

    def _make_entry(
        self,
        workflow_id: str,
        name: str,
        path: Path,
        default: bool,
        supports_layout: bool = False,
    ) -> WorkflowEntry | None:
        if not path.is_file():
            logger.error("workflow %s file missing: %s", workflow_id, path)
            return None
        template = WorkflowTemplate(
            path,
            self.settings.prompt_node_id,
            self.settings.negative_prompt_node_id,
        )
        try:
            default_lora = template.default_lora_filename()
        except WorkflowError as exc:
            logger.error("workflow %s is invalid: %s", workflow_id, exc)
            default_lora = None
        try:
            has_lora_node = template.has_lora_node()
        except Exception:
            has_lora_node = False
        return WorkflowEntry(
            id=workflow_id,
            name=name,
            file=path,
            default=default,
            template=template,
            default_lora=default_lora,
            supports_layout=supports_layout,
            has_lora_node=has_lora_node,
        )

    def get(self, workflow_id: str | None) -> WorkflowEntry | None:
        if workflow_id and workflow_id in self.entries:
            return self.entries[workflow_id]
        return self.entries.get(self.default_id or "")

    def default_entry(self) -> WorkflowEntry | None:
        return self.entries.get(self.default_id or "")

    def list_public(self) -> list[dict[str, Any]]:
        return [entry.public() for entry in self.entries.values()]
