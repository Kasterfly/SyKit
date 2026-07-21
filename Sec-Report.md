# SyKit Security Report

> **Status:** the built-site findings were addressed in 0.12.2. The package,
> remote-download, and self-update subsystems were reviewed for 0.13.0, and
> the fail-open updater finding was fixed. The pre-1.0 runtime and CLI review
> was completed for 0.14.2 and its actionable findings were patched. SyKit
> 1.0.0 declares that reviewed runtime stable without behavior changes.

- **Project:** SyKit 1.0.0 (stable)
- **Scope:** `build.py`, the generated runtime under `files/`, the public
  `sykit/` package, `package.py`, `package_remote.py`, `package_analysis.py`,
  and `update.py`.
- **Method:** manual data-flow and trust-boundary review, adversarial unit
  tests, a built-app browser flow through a reverse proxy, and a generated
  container start using locked dependencies.
- **Result:** no critical or high issue was found in the 0.14.2 assessment.
  Three medium and eight low runtime and build findings were addressed before
  the behavior-identical 1.0.0 declaration. The package system's inherent trust
  model was excluded from this assessment and remains documented below for
  maintainer review.

---

## 0.14.2 runtime and build review

The review traced anonymous session writes, background task recovery, tool
resolution, request header handling, protected static files, password hash
validation, and generated-file exclusions.

Implemented controls:

- Requests without a valid session cookie whose session contains only internal
  SyKit values are not persisted. The runtime warns about anonymous
  `per-session` limits in cookie and store modes, and login preserves the
  current rate identity.
- Recovered background calls stop after `task-max-attempts` claims, defaulting
  to three. Queue payloads are limited to 1 MiB, and the built-in sqlite store
  removes failed rows after seven days.
- Windows Node.js and npm lookup searches explicit PATH directories while
  skipping the current directory, including equivalent relative entries.
- Duplicate `X-API-Key` headers are rejected with 400 before lookup. Access
  logging fingerprints a key only when exactly one header is present.
- Permission-protected static assets use `no-cache` instead of the immutable
  public asset policy.
- Stored scrypt hashes are rejected when their composite memory request or
  derived-key size exceeds the verification bounds. Backend `ValueError`
  failures are normalized to `AuthError`.
- Successful builds add `built/` and `__sykitcache__/` to `.gitignore` when
  missing. Generated `.dockerignore` files also exclude `.env` and the API key
  sqlite database.

Residual operational limits:

- SyKit cannot safely terminate arbitrary Python task code. A hung call keeps
  renewing its lease, occupies a concurrency slot, and delays graceful
  shutdown. Task code must use bounded I/O and cooperative cancellation; the
  behavior and forced-termination recovery are documented.
- Installing a package remains equivalent to running trusted SyKit-level code.
  That package-system trust model was outside the 0.14.2 assessment scope.

## 0.13.0 package and updater review

The review traced every remote source from parsing through resolution,
download, extraction, analysis, apply, provenance recording, removal, update,
and rollback.

Verified controls:

- Remote transports and redirect hops require HTTPS. Credential-bearing URLs,
  unsafe archive members, case collisions, links, devices, excessive entry
  counts, and configured size-limit violations are rejected.
- Package analysis and apply use the same stored source snapshot and verify its
  content hash immediately before applying.
- Package writes are planned before mutation, use backups, and roll back on a
  failed operation. Protected roots cannot be package targets.
- Self-update preserves `.git` and `.packages`, snapshots installed packages,
  restores the old core on swap failure, and reports packages that cannot be
  reapplied.
- Self-update now requires a full commit SHA. GitHub API failures abort before
  archive download, `--yes` does not bypass the check, and branch updates need
  `--allow-unreleased`.
- A caller may provide a full 40-character SHA without GitHub API access. That
  is already an immutable identifier and is downloaded by that exact value.

Residual trust model:

- Installing a package still grants it SyKit-level code execution. Static
  analysis is review assistance, not a sandbox or signature verifier.
- Package installs, unlike self-updates, may intentionally use an unpinned
  moving ref or an HTTPS tarball after a visible warning and confirmation.
  Official package releases should use protected tags.
- GitHub tag protection, tag signing, and immutable-release enforcement are
  repository controls outside this source tree. The release checklist requires
  them before 1.0.

## Historical built-site findings resolved in 0.12.2

The following descriptions preserve the original pre-fix evidence. They are
not open findings and their recommendations were implemented in 0.12.2.

