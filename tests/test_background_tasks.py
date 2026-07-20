from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import build
from sykit import enqueue, scheduled, task
from sykit import tasks as task_api

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TASK_STORE = _load_module(
    "sykit_test_task_store", ROOT / "files" / "core" / "_task_store.py"
)
TASK_RUNTIME = _load_module(
    "sykit_test_task_runtime", ROOT / "files" / "core" / "_task_runtime.py"
)


class DecoratorTests(unittest.TestCase):
    def test_task_and_schedule_metadata(self) -> None:
        @task
        def manual(value):
            return value

        @scheduled("0 * * * *")
        def hourly():
            return None

        self.assertTrue(manual.__sykit__["task"])
        self.assertTrue(hourly.__sykit__["task"])
        self.assertEqual(hourly.__sykit__["schedule"], "0 * * * *")

    def test_enqueue_requires_a_task_and_active_runtime(self) -> None:
        def ordinary():
            return None

        with self.assertRaisesRegex(TypeError, "decorated"):
            enqueue(ordinary)

        @task
        def background(value):
            return value

        with self.assertRaisesRegex(RuntimeError, "endpoint or background task"):
            enqueue(background, 1)

        calls = []
        token = task_api._bind_enqueuer(
            lambda function, args, kwargs: (
                calls.append((function, args, kwargs)) or "task-id"
            )
        )
        try:
            self.assertEqual(enqueue(background, 2, ready=True), "task-id")
        finally:
            task_api._reset_enqueuer(token)
        self.assertEqual(calls, [(background, (2,), {"ready": True})])


