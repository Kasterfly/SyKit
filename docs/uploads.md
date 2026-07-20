# Uploads

Annotate one or more `@expose` parameters as `Upload` to accept multipart
files. SyKit changes only that endpoint to `multipart/form-data`; endpoints
without an upload parameter keep their JSON request format.

```python
import os
from pathlib import Path
from shutil import copyfileobj
from uuid import uuid4

from sykit import Upload, expose

MEDIA_ROOT = Path(os.environ["APP_MEDIA_ROOT"])


@expose("save_document", max_upload_bytes=5242880)
def save_document(file: Upload, title: str):
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}.bin"
    with (MEDIA_ROOT / stored_name).open("wb") as destination:
        copyfileobj(file.file, destination)
    return {"name": stored_name, "title": title, "bytes": file.size}
```

The generated `$python` function accepts browser `File` and `Blob` values:

```js
import { save_document } from "$python";

const selected = document.querySelector("input[type=file]").files[0];
const result = await save_document(selected, "Quarterly notes");
```

SyKit sets the multipart boundary. Do not set a `Content-Type` header in the
browser. Other parameters are JSON-encoded inside the multipart form so their
values match normal `@expose` calls. A manual client must JSON-encode every
non-file field as well.

## The `Upload` object

An endpoint receives a disk-backed `Upload` with:

- `file`: the seekable binary temporary file
- `size`: file content bytes, excluding multipart headers
- `client_filename`: the name declared by the client
- `client_content_type`: the content type declared by the client, or `None`
- `read(size=-1)`, `seek(offset, whence=0)`, and `tell()` convenience methods
- `closed`: whether the temporary file is closed

`client_filename` and `client_content_type` are untrusted hints. Never join the
client filename to a storage path, choose executable behavior from its
extension, or accept a file because its declared type looks safe. Generate a
storage name and validate the actual bytes with a parser suitable for the file
format.

Each upload parameter accepts exactly one file. Declare separate parameters
for separate files. Optional files use a normal default:

```python
@expose("images")
def images(primary: Upload, preview: Upload | None = None):
    ...
```

Duplicate multipart fields and wrong field types are rejected.

## Size limits

`max-request-bytes` is the global hard ceiling for every request. It counts
the entire multipart body, including field headers and boundaries, and is
enforced from `Content-Length` when available and again while bytes stream in.

`max_upload_bytes` adds a lower ceiling to one `@expose` endpoint:

```python
@expose("small_avatar", max_upload_bytes=262144)
def small_avatar(file: Upload):
    ...
```

The value must be a positive integer literal and cannot exceed
`max-request-bytes`. It also counts the complete multipart body. When omitted,
the endpoint uses the global cap. Requests over either limit receive 413.

The total file content must be slightly smaller than the configured limit
because multipart metadata also counts. Text fields are bounded by the same
limit, and the parser also limits their count to the endpoint's declared
parameter count.

## Temporary files and cleanup

File parts are written to operating-system temporary storage as they arrive;
SyKit does not retain the file content in request-body memory. The temporary
files are closed after the endpoint returns, raises an exception, fails input
validation, or crosses a streamed size limit.

An `Upload` is therefore valid only while its endpoint is running. Copy or
process all needed bytes before returning. A streaming response cannot keep
reading the temporary upload after the endpoint has returned.

## Where media belongs

`built/static/` contains generated frontend assets. Rebuilds replace it, so it
is not an upload destination.

- For one server, use a dedicated directory outside `built/`, generated file
  names, and a reverse proxy for deliberately public files.
- Serve private files through an endpoint that checks authorization.
- For containers, mount the media directory; the container filesystem is
  disposable.
- For multiple replicas or durable deployments, copy the upload into object
  storage during the endpoint call. A storage package can wrap that copy and
  return an object key or URL.

See [Deploying](deploy.md#uploaded-media) for reverse-proxy and container
guidance.