### 1. Resolved medium - Hidden endpoints were enumerable with non-standard methods

**Where:** `files/server.py:286-297` (`API_CATCHALL_METHODS`), routes built at `files/server.py:884-905`.

The hidden-endpoint design promises that a protected endpoint "answers with the same `404 {"error": "Endpoint not found."}` an unknown endpoint returns... for every HTTP method" (`docs/endpoints.md`). The API catch-all route enforces this only for the eight methods in `API_CATCHALL_METHODS` (`GET, HEAD, POST, PUT, DELETE, PATCH, OPTIONS, TRACE`).

Uvicorn/h11 accepts any valid HTTP method token (`CONNECT`, `QUERY`, `PROPFIND`, even `FOO`). Those miss the catch-all, fall through to Starlette's partial-match handling, and produce a 405 whose `Allow` header comes from the *matching route* - which differs between a real endpoint and a nonexistent path.

**Probe evidence** (no session, anonymous):

```
CONNECT /api/admin_tool      -> 405  Allow: POST
CONNECT /api/does_not_exist  -> 405  Allow: POST, TRACE, GET, DELETE, OPTIONS, PUT, HEAD, PATCH
```

Same result for `FOO` and `QUERY`. So an attacker who guesses a hidden endpoint's path can confirm it exists and learn its method, with no credentials. Standard methods were verified indistinguishable (all 404, identical bodies) - the gap is only the unlisted methods.

**Impact:** existence/method disclosure on a feature whose entire purpose is concealment. The attacker still cannot *invoke* the endpoint without passing its `@perms` check, so this is information disclosure, not access.

**Recommendation:** stop relying on the method-filtered catch-all. Either

- register the API catch-all without a method restriction and 404 everything under the API prefix that no endpoint route fully matched, or
- add middleware in front of the router that rewrites any 405 response under `API_PREFIX` to the same 404 body as unknown paths (this also harmonizes visible endpoints, which is harmless), or
- normalize/reject unknown methods at the `HostPolicyMiddleware` layer with the standard 404.

Add a probe test mirroring `test_security_hardening.py`'s hidden-endpoint loop but with an unlisted method (e.g. `QUERY`) to lock this in.

---

### 2. Resolved low-medium - Proxy scheme handling rejected same-origin POSTs

**Where:** `files/server.py:990-1002` (`_same_origin`), `files/server.py:1494` (`proxy_headers=False`), docs gap at `docs/configuration.md` ("Reverse proxies").

The origin check compares the request's `Origin` tuple `(scheme, host, port)` against the *direct connection's* scheme plus the `Host` header. `run()` hardcodes `proxy_headers=False`, so behind a normal HTTPS-terminating reverse proxy the app sees scheme `http` while every browser POST (the generated `$python` client uses `fetch` POST, and browsers always send `Origin` on POST) arrives with `Origin: https://...`. The tuple never matches and the request is rejected with `403 {"error":"Origin is not allowed."}` - for legitimate same-site users.

**Probe evidence:** an origin tuple that differs from the connection's scheme/port is 403'd even when the host matches (`_same_origin` does exact `(scheme, hostname, port)` comparison).

**Impact:** availability break that pressures operators into "fixes" with real security downsides - adding broad `default-CORS` entries, or loosening `allowed-hosts` - when the actual fix is proxy configuration. Fail-closed, so no direct bypass, but it trains bad workarounds.

**Recommendation:**

- Document in `docs/configuration.md` (Reverse proxies) that the origin check depends on the connection scheme, and that running behind TLS termination *requires* either the documented `uvicorn --proxy-headers --forwarded-allow-ips=...` launch (which also fixes `scope["scheme"]`) or listing the public origin in `default-CORS`.
- Consider a config flag like `"trust-proxy": true` that flips `proxy_headers` in `run()` so users don't have to abandon `python main.py` to get correct scheme handling.

---

### 3. Resolved low - Failed authentication bypassed configured rate limiting

**Where:** `files/server.py:688-702` - `_check_permissions` and `_check_api_key` both run and return before `LIMITER.check`.

Requests that fail authentication never consume any rate-limit budget:

```
POST /api/hook  (per-client limit: 2/3600s, wrong X-API-Key)  -> 401, 401, 401, 401, 401
POST /api/hook  (valid key)                                   -> 200, 200, 429, 429
```

