# Endpoints

Any Python file under `src/` can declare endpoints. `build` finds them, routes
them, and generates the `$python` client module for the frontend.

## Decorators

| Decorator | HTTP | Arguments come from | In `$python`? |
| --- | --- | --- | --- |
| `@expose(name)` | POST | JSON body | Yes |
| `@raw(name)` | GET | Query string | Yes |
| `@web_hook(name)` | POST | JSON body | No — for external callers |

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
- Every other parameter comes from the caller. Defaults work; unknown or
  missing arguments are rejected with an error response.

## Return values

- JSON-serializable data is sent as a JSON response.
- A Starlette `Response` is sent as-is.
- Both `def` and `async def` work, sync functions run in a thread pool.

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

### `@cors(...)`

```python
@cors(["https://example.com"])
```

Per-endpoint allowed origins, overriding `default-CORS` from the config.

### `@limits(...)`

```python
@limits({"per-session": "10s", "site-wide": 1000})
```

Request-rate caps. Keys: `per-session`, `site-wide`, `per-worker`. Values are
a count plus window (`10s`, `10m`, `10hr`) a bare number means per minute,
and `-1` means unlimited.

## Session helpers

From helper code that isn't itself an endpoint (only while handling a
request):

- `get_session()`: the visitor's session as a dict
- `update_session(key, value)`: set a key; passing `""` or `None` removes it
