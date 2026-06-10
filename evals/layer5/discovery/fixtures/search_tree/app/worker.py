def run(job, timeout=60):
    job.execute(deadline=timeout)
