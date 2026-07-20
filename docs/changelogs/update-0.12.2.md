# SyKit 0.12.2 - Security Hardening Update

SyKit 0.12.2 fixes three security findings in the built app and adds
security-event logging and startup warnings that make misconfiguration and
abuse easier to spot. No SyKit API changed; existing sessions, API keys, and
builds keep working.

## Fixed

- **Hidden endpoints were enumerable with non-standard HTTP methods.** The
  API catch-all only covered the standard methods, so any other method token
  (`CONNECT`, `QUERY`, ...) fell through to Starlette's partial-match 405,
  whose `Allow` header differed between a real hidden endpoint and a
  nonexistent path. A new middleware layer answers every non-standard method
  under the endpoint prefix with the same
  `404 {"error": "Endpoint not found."}` as an unknown path, so the
  documented "indistinguishable for every HTTP method" guarantee now holds.
- **Failed API-key and permission checks bypassed rate limiting.** Both
  checks ran and returned before the limiter, so probing with bad keys or no
  session consumed no budget. Rate limiting now runs before authentication
  errors are returned: invalid requests consume the configured `per-client`,
  `per-session`, and `site-wide` budgets and start answering 429 once
  exhausted. Hidden endpoints keep their indistinguishable 404 for failed
  permission checks even when over budget.
- **Same-origin requests were rejected behind TLS-terminating proxies.** The
  origin check compares `Origin` against the connection scheme, and the
  built-in launch never trusted forwarded headers, so every browser POST
  failed with `403 {"error":"Origin is not allowed."}` behind a standard
  HTTPS reverse proxy. See the new `trust-proxy` setting below.

## Added

- **`trust-proxy` setting.** `false` by default (unchanged behavior). When
  `true`, the built-in Uvicorn launch honors `X-Forwarded-*` headers from
  loopback proxies (or `$FORWARDED_ALLOW_IPS`), which restores correct
  client addresses for `per-client` rate limits and the correct scheme for
  the same-origin check behind a trusted reverse proxy. Enable it only when
  the app is reachable solely through that proxy; see
  [Reverse proxies](../configuration.md#reverse-proxies).
- **Security-event logging.** Rejected API keys (logged with a sha256
  fingerprint, never plaintext), failed session-permission checks, rejected
  cross-origin requests, and rate-limit 429s now appear in the server log.
  Request access records continue to use the configured `text` or `json`
  format.
- **Startup warnings.** The server warns when `session-https-only` is off
  while `allowed-hosts` is not loopback-only (cookies would travel
  unencrypted), and when `per-session` rate limits are configured with
  signed-cookie sessions (that scope resets when the cookie is cleared).

## Changed

- Requests that fail authentication on a rate-limited endpoint can now
  receive `429` instead of `401`/`403` once their budget is exhausted. This
  is deliberate; clients should already treat 429 as retryable via the
  `Retry-After` header.
- Documentation: the reverse-proxy section now covers the scheme/CORS
  interaction, `docs/apikeys.md` covers failed-key throttling and key
  rotation, `docs/observability.md` warns that the readiness response
  reveals backend topology, and `docs/configuration.md` notes the Windows
  `.env` permissions behavior and a starting Content-Security-Policy.

## Upgrade notes

- Rebuild your project (`python SyKit build`) so the new runtime and the
  `trust-proxy` config key land in `built/`.
- If you deploy behind a reverse proxy that terminates TLS, set
  `"trust-proxy": true` (or keep launching Uvicorn manually with
  `--proxy-headers --forwarded-allow-ips=<proxy>`) and confirm the proxy
  strips client-supplied forwarded headers from anywhere else.
- Production check, unchanged but now warned about at startup: set
  `"session-https-only": true` and prefer a server-side `session-store` so
  logout revokes.
