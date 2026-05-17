"""Canonical task/subtask compatibility state models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class LegacySubtaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _status_token(status: Any) -> str:
    if isinstance(status, Enum):
        return status.value
    return str(status)


@dataclass
class LegacySubtaskCheckpoint:
    subtask_id: str
    parent_task_id: str
    status: LegacySubtaskStatus
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "parent_task_id": self.parent_task_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "attempt_count": self.attempt_count,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LegacySubtaskCheckpoint":
        return cls(
            subtask_id=data["subtask_id"],
            parent_task_id=data["parent_task_id"],
            status=LegacySubtaskStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            attempt_count=int(data.get("attempt_count", 0) or 0),
            error_message=data.get("error_message"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    dependencies: list[str] = field(default_factory=list)
    subtasks: list[str] = field(default_factory=list)
    active_subtask: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "dependencies": self.dependencies,
            "subtasks": self.subtasks,
            "active_subtask": self.active_subtask,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskState":
        return cls(
            task_id=data["task_id"],
            status=TaskStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            dependencies=list(data.get("dependencies", [])),
            subtasks=list(data.get("subtasks", [])),
            active_subtask=data.get("active_subtask"),
            metadata=dict(data.get("metadata", {})),
        )


class StateManager:
    """Legacy task/subtask state manager retained for backward compatibility."""

    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir
        self._tasks: dict[str, TaskState] = {}
        self._subtasks: dict[str, LegacySubtaskCheckpoint] = {}
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._load_state()

    def create_task(
        self,
        task_id: str,
        dependencies: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskState:
        now = datetime.now()
        task = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            dependencies=dependencies or [],
            metadata=metadata or {},
        )
        self._tasks[task_id] = task
        self._persist_task(task)
        return task

    def create_subtask(
        self,
        subtask_id: str,
        parent_task_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> LegacySubtaskCheckpoint:
        if parent_task_id not in self._tasks:
            raise ValueError(f"Parent task {parent_task_id} not found")
        now = datetime.now()
        subtask = LegacySubtaskCheckpoint(
            subtask_id=subtask_id,
            parent_task_id=parent_task_id,
            status=LegacySubtaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        parent = self._tasks[parent_task_id]
        if subtask_id not in parent.subtasks:
            parent.subtasks.append(subtask_id)
            parent.updated_at = now
            self._persist_task(parent)
        self._subtasks[subtask_id] = subtask
        self._persist_subtask(subtask)
        return subtask

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        metadata: dict[str, Any] | None = None,
    ) -> TaskState:
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} not found")
        task = self._tasks[task_id]
        task.status = status
        task.updated_at = datetime.now()
        if metadata:
            task.metadata.update(metadata)
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            self._cleanup_task_subtasks(task_id)
        self._persist_task(task)
        return task

    def _normalize_legacy_subtask_status(self, status: Any) -> LegacySubtaskStatus:
        if isinstance(status, LegacySubtaskStatus):
            return status
        mapping = {
            "PENDING": LegacySubtaskStatus.PENDING,
            "RUNNING": LegacySubtaskStatus.IN_PROGRESS,
            "DONE": LegacySubtaskStatus.COMPLETED,
            "FAILED": LegacySubtaskStatus.FAILED,
            "pending": LegacySubtaskStatus.PENDING,
            "in_progress": LegacySubtaskStatus.IN_PROGRESS,
            "completed": LegacySubtaskStatus.COMPLETED,
            "failed": LegacySubtaskStatus.FAILED,
            "skipped": LegacySubtaskStatus.SKIPPED,
        }
        raw = _status_token(status)
        if raw in mapping:
            return mapping[raw]
        return LegacySubtaskStatus(raw)

    def update_subtask_status(
        self,
        subtask_id: str,
        status: Any,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LegacySubtaskCheckpoint:
        if subtask_id not in self._subtasks:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self._subtasks[subtask_id]
        normalized = self._normalize_legacy_subtask_status(status)
        subtask.status = normalized
        subtask.updated_at = datetime.now()
        if error_message:
            subtask.error_message = error_message
        if metadata:
            subtask.metadata.update(metadata)
        if normalized == LegacySubtaskStatus.FAILED:
            subtask.attempt_count += 1
        self._persist_subtask(subtask)
        return subtask

    def set_active_subtask(self, task_id: str, subtask_id: str | None) -> None:
        if task_id not in self._tasks:
            raise ValueError(f"Task {task_id} not found")
        task = self._tasks[task_id]
        task.active_subtask = subtask_id
        task.updated_at = datetime.now()
        self._persist_task(task)

    def get_task(self, task_id: str) -> TaskState | None:
        return self._tasks.get(task_id)

    def get_subtask(self, subtask_id: str) -> LegacySubtaskCheckpoint | None:
        return self._subtasks.get(subtask_id)

    def get_task_subtasks(self, task_id: str) -> list[LegacySubtaskCheckpoint]:
        if task_id not in self._tasks:
            return []
        task = self._tasks[task_id]
        return [self._subtasks[sid] for sid in task.subtasks if sid in self._subtasks]

    def check_dependencies_met(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        task = self._tasks[task_id]
        return all(
            dep_task is not None and dep_task.status == TaskStatus.COMPLETED
            for dep_task in (self._tasks.get(dep_id) for dep_id in task.dependencies)
        )

    def _cleanup_task_subtasks(self, task_id: str) -> None:
        if task_id not in self._tasks:
            return
        task = self._tasks[task_id]
        for subtask_id in task.subtasks:
            self._subtasks.pop(subtask_id, None)
            self._delete_subtask_file(subtask_id)
        task.subtasks = []
        task.active_subtask = None
        task.updated_at = datetime.now()
        self._persist_task(task)

    def _delete_subtask_file(self, subtask_id: str) -> None:
        if not self.state_dir:
            return
        subtask_file = self.state_dir / f"subtask_{subtask_id}.json"
        if subtask_file.exists():
            subtask_file.unlink()

    def _persist_task(self, task: TaskState) -> None:
        self._persist_json(f"task_{task.task_id}.json", task.to_dict())

    def _persist_subtask(self, subtask: LegacySubtaskCheckpoint) -> None:
        self._persist_json(f"subtask_{subtask.subtask_id}.json", subtask.to_dict())

    def _persist_json(self, filename: str, payload: dict[str, Any]) -> None:
        if not self.state_dir:
            return
        target = self.state_dir / filename
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_state(self) -> None:
        if not self.state_dir or not self.state_dir.exists():
            return
        self._load_task_files()
        self._load_subtask_files()

    def _load_task_files(self) -> None:
        assert self.state_dir is not None
        for task_file in self.state_dir.glob("task_*.json"):
            data = json.loads(task_file.read_text(encoding="utf-8"))
            task = TaskState.from_dict(data)
            self._tasks[task.task_id] = task

    def _load_subtask_files(self) -> None:
        assert self.state_dir is not None
        for subtask_file in self.state_dir.glob("subtask_*.json"):
            data = json.loads(subtask_file.read_text(encoding="utf-8"))
            subtask = LegacySubtaskCheckpoint.from_dict(data)
            self._subtasks[subtask.subtask_id] = subtask


__all__ = [
    "LegacySubtaskCheckpoint",
    "LegacySubtaskStatus",
    "StateManager",
    "TaskState",
    "TaskStatus",
]
