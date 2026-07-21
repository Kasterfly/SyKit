# SyKit

**S**velte + P**y**thon **Kit**. Call Python from Svelte like it's a local import.
You write plain Python functions and decorate them. SyKit generates the
client, the routes, and a compiled app.

```python
# src/endpoints.py
from sykit.utils import expose

@expose("ping")
def ping(session: dict):
    session["ping_count"] = session.get("ping_count", 0) + 1
    return {"pong": True, "count": session["ping_count"]}
```

```svelte
<!-- src/App.svelte -->
<script>
  import { ping } from "$python";

  let message = "Ready";

  async function checkBackend() {
    const result = await ping();
    message = result?.pong ? "Pong!" : JSON.stringify(result);
  }
</script>

<button onclick={checkBackend}>Ping Python</button>
<p>{message}</p>
```
## Requirements

- Python 3.11 through 3.14
- Node.js 22.12+ or 24.x on your PATH
- npm (included with standard Node.js installations)

Frontend and backend dependencies are pinned in checked-in lockfiles. The
human-edited `requirements.in` keeps the supported Python ranges.

## Quick start

Clone SyKit into your project directory. It lives alongside your code as a
tool folder, not as an installed library:

```
cd your-project
git clone --branch 0.14.1 --depth 1 https://github.com/Kasterfly/SyKit
python -m pip install --require-hashes -r SyKit/requirements.lock

python SyKit init  # creates src/ with a minimal starter app
python SyKit build # generates endpoints and compiles to built/
```

Then set a session secret and run the built app:

```bash
export SYKIT_SESSION_SECRET="a-long-random-string-of-at-least-32-bytes"   # PowerShell: $env:SYKIT_SESSION_SECRET = "..."
python built/main.py
```
The app serves on `http://127.0.0.1:8000` by default.

Endpoints can persist work and return immediately:

```python
from sykit import enqueue, expose, task


@task
def send_receipt(order_id):
    email_receipt(order_id)


@expose("orders/create")
def create_order(order_id):
    return {"task_id": enqueue(send_receipt, order_id)}
```

Tasks use a sqlite queue by default. Cron schedules, shared stores, delivery
semantics, and shutdown behavior are covered in
[Background Tasks](docs/background-tasks.md).

## Commands

| Command | What it does |
| --- | --- |
| `python SyKit init` | Create `src/sykit` configuration and a minimal starter app |
| `python SyKit build [--dev]` | Detect endpoints, generate the `$python` client, compile into `built/`; `--dev` also runs the app |
| `python SyKit keys <generate\|list\|revoke>` | Manage API keys for `@api_key` endpoints |
| `python SyKit package <add\|remove\|list\|diff>` | Manage packages that extend SyKit; install from local folders, GitHub, or the official packages repo |
| `python SyKit update [source] [--yes] [--allow-unreleased]` | Update from a commit-pinned release; branch content needs the explicit unreleased flag |
| `python SyKit version` | Show the SyKit version |
| `python SyKit help` | Show usage |
Commands operate on the current working directory (your project root).

## Docs

- [Endpoints](docs/endpoints.md): `@expose`, `@raw`, `@sse`, `@web_hook`, sessions,
  permissions, CORS, rate limits
- [Streaming](docs/streaming.md): SSE async iterators, lifecycle, errors,
  guards, session rules, and deployment notes
- [Uploads](docs/uploads.md): multipart `File`/`Blob` calls, disk-backed
  temporary files, size limits, validation, and media storage guidance
- [Background Tasks](docs/background-tasks.md): persistent task calls, UTC
  cron schedules, queue stores, delivery semantics, and graceful shutdown
- [Login and Access](docs/auth.md): password helpers, `login`/`logout`,
  permission-gated pages, and server-side session stores
- [API Keys](docs/apikeys.md): `@api_key` web hooks, the `keys` command,
  scopes, and per-key rate limits
- [Deploying](docs/deploy.md): the docker toggle, Compose, Swarm notes,
  and state in containers
- [Observability](docs/observability.md): liveness and readiness routes,
  request logs, and the endpoint error hook
- [Configuration](docs/configuration.md): every `config.json` key, plus
  required environment variables
- [Packages](docs/packages.md): reversible add-ons that patch the SyKit tool
  itself, installable from local folders, GitHub repos, or tarball URLs,
  with a pre-install warning report
- [Compatibility](docs/compatibility.md): the public surfaces and persistent
  formats being stabilized for 1.0

## Development

```bash
python -m pip install --require-hashes -r requirements-dev.lock
ruff check .
ruff format --check .
python -m coverage run -m unittest discover -s tests
python -m coverage report
python tests/smoke_quickstart.py
python tests/e2e_quickstart.py  # after: python -m playwright install chromium
```

## Status

Beta (`0.14.1`)

- The 1.0 compatibility candidate is frozen for the 0.14.x soak. A necessary
  correction will be released as another 0.14.x patch before 1.0.
- This is a side-project helper, not a production framework. For production
  setups there are probably far better options.

Licensed under [MIT](LICENSE).
