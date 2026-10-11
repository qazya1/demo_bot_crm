from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    AWAITING_ASSIGNMENT = "awaiting_assignment"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    POSTPONED = "postponed"
    DONE = "done"
    CANCELLED = "cancelled"


CANONICAL_ACTIVE_STATUSES = (
    TaskStatus.AWAITING_ASSIGNMENT.value,
    TaskStatus.ASSIGNED.value,
    TaskStatus.IN_PROGRESS.value,
)


class TaskWorkflowError(ValueError):
    """Raised when a transition violates canonical workflow."""


@dataclass(frozen=True)
class TaskSnapshot:
    id: int
    status: str
    assignee_id: int | None


def normalize_status(raw_status: str | None) -> str:
    status = (raw_status or "").strip().lower()
    if status in {"", "new", "waiting"}:
        return TaskStatus.AWAITING_ASSIGNMENT.value
    if status in {
        TaskStatus.AWAITING_ASSIGNMENT.value,
        TaskStatus.ASSIGNED.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.POSTPONED.value,
        TaskStatus.DONE.value,
    }:
        return status
    if status == TaskStatus.CANCELLED.value:
        return status
    raise TaskWorkflowError(f"Unsupported task status: {raw_status}")


def create_task_status() -> str:
    return TaskStatus.AWAITING_ASSIGNMENT.value


def assign_task(*, task: TaskSnapshot, specialist_id: int, actor_is_director: bool) -> str:
    """Назначить задачу специалисту. Задача сразу переходит в «В работе»."""
    if not actor_is_director:
        raise TaskWorkflowError("Only director can assign tasks")
    current = normalize_status(task.status)
    if current not in {TaskStatus.AWAITING_ASSIGNMENT.value, TaskStatus.ASSIGNED.value}:
        raise TaskWorkflowError("Можно назначать только задачи в ожидании или уже назначенные")
    if not specialist_id:
        raise TaskWorkflowError("assignee_id is required")
    # Задача сразу переходит в «В работе» — никаких промежуточных статусов
    return TaskStatus.IN_PROGRESS.value


def start_task(*, task: TaskSnapshot, actor_id: int) -> str:
    """Перевести задачу в «В работе». Идмпотентно: если уже in_progress — возвращает in_progress."""
    current = normalize_status(task.status)
    if current == TaskStatus.IN_PROGRESS.value:
        return TaskStatus.IN_PROGRESS.value
    if current not in {TaskStatus.AWAITING_ASSIGNMENT.value}:
        raise TaskWorkflowError("Можно начать только задачу в ожидании назначения")
    if not task.assignee_id or int(task.assignee_id) != int(actor_id):
        raise TaskWorkflowError("Начать задачу может только назначенный специалист")
    return TaskStatus.IN_PROGRESS.value


def complete_task(*, task: TaskSnapshot, actor_id: int) -> str:
    if normalize_status(task.status) != TaskStatus.IN_PROGRESS.value:
        raise TaskWorkflowError("Only in_progress tasks can be completed")
    if not task.assignee_id or int(task.assignee_id) != int(actor_id):
        raise TaskWorkflowError("Only assigned specialist can complete task")
    return TaskStatus.DONE.value