class BuildDiscoveryTests(unittest.TestCase):
    def _parse(self, source_text: str):
        temporary = tempfile.TemporaryDirectory(prefix="sykit-task-build-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "jobs.py"
        source.write_text(textwrap.dedent(source_text), encoding="utf-8")
        return build.parse_tasks(source, root)

    def test_discovers_manual_and_scheduled_tasks(self) -> None:
        tasks = self._parse(
            """
            from sykit import scheduled, task

            @task
            def send_email(user_id, *, urgent=False):
                return None

            @scheduled("*/15 0-6 * * 1-5")
            async def sweep():
                return None
            """
        )
        self.assertEqual(
            [item.task_id for item in tasks], ["jobs:send_email", "jobs:sweep"]
        )
        self.assertIsNone(tasks[0].schedule)
        self.assertTrue(tasks[1].is_async)
        self.assertEqual(tasks[1].schedule["minute"], (0, 15, 30, 45))
        self.assertEqual(tasks[1].schedule["weekday"], (1, 2, 3, 4, 5))
        manifest = build.generate_task_manifest(tasks)
        self.assertIn("'id': 'jobs:sweep'", manifest)
        self.assertIn("TASKS = [", manifest)

    def test_rejects_invalid_task_declarations(self) -> None:
        cases = (
            (
                """
                from sykit import task
                @task()
                def bad(): pass
                """,
                "without arguments",
            ),
            (
                """
                from sykit import scheduled
                @scheduled("* * * *")
                def bad(): pass
                """,
                "five cron fields",
            ),
            (
                """
                from sykit import scheduled
                @scheduled("60 * * * *")
                def bad(): pass
                """,
                "between 0 and 59",
            ),
            (
                """
                from sykit import scheduled
                @scheduled("* * * * *")
                def bad(value): pass
                """,
                "cannot declare parameters",
            ),
            (
                """
                from sykit import expose, task
                @task
                @expose("bad")
                def bad(): pass
                """,
                "cannot use endpoint decorators",
            ),
            (
                """
                from sykit import perms, task
                @perms({"Session": {"admin": True}})
                @task
                def bad(): pass
                """,
                "cannot use endpoint decorators",
            ),
        )
        for source, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(build.BuildError, message),
            ):
                self._parse(source)

    def test_cron_day_and_weekday_use_standard_or_semantics(self) -> None:
        schedule = build.parse_cron("0 0 1 * 1", Path("jobs.py"), 1)
        self.assertTrue(
            TASK_RUNTIME.cron_matches(
                schedule,
                datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(
            TASK_RUNTIME.cron_matches(
                schedule,
                datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc),
            )
        )
        self.assertFalse(
            TASK_RUNTIME.cron_matches(
                schedule,
                datetime(2026, 7, 2, 0, 0, tzinfo=timezone.utc),
            )
        )


class SqliteTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-task-store-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tasks.db"
        self.store = TASK_STORE.SqliteTaskStore(self.path)

    def test_enqueue_claim_complete_and_fail(self) -> None:
        first = self.store.enqueue("jobs:first", [1], {"ready": True})
        job = self.store.claim("worker-1", 60)
        self.assertEqual(job["id"], first)
        self.assertEqual(job["args"], [1])
        self.assertEqual(job["kwargs"], {"ready": True})
        self.assertTrue(self.store.heartbeat(first, "worker-1", 60))
        self.assertFalse(self.store.complete(first, "worker-2"))
        self.assertTrue(self.store.complete(first, "worker-1"))
        self.assertIsNone(self.store.claim("worker-2", 60))

        second = self.store.enqueue("jobs:second", [], {})
        self.assertEqual(self.store.claim("worker-1", 60)["id"], second)
        self.assertTrue(self.store.fail(second, "worker-1", "broken"))
        self.assertIsNone(self.store.claim("worker-2", 60))

    def test_expired_lease_is_recovered(self) -> None:
        with mock.patch.object(TASK_STORE.time, "time", return_value=100.0):
            task_id = self.store.enqueue("jobs:recover", [], {})
            self.assertEqual(self.store.claim("worker-1", 10)["id"], task_id)
        with mock.patch.object(TASK_STORE.time, "time", return_value=109.0):
            self.assertIsNone(self.store.claim("worker-2", 10))
        with mock.patch.object(TASK_STORE.time, "time", return_value=111.0):
            recovered = self.store.claim("worker-2", 10)
        self.assertEqual(recovered["id"], task_id)
        self.assertEqual(recovered["attempt"], 2)

    def test_scheduled_occurrence_is_inserted_once(self) -> None:
        task_id = self.store.enqueue_scheduled("jobs:hourly", [], {}, "jobs:hourly:100")
        self.assertIsNotNone(task_id)
        job = self.store.claim("worker", 60)
        self.assertTrue(self.store.complete(job["id"], "worker"))
        self.assertIsNone(
            self.store.enqueue_scheduled("jobs:hourly", [], {}, "jobs:hourly:100")
        )


class ResolveTaskStoreTests(unittest.TestCase):
    def test_default_and_custom_sqlite_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-task-resolve-") as directory:
            root = Path(directory)
            default = TASK_STORE.resolve_task_store("", root)
            custom = TASK_STORE.resolve_task_store("sqlite:state.db", root)
            self.assertEqual(
                default.database_path,
                root / TASK_STORE.DEFAULT_SQLITE_FILE,
            )
            self.assertEqual(custom.database_path, root / "state.db")

    def test_package_provider_convention(self) -> None:
        class Provider:
            enqueue = enqueue_scheduled = claim = heartbeat = complete = fail = (
                release
            ) = lambda *args, **kwargs: None

            def ready(self):
                return None

        provider = Provider()
        factory = mock.Mock(return_value=provider)
        module = types.SimpleNamespace(create=factory)
        with mock.patch.object(
            TASK_STORE, "import_module", return_value=module
        ) as load:
            resolved = TASK_STORE.resolve_task_store("shared:queue-name", Path.cwd())
        self.assertIs(resolved, provider)
        load.assert_called_once_with("core._taskstore_shared")
        factory.assert_called_once_with("queue-name")

    def test_invalid_and_incomplete_providers_fail(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scheme"):
            TASK_STORE.resolve_task_store("not-valid!:target", Path.cwd())
        module = types.SimpleNamespace(create=lambda target: object())
        with mock.patch.object(TASK_STORE, "import_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                TASK_STORE.resolve_task_store("empty:target", Path.cwd())


class TaskManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sykit-task-manager-")
        self.addCleanup(self.temporary.cleanup)
        self.store = TASK_STORE.SqliteTaskStore(Path(self.temporary.name) / "tasks.db")
        self.logger = logging.getLogger(f"sykit.test.tasks.{id(self)}")

    @staticmethod
    def _record(function, task_id: str, schedule=None):
        return {
            "metadata": {
                "id": task_id,
                "name": function.__name__,
                "module": function.__module__,
                "file": "jobs.py",
                "is_async": inspect.iscoroutinefunction(function),
                "schedule": schedule,
            },
            "function": function,
        }

    async def _wait_for(self, predicate, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Timed out waiting for the background task.")
            await asyncio.sleep(0.01)

    async def test_tasks_execute_and_can_enqueue_another_task(self) -> None:
        results = []

        @task
        async def second(value):
            results.append(value)

        @task
        async def first(value):
            enqueue(second, value + 1)

        manager = TASK_RUNTIME.TaskManager(
            self.store,
            [self._record(first, "jobs:first"), self._record(second, "jobs:second")],
            1,
            self.logger,
            poll_seconds=0.01,
        )
        manager.enqueue(first, (4,), {})
        await manager.start()
        await self._wait_for(lambda: results == [5])
        await manager.stop()

    async def test_stop_waits_for_an_inflight_task(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        finished = []

        @task
        async def slow():
            started.set()
            await release.wait()
            finished.append(True)

        manager = TASK_RUNTIME.TaskManager(
            self.store,
            [self._record(slow, "jobs:slow")],
            1,
            self.logger,
            poll_seconds=0.01,
        )
        manager.enqueue(slow, (), {})
        await manager.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        stopping = asyncio.create_task(manager.stop())
        await asyncio.sleep(0.05)
        self.assertFalse(stopping.done())
        release.set()
        await asyncio.wait_for(stopping, timeout=2)
        self.assertEqual(finished, [True])

    async def test_two_schedulers_create_one_occurrence(self) -> None:
        executions = []

        @scheduled("* * * * *")
        async def heartbeat():
            executions.append(True)

        schedule = build.parse_cron("* * * * *", Path("jobs.py"), 1)
        record = self._record(heartbeat, "jobs:heartbeat", schedule)
        manager_one = TASK_RUNTIME.TaskManager(
            self.store, [record], 1, self.logger, poll_seconds=0.01
        )
        second_store = TASK_STORE.SqliteTaskStore(
            Path(self.temporary.name) / "tasks.db"
        )
        manager_two = TASK_RUNTIME.TaskManager(
            second_store, [record], 1, self.logger, poll_seconds=0.01
        )
        await manager_one.start()
        await manager_two.start()
        await self._wait_for(lambda: len(executions) == 1)
        await asyncio.sleep(0.1)
        await manager_one.stop()
        await manager_two.stop()
        self.assertEqual(executions, [True])

    async def test_enqueue_rejects_non_json_arguments(self) -> None:
        @task
        def background(value):
            return value

        manager = TASK_RUNTIME.TaskManager(
            self.store,
            [self._record(background, "jobs:background")],
            1,
            self.logger,
        )
        with self.assertRaisesRegex(TypeError, "JSON serializable"):
            manager.enqueue(background, ({1, 2},), {})


class RuntimeIntegrationTests(unittest.TestCase):
    def test_endpoint_enqueues_and_worker_runs_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sykit-task-runtime-") as directory:
            runtime = Path(directory) / "project" / "built"
            runtime.mkdir(parents=True)
            (runtime / "core").mkdir()
            (runtime / "app").mkdir()
            (runtime / "static").mkdir()
            shutil.copy2(ROOT / "files" / "server.py", runtime / "server.py")
            for source in (ROOT / "files" / "core").glob("*.py"):
                shutil.copy2(source, runtime / "core" / source.name)
            shutil.copytree(ROOT / "sykit", runtime / "app" / "sykit")
            (runtime / "config.json").write_text(
                json.dumps(
                    {
                        "endpoints": "/api/",
                        "allowed-hosts": ["testserver"],
                        "task-concurrency": 1,
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "app" / "jobs.py").write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path
                    from sykit import task

                    RESULT = Path(__file__).resolve().parents[2] / "task-result.txt"

                    @task
                    def save(value):
                        RESULT.write_text(value, encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            (runtime / "app" / "endpoints.py").write_text(
                textwrap.dedent(
                    """
                    from sykit import enqueue, expose
                    from jobs import save

                    @expose("start")
                    def start(value):
                        return {"task_id": enqueue(save, value)}
                    """
                ),
                encoding="utf-8",
            )
            endpoint = {
                "kind": "expose",
                "method": "POST",
                "endpoint": "start",
                "name": "start",
                "module": "endpoints",
                "file": "endpoints.py",
                "is_async": False,
                "parameters": [
                    {
                        "name": "value",
                        "injected": False,
                        "required": True,
                        "upload": False,
                    }
                ],
                "permissions": {},
                "cors": [],
                "limits": {},
                "hidden": False,
                "token": None,
                "api_key": None,
                "max_upload_bytes": None,
            }
            (runtime / "core" / "_endpoints.py").write_text(
                "from endpoints import start\n"
                f"ENDPOINTS = [{{'metadata': {endpoint!r}, 'function': start}}]\n",
                encoding="utf-8",
            )
            task_metadata = {
                "id": "jobs:save",
                "name": "save",
                "module": "jobs",
                "file": "jobs.py",
                "is_async": False,
                "schedule": None,
            }
            (runtime / "core" / "_tasks.py").write_text(
                "from jobs import save\n"
                f"TASKS = [{{'metadata': {task_metadata!r}, 'function': save}}]\n",
                encoding="utf-8",
            )
            probe = runtime / "probe.py"
            probe.write_text(
                textwrap.dedent(
                    """
                    import time
                    from pathlib import Path

                    from starlette.testclient import TestClient
                    import server

                    result_path = Path(__file__).resolve().parents[1] / "task-result.txt"
                    with TestClient(server.app) as client:
                        response = client.post("/api/start", json={"value": "done"})
                        assert response.status_code == 200, response.text
                        assert len(response.json()["task_id"]) == 32
                        deadline = time.monotonic() + 3
                        while not result_path.exists() and time.monotonic() < deadline:
                            time.sleep(0.02)
                        assert result_path.read_text(encoding="utf-8") == "done"
                    """
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["SYKIT_SESSION_SECRET"] = (
                "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
            )
            result = subprocess.run(
                [sys.executable, str(probe)],
                cwd=runtime,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
