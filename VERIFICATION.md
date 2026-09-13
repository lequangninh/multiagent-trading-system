# M1 verification

M1 implementation is present; its full acceptance gate remains blocked.

Required command:
```
$ docker compose up -d && uv run pytest tests/test_models.py tests/test_bus.py -q
/bin/bash: line 1: docker: command not found
```

Python checks run independently with Python 3.12:
```
$ uv run pytest tests/test_models.py tests/test_bus.py -q
19 passed in 0.34s
$ uv run pytest -q
32 passed in 0.14s
$ uv run ruff check .
All checks passed!
$ uv run python -m swarm.main --check
config ok, sandbox=true
```

Bus tests use an injected simulated Redis transport; they do not verify a running
Redis server. Docker images and all service healthchecks remain unverified.
Dependencies resolved successfully and uv.lock is included.
Run the required command on a Docker-enabled host before advancing to M2.
