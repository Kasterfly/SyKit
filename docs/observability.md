# Observability

SyKit exposes health routes, one access log per request, and a callback for
unhandled endpoint exceptions. These features use only the standard library
and are enabled in every built app.

## Health routes

`GET /healthz` is the default liveness check. It answers:

```json
{"status":"ok"}
```

The liveness response bypasses session loading, API key stores, request body
handling, and endpoint dispatch. It still passes the Host policy, security
headers, and access logger. This makes it safe for a process monitor to use
even when an external store is offline. `HEAD` is also supported.

Change the path with `health-path`. The path must be outside the configured
endpoint prefix. A docker build puts this exact path in the generated Compose
healthcheck.

Readiness is disabled by default. Set a separate path to turn it on:

```json
{
  "health-path": "/healthz",
  "readiness-path": "/readyz"
}
```

The readiness route makes a read-only probe against the configured
server-side session store and the API key store when the app has keyed
endpoints. Cookie-only sessions need no probe. A healthy response is 200:

```json
{
  "status": "ready",
  "checks": {"sessions": "ok", "api_keys": "ok"}
}
```

If a store is unavailable, readiness returns 503, marks that check
`unavailable`, and logs the exception without exposing connection details in
the response. The first readiness probe can create the normal sqlite database
file and schema. Liveness never does.

## Access logging

Every HTTP response produces one `INFO` request record with these fields:

- `method`: HTTP method.
- `path`: URL path only. The query string is not logged.
- `status`: response status code.
- `duration_ms`: elapsed request time in milliseconds.
- `caller`: `anonymous`, `session`, or `api_key:<fingerprint>`.

API key fingerprints are the first 12 hexadecimal characters of a SHA-256
hash. The key itself is never logged. A non-empty loaded session is recorded
only as `session`; session values and cookie contents are never logged.

The default `log-format` is `text`:

```text
request method=GET path="/healthz" status=200 duration_ms=0.412 caller=anonymous
```

Set `"log-format": "json"` for one compact JSON object per line:

```json
{"caller":"anonymous","duration_ms":0.412,"event":"request","method":"GET","path":"/healthz","status":200}
```

`log-level` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
Levels above `INFO` suppress access records while retaining server messages at
or above the selected level. SyKit disables Uvicorn's separate access logger
to avoid duplicate request lines.

## Error hook

Register one callback to report exceptions that escape an endpoint:

```python
from sykit import register_error_hook


def report_error(error, request):
    error_reporter.capture(error, path=request.url.path)


register_error_hook(report_error)
```

The callback receives `(error, request)` before SyKit sends its generic
`500 {"error": "The endpoint failed."}` response. It may be a regular function
or `async def`. Registering another callback replaces the current one, and
`register_error_hook(None)` clears it.

The hook must not send its own response. If it raises, SyKit logs that failure
and still returns the original generic 500 response. The request object can
contain sensitive headers and session data, so reporting integrations should
scrub them before attaching request context.

Provider packages can stay additive: add a `sykit/` module that registers the
callback when imported, then import that module from application startup or an
endpoint module. Re-run `python SyKit init` after installing such a package so
its module is copied into the project's `src/sykit/` folder.
