# SyKit 0.5.0 - Login and Access Update

SyKit 0.5.0 adds a first-party login flow, permission-gated pages, and a
pluggable session store with server-side revocation. Full guide:
`docs/auth.md`.

## Added

- **`sykit.auth`:** scrypt password helpers (`hash_password`,
  `verify_password`, standard library only) plus `login(claims)` and
  `logout()`. Claims become exactly the session values `@perms` checks.
  `login()` replaces the whole session and rotates the session id when a
  session store is configured; `logout()` is a real server-side
  revocation with a store, not just a cleared cookie.
- **`page-perms` setting:** gate page path prefixes with the same
  permissions format `@perms` uses. A failing request receives exactly
  what a nonexistent page returns (the SPA fallback), so probing cannot
  tell a protected page from a missing one - the same idea `@hidden`
  applies to endpoints. Matching is case-insensitive and runs on the
  resolved file path.
- **`session-store` setting:** `""` keeps signed-cookie sessions,
  `"sqlite[:path]"` stores sessions server-side in sqlite (lifts the
  4 KB cookie ceiling, makes logout revoke every cookie copy, slides
  expiry per request), and `"scheme:target"` loads a store added by a
  package as `files/core/_store_<scheme>.py` with a `create(target)`
  function. If the store is unreachable the server answers 503 instead
  of silently degrading.

## Changed

- Session handling moved from Starlette's `SessionMiddleware` into
  SyKit's own middleware in `files/core/_sessions.py`. The signed-cookie
  format is unchanged, so existing session cookies stay valid.
- The build now stages every `files/core/*.py` module instead of a fixed
  list, so packages can ship extra core modules (session stores, for
  example) without editing `build.py`.
- Session cookies larger than 4000 bytes log a warning that points at
  the `session-store` setting.

## Upgrade notes

- Existing apps keep working without config changes; both new settings
  default to off. Rebuild deployed applications so the new server code
  and `sykit/auth.py` land in `built/`.
- Existing projects should re-run `python SyKit init` so `sykit/auth.py`
  is copied into `src/`, then rebuild.
- Signed-cookie logout still cannot invalidate stolen or replayed
  cookies; that limitation is inherent to cookie sessions and is why the
  session store exists. Production apps with real logins should set
  `"session-store": "sqlite"` (single worker or shared disk) or a
  store package.
