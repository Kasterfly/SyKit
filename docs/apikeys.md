# API Keys

API keys let external callers (scripts, services, CI) use `@web_hook`
endpoints without a browser session. Keys are managed from the command
line, sent as a header, checked before the endpoint runs, and can carry
scopes and per-key rate limits.

## Protecting an endpoint

```python
from sykit.utils import api_key, limits, web_hook

@web_hook("report")
@api_key(["reports:read"])
@limits({"per-key": "100m"})
def report(kind: str = "daily"):
    return {"rows": []}
```

- `@api_key` (bare) accepts any active key; `@api_key([...])` requires
  every listed scope.
- `@api_key` only works on `@web_hook` endpoints: browser calls carry
  sessions, not keys, and `$python` client endpoints stay session-based.
  It cannot be combined with `@hidden`.
- Callers send the key as the `X-API-Key` header:

```bash
curl -X POST https://app.example.com/api/report \
     -H "X-API-Key: sykit_..." \
     -H "Content-Type: application/json" -d "{}"
```

Responses: missing, unknown, or revoked key answers
`401 {"error": "A valid API key is required."}`; a valid key without a
required scope answers `403 {"error": "API key scope denied."}`; an
unreachable key store answers 503.

## Managing keys

```
python SyKit keys generate <name> [--scopes a,b]
python SyKit keys list
python SyKit keys revoke <key-id>
```

Run from the project root. `generate` prints the key once; only its
sha256 hash is stored, so a lost key means generating a new one.
`revoke` takes effect on the next request.

## Rate limiting

The `per-key` scope in `@limits(...)` (and `default-limits`) gives every
key its own bucket, so one caller cannot exhaust another's budget. It
requires `@api_key` on the endpoint; build refuses it elsewhere. The
other scopes work on keyed endpoints too.

## Storage

By default keys live in `.sykit-apikeys.sqlite3` in the project root,
next to `src/` - deliberately outside `built/`, so issued keys survive
rebuilds. The `apikey-store` setting changes that:

| `apikey-store` | Behavior |
| --- | --- |
| `""` (default) | sqlite file `.sykit-apikeys.sqlite3` in the project root |
| `"sqlite:path"` | sqlite file at a custom path (relative paths resolve from the project root) |
| `"scheme:target"` | A store registered by a package (see below) |

Deploying the built app means shipping the key database alongside it
(or pointing `apikey-store` at a shared location); the store is read on
every keyed request.

## Writing a key store package

A store package adds one file, `files/core/_keystore_<scheme>.py`, with
a `create(target)` function; no edits to SyKit are needed. The returned
object implements the `ApiKeyStore` interface from `core/_apikeys.py`:

- `lookup(key_hash)`: return the record dict for a sha256 key hash, or
  `None` (called on every keyed request).
- `create(record, key_hash)`: store a new record under its hash.
- `list_keys()`: every record, oldest first, without hashes.
- `revoke(key_id)`: mark a key revoked; `False` for unknown ids.

Records are dicts with `id`, `name`, `scopes` (list), `created` (unix
seconds), and `revoked` (bool). The `python SyKit keys` command works
against any store, so provider packages get the CLI for free.

## Security notes

- Keys are bearer credentials: serve keyed endpoints over HTTPS only,
  and treat a key like a password.
- Only hashes are stored; the plaintext key exists once, at generation.
- Key lookup is by exact hash match, so timing does not leak prefixes.
- Scopes are a coarse permission system for machines; interactive
  visitors should keep using sessions and `@perms`.
