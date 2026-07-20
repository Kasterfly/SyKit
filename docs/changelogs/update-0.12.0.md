# SyKit 0.12.0 - Streaming Update

SyKit 0.12.0 adds guarded one-way streaming without adding a new dependency.

## Added

- `@sse("path")` declares an async-generator GET endpoint. Each yielded
  JSON-compatible value is sent as an SSE data event.
- Generated `$python` wrappers return async iterators backed by streaming
  `fetch`. The parser handles UTF-8 chunk boundaries, CRLF or LF framing,
  comments, multiline data, cancellation, and terminal errors.
- `sse-heartbeat-seconds` controls idle keepalive comments and defaults to 15.
- Hidden-stream records resolve through the authorized runtime manifest without
  embedding their route in the compiled client.

## Security and lifecycle

- Permissions, hidden-route behavior, endpoint CORS, and rate limits run before
  the stream opens. A rate limit is charged once per connection.
- The stream receives a recursively read-only session snapshot. Session writes
  cannot be promised after response cookies have already been committed.
- Client cancellation and disconnects close the Python generator so its
  `finally` block runs.
- Exceptions after HTTP 200 reach the normal error hook and become a generic
  terminal `SyKitError`; private exception text stays server-side.
- Responses disable caching and advertise no buffering to compatible Nginx
  deployments.

## Scope decision

WebSockets are deferred beyond 1.0. Their upgrade origin checks, mutable session
lifecycle, mid-connection authorization, rate limits, and error contract do not
inherit the current HTTP endpoint guarantees cleanly. Shipping SSE alone keeps
0.12.0's guard behavior explicit and testable.
