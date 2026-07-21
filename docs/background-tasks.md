# Background Tasks

SyKit can persist work before an endpoint returns, run it outside the request,
and discover cron jobs while building. The public API is `@task`,
`@scheduled`, and `enqueue`.

## Enqueue work

Define tasks as top-level functions under `src/`, then enqueue them from an
endpoint:

```python
from sykit import enqueue, expose, task


@task
def send_receipt(order_id: str) -> None:
    order = load_order(order_id)
    email_receipt(order)


@expose("orders/create")
def create_order(items: list[dict]) -> dict:
    order = save_order(items)
    task_id = enqueue(send_receipt, order["id"])
    return {"order": order, "task_id": task_id}
```

`enqueue` writes the call to the configured task store before returning its
opaque task id. It is available while an endpoint or another background task
is running. Calling it during module import, from an arbitrary script, or
before the built app starts raises `RuntimeError`.

Task arguments and keyword arguments must be JSON serializable. Pass stable
identifiers and load current records inside the task. Request objects,
`Upload` values, open files, and custom class instances cannot be persisted;
avoid copying request-session state into a task even when its values happen to
be JSON compatible. Queue payloads are not encrypted, so do not place secrets
in task arguments unless the store protects them appropriately. The encoded
arguments and keyword arguments may be at most 1 MiB per call.

Tasks may be synchronous or asynchronous. Synchronous functions run in a
thread pool; asynchronous functions run on the server event loop and must not
block it. Return values are discarded. A task can call `enqueue` to create
more work. Endpoint parameter injection and session helpers are unavailable
during background execution.

`task-concurrency` controls how many calls each server process may run at the
same time. It defaults to `1`, so a configuration with two server `workers`
and `task-concurrency` set to `3` can run up to six calls concurrently.

## Scheduled jobs

`@scheduled` makes a parameterless task run on a five-field UTC cron
schedule:

```python
from sykit import scheduled


@scheduled("0 3 * * *")
def remove_expired_exports() -> None:
    delete_expired_exports()
```

The fields are minute, hour, day of month, month, and day of week. Fields are
numeric and support `*`, comma-separated values, ranges, and steps such as
`*/15` or `1-5/2`. Sunday is `0` or `7`. When both day of month and day of
week are restricted, a match on either field runs the job, following standard
cron behavior.

Schedules are evaluated in UTC once the app is running. Startup does not
backfill occurrences missed while the app was offline. The store records an
occurrence key for each matching UTC minute, so multiple schedulers sharing
one store enqueue that occurrence only once.

## Delivery and failures

Delivery is at least once. A worker leases each call and renews the lease
while it runs. If the process disappears, another worker can reclaim the call
after the lease expires. A crash after an external side effect but before the
completion write can therefore run the task again. Design task effects to be
idempotent, commonly by using a unique business record id.

An exception from task code is logged and marks the call failed; it is not
automatically retried. Process loss is different: an expired running lease is
recovered automatically. `task-max-attempts` defaults to `3`; a call reclaimed
after that many claims is marked failed instead of being run again. The
built-in sqlite store retains failed rows for seven days for diagnosis, then
deletes them during queue cleanup. Background failures have no request object
and do not invoke the endpoint error hook.

During graceful shutdown, schedulers stop and workers stop claiming new
calls. The app then waits for every in-flight call to finish. Generated
Compose files set `stop_grace_period: 1m`; increase the deployment platform's
termination grace when legitimate tasks may run longer. A forced kill can
interrupt a call, but its expired lease keeps the persisted call recoverable.

There is no built-in task timeout. A task that hangs keeps renewing its lease,
occupies one concurrency slot, and can block graceful shutdown. Use bounded
network timeouts and cancellable operations inside tasks. If a process must be
forcibly stopped, its call becomes recoverable after the lease expires and the
attempt cap prevents an endless crash-recovery loop.

## Task stores

`task-store` selects persistence:

- `""` or `"sqlite"`: `.sykit-tasks.sqlite3` beside `built/` for a local app.
- `"sqlite:path/to/tasks.db"`: a custom absolute path, or a path relative to
  the project root. Its parent directory must already exist.
- `"scheme:target"`: a package-provided shared store.

The default sqlite queue safely coordinates server processes on one machine.
Do not put sqlite on a network filesystem. Multiple containers or hosts need
one shared store; otherwise every replica has an independent queue and each
replica can run the same schedule.

In a container, use a mounted path such as
`"sqlite:/data/sykit-tasks.sqlite3"` for one-replica durability. Use a shared
database store for multiple replicas.

### Store package interface

A store package adds `files/core/_taskstore_<scheme>.py` with a
`create(target)` function. It returns a synchronous, thread-safe object with
these methods:

| Method | Contract |
| --- | --- |
| `enqueue(task_name, args, kwargs)` | Persist a manual call and return a unique string id. |
| `enqueue_scheduled(task_name, args, kwargs, schedule_key)` | Atomically persist an occurrence and return its id, or return `None` if that occurrence already exists. |
| `claim(worker_id, lease_seconds)` | Atomically lease one available call, or return `None`. A call is a dictionary with `id`, `task`, `args`, `kwargs`, and `attempt`. |
| `heartbeat(task_id, worker_id, lease_seconds)` | Extend an owned running lease and return whether it was still owned. |
| `complete(task_id, worker_id)` | Remove an owned completed call and return whether it was still owned. |
| `fail(task_id, worker_id, error)` | Persist an owned failure and return whether it was still owned. |
| `release(task_id, worker_id)` | Return an owned interrupted call to the queue and return whether it was still owned. |
| `ready()` | Probe the store without changing queue state; raise when unavailable. |

`claim` and `enqueue_scheduled` are the two atomic operations that guarantee
one active owner and one queued copy per scheduled occurrence. The readiness
route includes the task store when background tasks exist.
