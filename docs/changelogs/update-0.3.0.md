# SyKit 0.3.0 - Security Hardening Update

SyKit 0.3.0 applies the July 2026 security audit fixes and adds controls for
client-based rate limiting, session lifetime, and Content-Security-Policy.

## Added

- **Per-client rate limits:** `per-client` is supported by
  `@limits(...)` and `"default-limits"`. The scope is keyed by the
  client address seen by the ASGI server, so clearing a session cookie does
  not reset it.
- **Content-Security-Policy setting:** set
  `"content-security-policy"` to emit that header on every response. It
  defaults to an empty string, which leaves CSP disabled.
- **Session lifetime setting:** `"session-max-age"` controls the session
  cookie lifetime in seconds and defaults to `1209600`, or 14 days.
- **Security regression coverage:** tests cover endpoint concealment,
  client-based limits, proxy-header defaults, session and CSP behavior,
  dotenv safety, package metadata, and release versioning.

## Security changes

- Hidden endpoints and unknown API paths now return the same JSON 404 for all
  HTTP methods, including `OPTIONS` and `TRACE`. Method probing no
  longer exposes an `Allow` header for hidden routes.
- The built-in Uvicorn launch disables proxy-header trust. A direct client
  cannot use `X-Forwarded-For` or `X-Forwarded-Proto` to replace
  its address or scheme.
- Package manifest `name`, `desc`, and `credit` fields reject
  terminal control characters before they reach `package list` or
  `package diff` output.
- `dotenv` is reserved as a top-level project module name, preventing
  project code from shadowing python-dotenv during startup.
- When dotenv support creates `.env`, it uses owner-only permissions on
  POSIX and ensures `.env` is listed in the project `.gitignore`.

## Upgrade notes

- Rebuild deployed applications after updating SyKit so the hardened server
  and rate limiter are copied into `built/`.
- Existing 0.2.0 configuration remains valid. The default session lifetime
  is unchanged, and CSP stays disabled until a policy is configured.
- With default proxy handling, applications behind one reverse proxy see one
  shared `per-client` identity. To preserve real client addresses, launch
  Uvicorn with proxy headers enabled and restrict
  `--forwarded-allow-ips` to the trusted proxy.
- Session cookies are signed rather than encrypted. Do not store secrets in
  a session.
