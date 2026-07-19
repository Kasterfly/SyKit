# Login and Access

SyKit 0.5.0 ships three pieces that together make a real login flow:
password helpers and `login`/`logout` in `sykit.auth`, permission-gated
pages through the `page-perms` setting, and a pluggable session store
behind the `session-store` setting.

## Passwords

`sykit.auth` hashes passwords with scrypt (standard library, no new
dependencies). Where user records live is the app's concern; a database
package such as `db-base` pairs well.

```python
from sykit import auth

stored = auth.hash_password("correct horse battery staple")
auth.verify_password("correct horse battery staple", stored)  # True
```

- `hash_password(password)` returns a self-describing
  `scrypt$N$r$p$salt$key` string to store with the user record.
- `verify_password(password, stored)` returns `False` for a wrong, empty,
  or non-string password. A malformed `stored` value raises `AuthError`:
  that is corrupt data, not a failed login attempt.

## Logging in and out

`auth.login(claims)` replaces the visitor's session with the given dict
after you have verified credentials. The claims become exactly the values
`@perms` checks. `auth.logout()` clears the session.

```python
from sykit import auth
from sykit.utils import expose, limits, perms

@expose("login")
@limits({"per-client": "10m"})
def login(username: str, password: str):
    user = find_user(username)
    if user is None or not auth.verify_password(password, user["hash"]):
        return {"ok": False}
    auth.login({"user": username, "role": user["role"]})
    return {"ok": True}

@expose("logout")
def logout():
    auth.logout()
    return {"ok": True}

@expose("admin_stats")
@perms({"Session": {"role": "admin"}})
def admin_stats(): ...
```

Notes:

- Always rate limit login endpoints with `per-client`; it is the scope
  that survives cookie resets.
- `login()` drops everything already in the session before applying the
  claims, and rotates the session id when a server-side session store is
  configured, so a login can never be fixated onto an id the client
  presented earlier.
- With the default signed-cookie sessions, `logout()` clears the cookie
  but cannot invalidate copies of it; a captured cookie stays valid until
  it expires. Configure a session store to make logout a real server-side
  revocation.

## Permission-gated pages

The `page-perms` setting maps page path prefixes to the same permissions
format `@perms` uses:

```json
"page-perms": {
    "/admin": {"Session": {"role": "admin"}}
}
```

A request under a listed prefix whose session fails the check receives
exactly what a nonexistent page returns: the SPA fallback (`index.html`).
Probing cannot tell a protected page from a page that was never there,
the same idea `@hidden` endpoints apply to the API.

- Protect real files by placing them under the prefix, for example
  `src/public/admin/report.pdf` ends up at `/admin/report.pdf` in the
  built app and is only served to sessions that pass.
- Matching is case-insensitive and runs on the resolved file path, so
  case or short-name aliases cannot slip past the prefix.
- The SPA shell (`index.html`) itself is always served; gate content, not
  the shell. Anything compiled into the public JavaScript bundle is
  visible to everyone regardless of routing.
- The site root `"/"` cannot be listed, prefixes under the `endpoints`
  prefix belong to `@perms`, and every listed prefix needs a non-empty
  `"Session"` object; the server refuses to start otherwise.

## Session storage

By default sessions live in a signed cookie (unchanged from earlier
versions; existing cookies stay valid). Cookies cap session state at
about 4 KB and cannot be revoked server-side. The `session-store` setting
moves the data server-side; the cookie then only carries a signed random
session id:

| `session-store` | Behavior |
| --- | --- |
| `""` (default) | Signed-cookie sessions |
| `"sqlite"` | Built-in sqlite store in `.sykit-sessions.sqlite3` next to the built app |
| `"sqlite:path"` | The same, at a custom path (relative paths resolve from `built/`) |
| `"scheme:target"` | A store registered by a package (see below) |

With a store configured:

- `logout()` deletes the server-side session, revoking every copy of the
  cookie at once.
- `login()` issues a fresh session id.
- Session data is no longer size-capped by the cookie.
- Expiry slides: each request pushes it `session-max-age` seconds ahead.
- If the store is unreachable the server answers
  `503 {"error": "Sessions are temporarily unavailable."}` rather than
  silently degrading.

## Writing a session store package

A store package adds one file, `files/core/_store_<scheme>.py`, and needs
no edits to SyKit: the build stages every `files/core/*.py` module, and
`session-store: "<scheme>:<target>"` imports `core._store_<scheme>` and
calls its `create(target)`.

```python
# files/core/_store_postgres.py (shipped by a package)
def create(target):
    return PostgresSessionStore(target)
```

The returned object implements the `SessionStore` interface from
`core/_sessions.py`:

- `load(session_id)`: return the stored dict, or `None` when missing or
  expired.
- `save(session_id, data, max_age)`: store the dict and (re)set its
  expiry `max_age` seconds ahead.
- `touch(session_id, max_age)`: push an existing session's expiry ahead.
- `delete(session_id)`: remove the session; missing ids are not an error.

Methods are called from a thread pool, so blocking clients are fine; they
must tolerate concurrent calls from multiple workers. Session ids are
opaque random strings; stores never see cookie signatures or secrets.
