# SyKit 0.9.0 - Observability Update

SyKit 0.9.0 makes built apps easier to monitor and debug without requiring an
external service.

## Added

- A configurable liveness route at `/healthz` by default. It bypasses session
  loading and store access, while retaining host checks, security headers, and
  access logging.
- An optional `readiness-path`. When enabled, it probes active server-side
  session and API key stores and returns 503 if either is unavailable.
- One request log for every HTTP response with method, path, status, elapsed
  milliseconds, and a privacy-safe caller identity. Logs support text and JSON
  formats through `log-format`; `log-level` controls the threshold.
- `register_error_hook(callback)`, which receives an unhandled endpoint
  exception and its request before the generic 500 response is sent. Sync and
  async callbacks are supported, and hook failures cannot replace the response.
- A generated Compose healthcheck that uses the configured liveness path.
- [Observability documentation](../observability.md) covering health response
  behavior, log fields, privacy guarantees, and error-hook registration.

## Configuration

Four keys are new:

| Key | Default |
| --- | --- |
| `health-path` | `"/healthz"` |
| `readiness-path` | `""` (disabled) |
| `log-format` | `"text"` |
| `log-level` | `"INFO"` |

Health paths must be distinct, valid URL paths outside the endpoint prefix.
Existing configurations keep working because every key has a default.

## Upgrade note

After updating the SyKit tool, run `python SyKit init` in existing projects so
`src/sykit` receives the new error-hook module and top-level export. Then
rebuild the app.
