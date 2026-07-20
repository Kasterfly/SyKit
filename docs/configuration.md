# Configuration

`init` places `sykit/config.json` inside `src/`. Every key is optional; the
defaults below apply when a key is missing.

| Key | Default | Meaning |
| --- | --- | --- |
| `endpoints` | `"/api/"` | URL prefix for all endpoints |
| `host-ip` / `host-port` | `"127.0.0.1"` / `8000` | Bind address of the built server |
| `allowed-hosts` | `127.0.0.1`, `localhost`, `::1` | Host header allowlist |
| `workers` | `1` | Server worker processes |
| `max-request-bytes` | `1048576` | Global request body cap, including multipart overhead ([upload limits](uploads.md#size-limits)) |
| `frontend-packages` | `{}` (locked SyKit defaults) | Optional overrides for the pinned Svelte 5, Vite, and Svelte plugin versions |
| `cache-svelte` | `true` | Keep the npm cache (`__sykitcache__/`) between builds; `false` removes it after each build |
| `docker` | `false` | Also write `Dockerfile`, `compose.yaml`, and `.dockerignore` into `built/` on every build ([details](deploy.md)) |
| `health-path` | `"/healthz"` | Liveness route that does not load sessions or query stores ([details](observability.md#health-routes)) |
| `readiness-path` | `""` (disabled) | Optional readiness route that probes the active session and API key stores ([details](observability.md#health-routes)) |
| `log-format` | `"text"` | Request log format: `"text"` or `"json"` ([details](observability.md#access-logging)) |
| `log-level` | `"INFO"` | Minimum server log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `session-https-only` | `false` | Send the session cookie only over HTTPS; turn on in production behind TLS |
| `session-max-age` | `1209600` (14 days) | Session lifetime in seconds |
| `session-store` | `""` (signed cookies) | Where session data lives: `""`, `sqlite[:path]`, or a `scheme:target` store added by a package ([details](auth.md#session-storage)) |
| `apikey-store` | `""` (project-root sqlite) | Where API keys live: `""`, `sqlite:path`, or a `scheme:target` store added by a package ([details](apikeys.md#storage)) |
| `content-security-policy` | none | Content-Security-Policy header sent with every response; an empty string disables it |
| `use-dotenv` | `false` | Load `.env` from the project root at startup (needs `python-dotenv`); build creates the file if missing, protects it on POSIX, and adds it to `.gitignore` |
| `sykit-folder-path` | `""` | Where the `sykit/` folder lives inside `src/` (relative path; `""` means `src/sykit`, and path "example/" means `src/example/sykit`) |
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
POSIX. `build --dev` generates a temporary secret when neither provides one.

## Frontend toolchain

SyKit's default frontend manifest and lockfile live in
`files/frontend-build/`. Default builds use `npm ci` so every clean checkout
gets the same dependency tree. Setting an entry in `frontend-packages` opts
that project into a custom npm resolution and a cache-local lockfile.

Supported Node.js versions are 20.19+, 22.12+, and 24+. SyKit checks this
before installing frontend dependencies.

## Reverse proxies

SyKit's built-in Uvicorn launch does not trust proxy headers. This prevents a
direct client from forging its address or request scheme. If the app is only
reachable through a trusted reverse proxy, start Uvicorn from `built/` with
proxy handling enabled and name only that proxy:

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
