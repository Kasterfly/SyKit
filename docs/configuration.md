# Configuration

`init` places `sykit/config.json` inside `src/`. Every key is optional; the
defaults below apply when a key is missing.

| Key | Default | Meaning |
| --- | --- | --- |
| `endpoints` | `"/api/"` | URL prefix for all endpoints |
| `host-ip` / `host-port` | `"127.0.0.1"` / `8000` | Bind address of the built server |
| `allowed-hosts` | `127.0.0.1`, `localhost`, `::1` | Host header allowlist |
| `workers` | `1` | Server worker processes |
| `max-request-bytes` | `1048576` | Request body size cap |
| `frontend-packages` | `{}` (locked SyKit defaults) | Optional overrides for the pinned Svelte 5, Vite, and Svelte plugin versions |
| `cache-svelte` | `true` | Keep the npm cache (`__sykitcache__/`) between builds; `false` removes it after each build |
| `session-https-only` | `false` | Send the session cookie only over HTTPS; turn on in production behind TLS |
| `use-dotenv` | `false` | Load `.env` from the project root at startup (needs `python-dotenv`); build creates `.env` from `.env.example` if missing and never overwrites it |
| `sykit-folder-path` | `""` | Where the `sykit/` folder lives inside `src/` (relative path; `""` means `src/sykit`, and path "example/" means `src/example/sykit`) |
| `default-perms` | `{}` | Permissions applied to endpoints without their own `@perms` |
| `default-CORS` | `[]` | CORS origins applied to endpoints without their own `@cors` |
| `default-limits` | unlimited | Rate limits applied to endpoints without their own `@limits` ([format](endpoints.md#limits)) |

## Environment

| Variable | Meaning |
| --- | --- |
| `SYKIT_SESSION_SECRET` | **Required to run the built app.** Signs session cookies; must be at least 32 bytes of long, random data |

With `use-dotenv` enabled it can live in the project-root `.env` instead of
the shell environment (add `.env` to your project's `.gitignore`).
`build --dev` generates a temporary secret when neither provides one.

## Frontend toolchain

SyKit's default frontend manifest and lockfile live in
`files/frontend-build/`. Default builds use `npm ci` so every clean checkout
gets the same dependency tree. Setting an entry in `frontend-packages` opts
that project into a custom npm resolution and a cache-local lockfile.

Supported Node.js versions are 20.19+, 22.12+, and 24+. SyKit checks this
before installing frontend dependencies.
