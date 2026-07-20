# Streaming

SyKit 0.12.0 adds one-way server streaming with Server-Sent Events (SSE).
An `@sse` async generator yields JSON-compatible values, and its generated
`$python` wrapper returns an async iterator.

```python
import asyncio

from sykit import perms, sse


@sse("orders/progress")
@perms({"Session": {"role": "staff"}})
async def order_progress(order_id, session):
    for percent in (0, 25, 50, 75, 100):
        yield {
            "order_id": order_id,
            "percent": percent,
            "viewer": session.get("user", ""),
        }
        await asyncio.sleep(1)
```

```js
import { order_progress } from "$python";

for await (const update of order_progress(orderId)) {
  console.log(update.percent);
}
```

The request starts when iteration begins. Parameters use the same JSON-encoded
GET query format as `@raw`. Every yielded value must be JSON serializable.
`request` injection and `Upload` parameters are not supported on a stream.

## Lifecycle and errors

Breaking or returning from `for await` cancels the browser reader. SyKit then
closes the server generator, including its `finally` block. Use that block to
release subscriptions, cursors, or other stream-owned resources.

```python
@sse("events")
async def events():
    subscription = await open_subscription()
    try:
        async for event in subscription:
            yield event
    finally:
        await subscription.close()
```

While an event is pending, SyKit sends an SSE comment every
`sse-heartbeat-seconds` seconds. The default is 15. Comments do not appear in
the JavaScript iterator. They keep an idle connection active through servers
that honor streaming responses.

Once a stream has opened with HTTP 200, its status cannot be changed. If the
generator or JSON encoder fails, SyKit reports the original exception to the
registered error hook and sends a generic terminal stream error. The generated
client throws `SyKitError` with status 500 and does not expose private exception
text. A network failure after opening throws `SyKitError` with status 0.

SSE in 0.12.0 has no automatic reconnect, replay, event ids, or named public
events. Start a new iterator if the application decides to retry. Events can be
delivered again when an application reconnects, so consumers should tolerate
duplicates when that matters.

## Guards and sessions

SSE uses the normal endpoint path and HTTP middleware:

- `@perms` is checked before response headers are sent.
- `@hidden` keeps the route out of the compiled client and unauthorized
  manifest. Direct probes receive the normal hidden 404 response.
- `@cors` and `default-CORS` use the same GET policy as other endpoints.
- `@limits` charges one request when the connection is established, not one
  request per yielded event.

The injected `session` is a recursively read-only snapshot. Direct mutation,
`update_session`, `login`, and `logout` fail the stream. Cookies are committed
before generator code starts, so allowing later writes would silently lose or
misreport state. Change the session in a normal endpoint before opening the
stream.

Background `enqueue()` calls remain available while the generator is active.

## Protocol and deployment

SyKit emits UTF-8 `text/event-stream` responses with JSON `data` fields,
`Cache-Control: no-cache`, and `X-Accel-Buffering: no`. The generated client
uses streaming `fetch` instead of the browser `EventSource` object so parameter
encoding, credentials, hidden routes, cancellation, and `SyKitError` behavior
stay consistent with the rest of `$python`.

Some reverse proxies and hosting platforms buffer responses or enforce idle
and maximum connection timeouts. Configure those layers for streaming even
though SyKit sends heartbeat comments. Browsers can also enforce a low
per-origin SSE connection limit under HTTP/1.1; HTTP/2 multiplexing avoids that
small fixed cap. See the
[MDN SSE guide](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)
and the
[HTML event stream standard](https://html.spec.whatwg.org/multipage/server-sent-events.html)
for protocol details.

SSE is one-way. Use normal endpoints for client-to-server commands. WebSockets
are deferred beyond 1.0 because upgrade-origin checks, session persistence,
mid-connection authorization, rate limits, and error handling need a separate
security lifecycle instead of pretending the HTTP endpoint guards transfer
unchanged.
