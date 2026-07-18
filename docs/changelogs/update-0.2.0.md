# SyKit 0.2.0 - Endpoint Safety Patch

Endpoints a visitor lacks permission for are now undiscoverable, both over
HTTP and in the compiled frontend bundle.

## Added

- **`@hidden` guard** (`sykit.utils.hidden`): stack it bare on an endpoint
  alongside `@perms`. When a visitor fails the permission check, the endpoint
  answers `404 {"error": "Endpoint not found."}` (byte-identical to a route
  that does not exist) instead of 401/403.
- **All-method API 404 catch-all**: unknown paths under the API prefix answer
  the same 404 for every HTTP method (previously a POST to an unknown path
  returned a distinguishable 405), so hidden, missing, and wrong-method
  requests are indistinguishable.
- **Runtime-resolved client wrappers**: the generated `$python` wrapper for a
  hidden endpoint embeds no URL, method, or parameter names, only an opaque
  token that changes every build. On first call the client asks the new
  reserved `__sykit_manifest__` endpoint which hidden endpoints the current
  session may use and resolves the route from that answer. After a login the
  next call re-checks automatically; no reload needed.
- **`hidden_api()` fallback**: for sessions without access, the wrapper
  throws the same `SyKitError` (status 404, "Endpoint not found.") a
  nonexistent endpoint produces, so inspecting or exercising a public page's
  code reveals nothing about endpoints it may not use.
- **Build-time rules**: `@hidden` takes no arguments and requires session
  permissions (`@perms` or `default-perms`); `@hidden` + `@cors` is rejected
  (a per-endpoint CORS rule would make the endpoint detectable); the endpoint
  path `__sykit_manifest__` and the `$python` export name `hidden_api` are
  now reserved.
- **Tests**: `tests/test_hidden_endpoints.py` covers the parser, build
  validation, generated client module, live ASGI server behavior, and the
  client runtime under Node.

## Changed

- POST (or any non-GET method) to a visible GET-only endpoint now returns 404
  instead of 405, as part of the catch-all above.
- Docs: `docs/endpoints.md` gained a `### @hidden` guard section.

## Notes

- Hidden tokens are random per build, so rebuilding changes the bundle hash
  even with unchanged sources.
- Visible endpoints keep the documented 401/403 permission behavior.