So key-guessing and permission-probing traffic is unthrottled at the application layer, and each failed API-key attempt still costs a sqlite lookup. The key space (~256-bit secret) makes brute force infeasible, and `@perms` endpoints on the browser side are expected to be protected by rate-limiting the *login* endpoint instead (the docs correctly advise `per-client` there). The residual risk is DoS noise and log/db pressure on `@web_hook` endpoints, plus silent probing.

**Recommendation:** apply `per-client`/`site-wide` checks (when configured on the endpoint) *before* key validation as well, or add a small fixed per-client throttle specifically on authentication failures. At minimum, note the ordering in `docs/apikeys.md` so operators put a proxy-level limit in front of web hooks.

---

### 4. Low - Session cookie defaults are development-friendly, not production-safe

**Where:** `files/core/_sessions.py:237-239`, `sykit/config.json:17`, `files/server.py:1400-1403`.

Verified `Set-Cookie` on login (default config):

```
sykit_session=...; Path=/; Max-Age=1209600; HttpOnly; SameSite=lax
```

- `session-https-only` defaults to `false`, so the cookie has no `Secure` flag unless the operator opts in. Docs do say to enable it in production, but nothing warns at build/start time when it's off while `allowed-hosts` contains a public name.
- No `__Host-` name prefix, which would add resistance to cookie injection from sibling subdomains (the `__Host-` rules also require `Secure` and `Path=/`, both already satisfiable).
- Cookie-mode (default) sessions are signed but not encrypted - claims are base64-readable by the client - and `logout()` cannot revoke a captured cookie until `session-max-age` (14 days default) passes. Both are documented trade-offs; they're restated here because the defaults are what most users will ship.

**Recommendation:** keep the defaults for DX, but emit a startup warning when `session-https-only` is false and the host/allowed-hosts look non-local; consider switching the cookie name to `__Host-sykit_session` when `session-https-only` is on; recommend `session-store: "sqlite"` in the deployment docs for real logout revocation.

---

### 5. Low - `per-session` rate limits are evadable in cookie mode

**Where:** `files/core/_limits.py:50-56`.

The per-session bucket identity is a `__sykit_rate_id` value stored *in the session*. With signed-cookie sessions a client just deletes (or never sends) the cookie and gets a fresh identity every request. The auth docs hint at this ("`per-client` is the scope that survives cookie resets"), but `per-session` is still presented as a general-purpose scope and is the first entry in the generated `default-limits` block.

**Recommendation:** document the caveat directly in `docs/endpoints.md` (Limits) - `per-session` only has teeth with a server-side `session-store` - and consider a startup warning when `per-session` limits are configured alongside cookie-mode sessions.

---

### 6. Low - Readiness route discloses backend topology

**Where:** `files/server.py:1132-1169`.

When `readiness-path` is enabled, the response body enumerates which stores exist and their health (`{"sessions": "ok", "api_keys": "ok", "tasks": "ok"}`) to any unauthenticated client. Liveness (`/healthz`) is fine. Readiness details are useful for orchestration but reveal architecture that aids targeted attacks (e.g., which subsystems to DoS).

**Recommendation:** keep readiness disabled by default (it is), and document that when enabled it should be restricted at the proxy/network layer - or gate the detailed `checks` map behind an optional shared secret header.

---

### 7. Low - No security-event audit logging

**Where:** `files/server.py` dispatch path generally.

The access log records method/path/status/caller, but security-relevant failures are not logged as events: failed API-key attempts (401 at `files/server.py:663,674`), permission denials (401/403 at `files/server.py:517-522`), origin rejections (403 at `files/server.py:1346-1349`), and rate-limit hits (429 at `files/server.py:703-708`). Failed keys fail silently - not even the key fingerprint is logged - so brute-force or probing campaigns are invisible unless someone correlates status codes in the access log.

**Recommendation:** log a warning line (with the existing API-key fingerprint helper where applicable) on authentication failures and origin rejections, and an info line on 429s. This is cheap and makes the existing JSON log format much more useful for detection.

---

### 8. Informational - No default CSP / transport hardening headers

**Where:** `files/server.py:1280-1299`.

