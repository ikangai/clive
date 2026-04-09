# tests/test_coordinator.py
from coordinator import TaskCoordinator, SplitResult
from server.queue import JobQueue, JobStatus

def test_coordinator_splits_task(tmp_path):
    """Coordinator must split a task into sub-tasks and enqueue them."""
    q = JobQueue(str(tmp_path))
    coord = TaskCoordinator(queue=q)
    subtasks = ["research topic A", "research topic B", "research topic C"]
    result = coord.dispatch(subtasks, user="test", toolset="minimal")
    assert len(result.job_ids) == 3

def test_coordinator_collects_results(tmp_path):
    """Coordinator must collect results from completed sub-tasks."""
    q = JobQueue(str(tmp_path))
    coord = TaskCoordinator(queue=q)
    subtasks = ["task 1", "task 2"]
    result = coord.dispatch(subtasks, user="test", toolset="minimal")

    # Simulate workers completing the jobs
    for job_id in result.job_ids:
        q.dequeue()  # mark as running
    for job_id in result.job_ids:
        q.complete(job_id, result=f"result for {job_id}", status=JobStatus.COMPLETED)

    results = coord.collect(result.job_ids)
    assert len(results) == 2
    assert all(r is not None for r in results)

def test_coordinator_handles_partial_failure(tmp_path):
    """Coordinator must handle some sub-tasks failing."""
    q = JobQueue(str(tmp_path))
    coord = TaskCoordinator(queue=q)
    subtasks = ["ok task", "failing task"]
    result = coord.dispatch(subtasks, user="test", toolset="minimal")

    # Complete first, fail second
    q.dequeue()
    q.complete(result.job_ids[0], result="success", status=JobStatus.COMPLETED)
    q.dequeue()
    q.complete(result.job_ids[1], result="error", status=JobStatus.FAILED)

    results = coord.collect(result.job_ids)
    assert results[0].status == JobStatus.COMPLETED
    assert results[1].status == JobStatus.FAILED

def test_coordinator_empty_subtasks(tmp_path):
    """Coordinator must handle empty subtask list."""
    q = JobQueue(str(tmp_path))
    coord = TaskCoordinator(queue=q)
    result = coord.dispatch([], user="test", toolset="minimal")
    assert len(result.job_ids) == 0

def test_split_result_summary(tmp_path):
    """SplitResult must provide a summary."""
    q = JobQueue(str(tmp_path))
    coord = TaskCoordinator(queue=q)
    result = coord.dispatch(["a", "b"], user="test", toolset="minimal")
    assert "2" in result.summary()
