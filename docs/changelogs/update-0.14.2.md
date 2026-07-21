# SyKit 0.14.2 Security Patch

SyKit 0.14.2 is a pre-1.0 security patch for session persistence, task
recovery, request handling, build tools, password verification, and generated
file exclusions.

## Security fixes

- A request without a valid session cookie no longer creates stored session
  state when the session contains only internal SyKit keys. Anonymous endpoints
  should still use `per-client` limits because clients can omit cookies.
- `login()` keeps the current internal rate identity while replacing user
  claims and rotating the server-side session id.
- Background calls stop being reclaimed after `task-max-attempts` claims. The
  default is `3`. Encoded task payloads are capped at 1 MiB, and failed rows
  in the built-in SQLite queue are kept for seven days.
- Windows builds resolve Node.js and npm only from explicit PATH directories,
  skipping the current project directory.
- Repeated `X-API-Key` headers return 400. The access log fingerprints a key
  only when exactly one header is present.
- Static files matched by `page-perms` use `Cache-Control: no-cache`, including
  files under `assets/`.
- Stored scrypt hashes are rejected before derivation when their composite
  memory request or derived-key length exceeds the verification bounds.
- Builds add `built/` and `__sykitcache__/` to `.gitignore` when missing.
  Generated `.dockerignore` files exclude `.env` and API key SQLite state.

## Background task note

A task that hangs can keep renewing its lease, occupy a concurrency slot, and
delay graceful shutdown. Use timeouts for network and subprocess work. Forced
termination makes the call recoverable after lease expiry; the attempt cap
prevents an endless process-crash loop.

## Upgrade

Install the package on SyKit 0.14.1 with `--yes --allow-core`, reinstall the
locked requirements if needed, run `python SyKit init` for existing apps, and
rebuild generated applications.
