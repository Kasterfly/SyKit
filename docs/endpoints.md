# Endpoints

Any Python file under `src/` can declare endpoints. `build` finds them, routes
them, and generates the `$python` client module for the frontend.

## Decorators

| Decorator | HTTP | Arguments come from | In `$python`? |
| --- | --- | --- | --- |
| `@expose(name)` | POST | JSON or multipart body | Yes |
| `@raw(name)` | GET | Query string | Yes |
| `@sse(name)` | GET | Query string | Yes, as an async iterator |
| `@web_hook(name)` | POST | JSON body | No - for external callers |

```python
from sykit.utils import expose

@expose("greet")
def greet(name, session: dict):
    session["last_greeted"] = name
    return {"message": f"Hello, {name}!"}
```

```js
import { greet } from "$python";

const result = await greet("Ada");  // {message: "Hello, Ada!"}
```

Client wrappers take the same parameters, in the same order, as the Python
function (minus the injected ones below). Failed calls throw `SyKitError`
(also exported from `$python`) with `.status` and `.details`.

## Parameters

- `session` and `request` are injected by name: `session` is a per-visitor
  dict persisted in a signed cookie, `request` is the raw Starlette request.
  The client never sends them.
- `@sse` supports `session`, but not `request` or `Upload`. Its session is a
  recursively read-only snapshot because response headers and cookies are
  committed before streamed code runs. See [Streaming](streaming.md).
- Every other parameter comes from the caller. Defaults work; unknown or
  missing arguments are rejected with an error response.
- A parameter annotated as `Upload` switches that `@expose` endpoint to a
  multipart request. The generated client accepts a browser `File` or `Blob`
  for it. Multiple named upload parameters and optional uploads are supported;
  see [Uploads](uploads.md).

## Return values

- JSON-serializable data is sent as a JSON response.
- A Starlette `Response` is sent as-is.
- Both `def` and `async def` work, sync functions run in a thread pool.
- `@sse` is the exception: it must be an async generator, and each yielded
  JSON-serializable value becomes the next client iterator value.

## Guards

Stack these on any endpoint decorator (all from `sykit.utils`):

### `@perms(...)`

```python
@expose("admin_stats")
@perms({"Session": {"role": "admin"}})
def admin_stats(): ...
```

The visitor's session must contain every listed key with exactly that value.
No session (401). Wrong value (403). `@requires(...)` is an alias.

Pages (non-API paths) are gated with the `page-perms` setting, and
`sykit.auth` turns verified credentials into these session values; see
[Login and Access](auth.md).

### `@hidden`

```python
@expose("admin_tool")
@perms({"Session": {"role": "admin"}})
@hidden
def admin_tool(): ...
```

Conceals the endpoint from anyone who fails its permission check, instead of
answering 401/403:

- The server answers with the same `404 {"error": "Endpoint not found."}` an
  unknown endpoint returns, so probing cannot tell the two apart. (Unknown
  API paths return that 404 for every HTTP method, standard or not.)
- The compiled `$python` client does not embed the endpoint's URL, method, or
  parameter names. Hidden wrappers resolve their route at runtime through an
  opaque per-build token and the reserved `__sykit_manifest__` endpoint,
  which lists only the hidden endpoints the caller's session passes
  permissions for. For everyone else the wrapper falls back to
  `hidden_api()`, which throws the same "Endpoint not found." `SyKitError`
  (status 404) a nonexistent endpoint produces, so inspecting a public
  page's bundle reveals nothing about endpoints it may not use.

Rules:

- `@hidden` takes no arguments and requires session permissions (its own
  `@perms`, or a non-empty `default-perms`).
- It cannot be combined with `@cors`; a custom CORS rule would make the
  endpoint detectable.
- The endpoint path `__sykit_manifest__` and the `$python` export name
  `hidden_api` are reserved.
- After a login that grants access, the client re-checks the manifest on the
  next hidden call automatically; no reload needed.

### `@api_key(...)`

```python
@web_hook("report")
@api_key(["reports:read"])
def report(): ...
```

Requires an API key (the `X-API-Key` header) on a `@web_hook` endpoint;
bare `@api_key` accepts any active key, a list requires those scopes.
Only for `@web_hook`, and not combinable with `@hidden`. Keys are
managed with `python SyKit keys`; see [API Keys](apikeys.md).

### `@cors(...)`

```python
@cors(["https://example.com"])
```

Per-endpoint allowed origins, overriding `default-CORS` from the config.

### `@limits(...)`

```python
@limits({"per-client": "5m", "per-session": "10s", "site-wide": 1000})
```

Request-rate caps support four scopes:

- `per-client`: one shared count for each client address seen by the ASGI
  server. Use this for login and abuse protection that must survive cookie
  resets. Clients behind one reverse proxy share a bucket unless Uvicorn is
  launched with proxy headers enabled for that trusted proxy.
- `per-session`: one count per signed session cookie. A client can reset this
  scope by clearing cookies.
- `per-key`: one count per API key, so one caller cannot exhaust
  another's budget. Requires `@api_key` on the endpoint.
- `site-wide`: one count shared by all workers and clients.
- `per-worker`: one in-memory count in each server worker.

Values are a count plus window (`10s`, `10m`, `10hr`), a bare number means per
minute, and `-1` means unlimited.

## Session helpers

From helper code that isn't itself an endpoint (only while handling a
request):

- `get_session()`: the visitor's session as a dict
- `update_session(key, value)`: set a key; passing `""` or `None` removes it

For logging visitors in and out (and hashing their passwords), use
`sykit.auth`; see [Login and Access](auth.md).

Unhandled endpoint exceptions return a generic 500 response. Apps and
packages can observe the original exception through SyKit's error hook;
see [Observability](observability.md#error-hook).