`X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, and `Referrer-Policy` are set on every response (verified). `Content-Security-Policy` ships empty (`sykit/config.json:22`) and there is no HSTS. For an SPA serving JSON this is mostly defense-in-depth, but a conservative default such as `default-src 'self'` (verified compatible in your own hardening test) would be a better shipped default, with the config key to relax it. HSTS is best left to the TLS terminator - just document it.

---

### 9. Informational - API keys have no expiry or rotation story

**Where:** `files/core/_apikeys.py`.

Key records carry `created` and `revoked` only. A leaked key is valid until someone notices and runs `keys revoke`. Consider optional `expires` on creation, and document a rotation cadence in `docs/apikeys.md`. The hashed-at-rest storage and fingerprint-only logging are good as-is.

---

### 10. Informational - `.env` file permissions are POSIX-only

**Where:** `build.py:1907-1908`.

`os.chmod(ENV_PATH, 0o600)` runs only on POSIX; on Windows the generated `.env` inherits the directory ACL. On a personal machine that's typically fine, but on shared Windows hosts the session secret may be readable by other users. Worth a note in `docs/configuration.md` (Environment).

---

## Verified solid (checked, no action needed)

These were specifically probed or audited and held up:

- **Path traversal / static containment:** `/../config.json`, `/....//config.json`, `/assets/../../server.py` all 404; symlink-aware `resolve()` containment in `_spa` is correct.
- **Page perms:** denied sessions get the exact SPA fallback a missing page gets (verified); the check runs on the resolved path, and on Windows `resolve()` expands 8.3 short-name aliases (`ADMIN-~1`) back to the long name, so the prefix check cannot be skipped via short names or case (verified on this machine). Authorized sessions receive the file.
- **Hidden endpoints over standard methods:** GET/PUT/DELETE/OPTIONS/TRACE on a hidden path are byte-identical 404s to a nonexistent path (verified), and the `__sykit_manifest__` route returns `{}` anonymously without revealing anything (verified).
- **CSRF layers:** cross-site API requests are blocked three ways - `Origin` allowlist + same-origin comparison (403 verified), `Sec-Fetch-Site: cross-site` without Origin (403 verified), and `SameSite=lax` cookies; JSON endpoints also require a non-simple `Content-Type`. The non-API static side is correctly unaffected (verified).
- **Request-body / upload limits:** dual enforcement (`Content-Length` pre-check + counted streaming) at `files/server.py:1214-1277`, per-endpoint multipart caps, disk-spooled temp files closed on all paths, and `max_upload_bytes <= max-request-bytes` enforced at build time.
- **JSON hardening:** duplicate keys and `NaN`/`Infinity` rejected, recursion errors handled, strict content-type required.
- **Sessions:** HMAC-signed cookies with enforced >=32-byte secret, id rotation + old-id deletion on `login()` in store mode (anti-fixation), expired-row cleanup, and 503 fail-closed when a configured store is down.
- **Passwords:** scrypt with bounded composite memory and key length,
  `hmac.compare_digest`, and malformed-hash rejection (`sykit/auth.py`).
- **API keys:** only sha256 hashes stored, generation via `secrets`, scope checks fail closed.
- **Host header policy:** strict allowlist with wildcard support that doesn't over-match (`*.example.com` vs `evil-example.com`), running before anything that trusts `Host`.
- **Rate limiter internals:** transactional sqlite accounting with rollback on exceed (denied requests don't burn budget), per-window cleanup, per-client based on the direct peer (correct given `proxy_headers=False`).
- **Rate limiting works when reached:** verified `200, 200, 429` with `Retry-After` on a `per-client` limit.
- **Build hygiene:** no sourcemaps, `npm ci --ignore-scripts` with a pinned
  lockfile, Windows PATH lookup that excludes the current directory, reserved
  module names blocking stdlib/runtime shadowing, endpoint-path normalization
  rejecting `..`/control chars, generated ignore files exclude secrets and
  state, and Compose requires `SYKIT_SESSION_SECRET` rather than embedding it.

## Suggested production checklist

1. `"session-https-only": true` and a real random `SYKIT_SESSION_SECRET` (32+ bytes) from your secret store.
2. `"session-store": "sqlite"` (or a packaged store) so logout revokes.
3. Exact `"allowed-hosts"` for your domain; keep `"host-ip": "127.0.0.1"` behind the proxy.
4. Launch with the documented `uvicorn --proxy-headers --forwarded-allow-ips=<proxy>` (fixes both client-IP rate limits and the same-origin CORS check from finding 2).
5. `@limits({"per-client": "..."})` on login and unauthenticated endpoints;
   use a proxy-level limit as an additional outer control where appropriate.
6. Set a baseline `"content-security-policy"` (start with `default-src 'self'` and relax as needed).
7. Leave `readiness-path` disabled on public deployments, or restrict it at the proxy.
8. Treat `@hidden` as concealment, not authorization; keep its required
   permission check meaningful.
