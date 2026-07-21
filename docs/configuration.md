# Configuration

`init` places `sykit/config.json` inside `src/`. Every listed key is optional;
the defaults below apply when a key is missing. Unknown top-level keys stop the
build instead of silently selecting a default.

| Key | Default | Meaning |
| --- | --- | --- |
| `endpoints` | `"/api/"` | URL prefix for all endpoints |
| `host-ip` / `host-port` | `"127.0.0.1"` / `8000` | Bind address of the built server |
| `allowed-hosts` | `127.0.0.1`, `localhost`, `::1` | Host header allowlist |
| `workers` | `1` | Server worker processes |
| `task-concurrency` | `1` | Background calls that may run concurrently in each server process ([details](background-tasks.md#enqueue-work)) |
| `sse-heartbeat-seconds` | `15` | Seconds between SSE keepalive comments while an event is pending; positive integer ([details](streaming.md#lifecycle-and-errors)) |
| `max-request-bytes` | `1048576` | Global request body cap, including multipart overhead ([upload limits](uploads.md#size-limits)) |
| `frontend-packages` | `{}` (locked SyKit defaults) | Optional overrides for the pinned Svelte 5, Vite, and Svelte plugin versions |
| `cache-svelte` | `true` | Keep the npm cache (`__sykitcache__/`) between builds; `false` removes it after each build |
| `docker` | `false` | Also write `Dockerfile`, `compose.yaml`, and `.dockerignore` into `built/` on every build ([details](deploy.md)) |
| `trust-proxy` | `false` | Trust `X-Forwarded-*` headers from loopback proxies (or `$FORWARDED_ALLOW_IPS`); enable only behind a trusted reverse proxy ([details](#reverse-proxies)) |
| `health-path` | `"/healthz"` | Liveness route that does not load sessions or query stores ([details](observability.md#health-routes)) |
| `readiness-path` | `""` (disabled) | Optional readiness route that probes the active session and API key stores ([details](observability.md#health-routes)) |
| `log-format` | `"text"` | Request log format: `"text"` or `"json"` ([details](observability.md#access-logging)) |
| `log-level` | `"INFO"` | Minimum server log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `session-https-only` | `false` | Send the session cookie only over HTTPS; turn on in production behind TLS (the server warns at startup when it is off but `allowed-hosts` is not loopback-only) |
| `session-max-age` | `1209600` (14 days) | Session lifetime in seconds |
| `session-store` | `""` (signed cookies) | Where session data lives: `""`, `sqlite[:path]`, or a `scheme:target` store added by a package ([details](auth.md#session-storage)) |
| `apikey-store` | `""` (project-root sqlite) | Where API keys live: `""`, `sqlite:path`, or a `scheme:target` store added by a package ([details](apikeys.md#storage)) |
| `task-store` | `""` (project-root sqlite) | Where background calls live: `""`, `sqlite:path`, or a `scheme:target` store added by a package ([details](background-tasks.md#task-stores)) |
| `content-security-policy` | none | Content-Security-Policy header sent with every response; an empty string disables it. `default-src 'self'` is a good starting point for most apps |
| `use-dotenv` | `false` | Load `.env` from the project root at startup (needs `python-dotenv`); build creates the file if missing, protects it on POSIX, and adds it to `.gitignore` |
| `sykit-folder-path` | `""` | Where the `sykit/` folder lives inside `src/` (relative path; `""` means `src/sykit`, and path "example/" means `src/example/sykit`) |
| `extensions` | `{}` | Reserved object for package-specific configuration; each package should own one child key |
| `default-perms` | `{}` | Permissions applied to endpoints without their own `@perms` |
| `page-perms` | `{}` | Page path prefixes gated by session permissions; denied requests get the same response as a nonexistent page ([details](auth.md#permission-gated-pages)) |
| `default-CORS` | `[]` | CORS origins applied to endpoints without their own `@cors` |
| `default-limits` | unlimited | Rate limits applied to endpoints without their own `@limits` ([format](endpoints.md#limits)) |

## Environment

| Variable | Meaning |
| --- | --- |
| `SYKIT_SESSION_SECRET` | **Required to run the built app.** Signs session cookies; must be at least 32 bytes of long, random data |

With `use-dotenv` enabled it can live in the project-root `.env` instead of
the shell environment. Build creates `.env` from `.env.example` when needed,
adds `.env` to the project `.gitignore`, and uses owner-only permissions on
POSIX. On Windows the file keeps the directory's ACL instead, so review who
can read it on shared machines. `build --dev` generates a temporary secret
when neither provides one.

## Frontend toolchain

SyKit's default frontend manifest and lockfile live in
`files/frontend-build/`. Default builds use `npm ci` so every clean checkout
gets the same dependency tree. Setting an entry in `frontend-packages` opts
that project into a custom npm resolution and a cache-local lockfile.

Supported Node.js versions are LTS 22.12+ and 24.x. SyKit rejects end-of-life,
odd, and untested future Node lines before installing frontend dependencies.
CI tests Node 22.12 and 24 with every documented Python minor from 3.11 through
3.14.

## Backend dependencies

`requirements.in` holds supported runtime ranges. `requirements.lock` pins the
resolved runtime tree with hashes and is the install source for generated apps,
Docker, and runtime-only CI jobs. `requirements-dev.in` and
`requirements-dev.lock` do the same for unit tests, coverage, browser E2E,
lint, and other development tools, including the HTTP transport used only by
Starlette's test client. Regenerate locks with pip-tools after changing an
input; do not hand-edit the generated files.

## Reverse proxies

SyKit's built-in Uvicorn launch does not trust proxy headers by default.
This prevents a direct client from forging its address or request scheme.
Two features depend on those values being correct:

- `per-client` rate limits use the client address; without proxy headers
  every visitor shares the proxy's bucket.
- The same-origin check compares each `Origin` against the request scheme
  and `Host`. Behind a TLS-terminating proxy the app sees `http` while
  browsers send `Origin: https://...`, so same-origin calls are rejected with
  `403 {"error":"Origin is not allowed."}` until the scheme is forwarded.

If the app is only reachable through a trusted reverse proxy, set
`"trust-proxy": true` in `config.json` and rebuild. The built-in launch then
honors `X-Forwarded-*` headers from loopback proxies only (or from the
addresses in the `FORWARDED_ALLOW_IPS` environment variable). Terminate TLS
at the proxy so only that proxy can reach the app; a direct client must
never be able to set these headers itself.

Alternatively, start Uvicorn from `built/` with proxy handling enabled and
name only that proxy:

```bash
python -m uvicorn server:app --proxy-headers --forwarded-allow-ips="127.0.0.1"
```

Replace `127.0.0.1` with the actual proxy address. Do not use a wildcard when
untrusted clients can reach the application listener.

## SyKit tool settings

The package commands read two optional keys from the SyKit tool's own
`sykit/config.json` (the template that sits next to the SyKit source, not a
project's copy):

| Key | Default | Meaning |
| --- | --- | --- |
| `package-default-repo` | `"Kasterfly/SyKit-Packages"` | GitHub repo used to resolve bare package names in `package add <name>` |
| `package-max-download-mb` | `50` | Size cap for remote package downloads and their extracted content |
| `update-repo` | `"Kasterfly/SyKit"` | GitHub repo `python SyKit update` fetches SyKit releases from |

There is deliberately no setting that disables the pre-install analysis, the
confirmation prompt, or the critical-finding gate described in
[Packages](packages.md).
