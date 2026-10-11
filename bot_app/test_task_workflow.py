import unittest

from bot_app.task_workflow import (
    TaskSnapshot,
    TaskWorkflowError,
    TaskStatus,
    assign_task,
    complete_task,
    create_task_status,
    normalize_status,
    start_task,
)


class TaskWorkflowUnitTests(unittest.TestCase):
    def test_create_always_awaiting_assignment(self):
        self.assertEqual(create_task_status(), TaskStatus.AWAITING_ASSIGNMENT.value)

    def test_normalize_legacy_new_waiting(self):
        self.assertEqual(normalize_status("new"), TaskStatus.AWAITING_ASSIGNMENT.value)
        self.assertEqual(normalize_status("waiting"), TaskStatus.AWAITING_ASSIGNMENT.value)

    def test_assign_only_director_and_only_once(self):
        task = TaskSnapshot(id=1, status="awaiting_assignment", assignee_id=None)
        self.assertEqual(
            assign_task(task=task, specialist_id=7, actor_is_director=True),
            TaskStatus.ASSIGNED.value,
        )
        with self.assertRaises(TaskWorkflowError):
            assign_task(task=task, specialist_id=7, actor_is_director=False)

        with self.assertRaises(TaskWorkflowError):
            assign_task(
                task=TaskSnapshot(id=1, status="awaiting_assignment", assignee_id=3),
                specialist_id=7,
                actor_is_director=True,
            )

    def test_complete_only_assignee(self):
        task = TaskSnapshot(id=2, status="in_progress", assignee_id=7)
        self.assertEqual(complete_task(task=task, actor_id=7), TaskStatus.DONE.value)
        with self.assertRaises(TaskWorkflowError):
            complete_task(task=task, actor_id=8)

    def test_start_only_assignee_from_assigned_or_awaiting(self):
        self.assertEqual(
            start_task(task=TaskSnapshot(id=3, status="assigned", assignee_id=7), actor_id=7),
            TaskStatus.IN_PROGRESS.value,
        )
        self.assertEqual(
            start_task(task=TaskSnapshot(id=4, status="awaiting_assignment", assignee_id=7), actor_id=7),
            TaskStatus.IN_PROGRESS.value,
        )
        with self.assertRaises(TaskWorkflowError):
            start_task(task=TaskSnapshot(id=5, status="assigned", assignee_id=7), actor_id=8)


if __name__ == "__main__":
    unittest.main()
