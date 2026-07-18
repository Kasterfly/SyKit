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

- Python 3.10+
- Node.js 20.19+, 22.12+, or 24+ on your PATH
- npm (included with standard Node.js installations)

Frontend build dependencies are pinned and installed from SyKit's lockfile on
the first build.

## Quick start

Clone SyKit into your project directory. It lives alongside your code as a
tool folder, not as an installed library:

```
cd your-project
git clone https://github.com/Kasterfly/SyKit
python -m pip install -r SyKit/requirements.txt

python SyKit init  # creates src/ with a minimal starter app
python SyKit build # generates endpoints and compiles to built/
```

Then set a session secret and run the built app:

```bash
export SYKIT_SESSION_SECRET="a-long-random-string-of-at-least-32-bytes"   # PowerShell: $env:SYKIT_SESSION_SECRET = "..."
python built/main.py
```
The app serves on `http://127.0.0.1:8000` by default.

## Commands

| Command | What it does |
| --- | --- |
| `python SyKit init` | Create `src/sykit` configuration and a minimal starter app |
| `python SyKit build [--dev]` | Detect endpoints, generate the `$python` client, compile into `built/`; `--dev` also runs the app |
| `python SyKit package <add\|remove\|list\|diff>` | Manage packages that extend SyKit; install from local folders, GitHub, or the official packages repo |
| `python SyKit version` | Show the SyKit version |
| `python SyKit help` | Show usage |
Commands operate on the current working directory (your project root).

## Docs

- [Endpoints](docs/endpoints.md): `@expose`, `@raw`, `@web_hook`, sessions,
  permissions, CORS, rate limits
- [Configuration](docs/configuration.md): every `config.json` key, plus
  required environment variables
- [Packages](docs/packages.md): reversible add-ons that patch the SyKit tool
  itself, installable from local folders, GitHub repos, or tarball URLs,
  with a pre-install warning report

## Development

```bash
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
python tests/smoke_quickstart.py
```

## Status

Beta (`0.4.0`)

- Expect breaking changes before 1.0.
- This is a side-project helper, not a production framework. For production
  setups there are probably far better options.

Licensed under [MIT](LICENSE).
