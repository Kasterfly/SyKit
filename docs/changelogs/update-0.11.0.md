# SyKit 0.11.0 - Background Update

SyKit 0.11.0 adds persistent work that continues after an endpoint returns,
UTC cron schedules, and task-aware graceful shutdown.

## Added

- `@task` marks synchronous or asynchronous top-level functions for
  background execution.
- `enqueue(task, *args, **kwargs)` persists JSON-compatible arguments before
  returning an opaque task id.
- `@scheduled("minute hour day month weekday")` declares parameterless UTC
  cron jobs discovered and validated at build time.
- A process-safe sqlite queue provides atomic claims, renewable leases,
  failure records, crash recovery, and scheduled-occurrence deduplication.
- `task-store` follows the existing `scheme:target` provider pattern so
  packages can supply shared database queues.
- `task-concurrency` controls runners per server process.
- Readiness checks include the task store when the build contains tasks.
- Generated Compose files allow one minute for in-flight work to drain after
  SIGTERM.

## Behavior

- Delivery is at least once. Calls recovered after process loss can repeat,
  so task effects should be idempotent.
- Task exceptions are logged and recorded as failed without an automatic
  retry.
- Schedules use UTC, do not backfill downtime, and run once per occurrence
  only when every replica shares one task store.
- Shutdown stops new claims and waits for in-flight calls to finish. A forced
  kill leaves the call recoverable after its lease expires.

See [Background Tasks](../background-tasks.md) for examples, cron syntax,
deployment guidance, and the package store interface.
