from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from starlette.concurrency import run_in_threadpool

from sykit import tasks as task_api

DEFAULT_LEASE_SECONDS = 300
DEFAULT_POLL_SECONDS = 0.25


def cron_matches(schedule: dict[str, Any], moment: datetime) -> bool:
    """Return whether a generated cron schedule matches a UTC minute."""
    weekday = (moment.weekday() + 1) % 7
    if (
        moment.minute not in schedule["minute"]
        or moment.hour not in schedule["hour"]
        or moment.month not in schedule["month"]
    ):
        return False

    day_matches = moment.day in schedule["day"]
    weekday_matches = weekday in schedule["weekday"]
    if schedule["day_any"] and schedule["weekday_any"]:
        return True
    if schedule["day_any"]:
        return weekday_matches
    if schedule["weekday_any"]:
        return day_matches
    return day_matches or weekday_matches


class TaskManager:
    def __init__(
        self,
        store,
        records: list[dict[str, Any]],
        concurrency: int,
        logger,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self.store = store
        self.records = records
        self.concurrency = concurrency
        self.logger = logger
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._by_name = {record["metadata"]["id"]: record for record in self.records}
        self._names_by_function = {
            record["function"]: record["metadata"]["id"] for record in self.records
        }
        self._stop = asyncio.Event()
        self._runners: list[asyncio.Task] = []

    @staticmethod
    def _payload(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        try:
            encoded = json.dumps(
                {"args": args, "kwargs": kwargs},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            payload = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise TypeError(
                "Background task arguments must be JSON serializable."
            ) from error
        return payload["args"], payload["kwargs"]

    def enqueue(
        self,
        function,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> str:
        task_name = self._names_by_function.get(function)
        if task_name is None:
            raise ValueError(
                "The task was not discovered by the current build. "
                "Decorate a top-level function in src and rebuild."
            )
        normalized_args, normalized_kwargs = self._payload(args, kwargs)
        return self.store.enqueue(task_name, normalized_args, normalized_kwargs)

    async def start(self) -> None:
        if self._runners:
            return
        if self._stop.is_set():
            self._stop = asyncio.Event()
        run_id = uuid.uuid4().hex[:12]
        self._runners = [
            asyncio.create_task(
                self._worker(f"{os.getpid()}:{run_id}:{index}"),
                name=f"sykit-task-worker-{index}",
            )
            for index in range(self.concurrency)
        ]
        if any(record["metadata"].get("schedule") for record in self.records):
            self._runners.append(
                asyncio.create_task(
                    self._scheduler(),
                    name="sykit-task-scheduler",
                )
            )

    async def stop(self) -> None:
        if not self._runners:
            return
        self._stop.set()
        runners = list(self._runners)
        try:
            await asyncio.gather(*runners)
        finally:
            self._runners.clear()

    async def _pause(self, seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _worker(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                job = await run_in_threadpool(
                    self.store.claim,
                    worker_id,
                    self.lease_seconds,
                )
            except Exception:
                self.logger.exception("The background task store is unavailable.")
                await self._pause(1.0)
                continue
            if job is None:
                await self._pause(self.poll_seconds)
                continue
            if self._stop.is_set():
                try:
                    await run_in_threadpool(
                        self.store.release,
                        str(job.get("id", "")),
                        worker_id,
                    )
                except Exception:
                    self.logger.exception(
                        "Could not release a background task claimed during shutdown."
                    )
                return
            await self._run_job(job, worker_id)

    async def _heartbeat(
        self,
        task_id: str,
        worker_id: str,
        finished: asyncio.Event,
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not finished.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=interval)
            if finished.is_set():
                return
            try:
                renewed = await run_in_threadpool(
                    self.store.heartbeat,
                    task_id,
                    worker_id,
                    self.lease_seconds,
                )
            except Exception:
                self.logger.exception(
                    "Could not renew the lease for background task %s.", task_id
                )
            else:
                if not renewed:
                    self.logger.error(
                        "Lost the lease for background task %s while it was running.",
                        task_id,
                    )
                    return

    async def _run_job(self, job: dict[str, Any], worker_id: str) -> None:
        task_id = str(job.get("id", ""))
        task_name = job.get("task")
        record = self._by_name.get(task_name)
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(task_id, worker_id, finished),
            name=f"sykit-task-heartbeat-{task_id}",
        )
        token = task_api._bind_enqueuer(self.enqueue)
        try:
            try:
                if record is None:
                    raise RuntimeError(f"Unknown background task {task_name!r}.")
                args = job.get("args")
                kwargs = job.get("kwargs")
                if not isinstance(args, list) or not isinstance(kwargs, dict):
                    raise RuntimeError(
                        "The task store returned an invalid task payload."
                    )
                function = record["function"]
                if record["metadata"]["is_async"]:
                    await function(*args, **kwargs)
                else:
                    await run_in_threadpool(function, *args, **kwargs)
            finally:
                finished.set()
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
        except asyncio.CancelledError:
            try:
                await run_in_threadpool(self.store.release, task_id, worker_id)
            except Exception:
                self.logger.exception(
                    "Could not release cancelled background task %s.", task_id
                )
            raise
        except Exception as error:
            self.logger.exception(
                "Background task %s (%s) failed.",
                task_id,
                task_name,
            )
            try:
                saved = await run_in_threadpool(
                    self.store.fail,
                    task_id,
                    worker_id,
                    f"{type(error).__name__}: {error}",
                )
            except Exception:
                self.logger.exception(
                    "Could not record failure for background task %s.", task_id
                )
            else:
                if not saved:
                    self.logger.error(
                        "Could not mark background task %s failed because its "
                        "lease was lost.",
                        task_id,
                    )
        else:
            try:
                completed = await run_in_threadpool(
                    self.store.complete,
                    task_id,
                    worker_id,
                )
            except Exception:
                self.logger.exception(
                    "Could not complete background task %s in the store.", task_id
                )
            else:
                if not completed:
                    self.logger.error(
                        "Could not complete background task %s because its lease "
                        "was lost.",
                        task_id,
                    )
        finally:
            task_api._reset_enqueuer(token)

    async def _scheduler(self) -> None:
        seen_minute: int | None = None
        seen_tasks: set[str] = set()
        scheduled = [
            record for record in self.records if record["metadata"].get("schedule")
        ]
        while not self._stop.is_set():
            minute = int(datetime.now(timezone.utc).timestamp() // 60)
            if minute != seen_minute:
                seen_minute = minute
                seen_tasks.clear()
            moment = datetime.fromtimestamp(minute * 60, timezone.utc)
            for record in scheduled:
                metadata = record["metadata"]
                task_name = metadata["id"]
                if task_name in seen_tasks or not cron_matches(
                    metadata["schedule"], moment
                ):
                    continue
                try:
                    await run_in_threadpool(
                        self.store.enqueue_scheduled,
                        task_name,
                        [],
                        {},
                        f"{task_name}:{minute}",
                    )
                except Exception:
                    self.logger.exception(
                        "Could not enqueue scheduled task %s.", task_name
                    )
                else:
                    seen_tasks.add(task_name)
            await self._pause(1.0)


__all__ = ["TaskManager", "cron_matches"]
