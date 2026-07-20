# SyKit 0.10.0 - Uploads Update

SyKit 0.10.0 adds safe multipart file handling to exposed endpoints and their
generated browser clients.

## Added

- The public `Upload` annotation for one or more parameters on `@expose`
  endpoints.
- Generated `$python` wrappers that send browser `File` and `Blob` values as
  `FormData` while preserving JSON values for the endpoint's other parameters.
- Disk-backed multipart file parsing with automatic temporary-file cleanup on
  success, input errors, endpoint exceptions, and streamed size-limit exits.
- Optional `max_upload_bytes` on `@expose`, bounded by the global
  `max-request-bytes` setting.
- Documentation for byte validation, untrusted client metadata, local media,
  reverse proxies, containers, and object storage.

## Safety behavior

- `max-request-bytes` remains the global request ceiling and now explicitly
  covers multipart uploads and their protocol overhead.
- Each multipart endpoint accepts only its declared number of file and JSON
  fields. Duplicate fields, wrong field types, and invalid JSON fields return
  400.
- Client filenames and content types are exposed only as explicitly named
  untrusted metadata. Applications must validate file bytes and generate safe
  storage names.
- Temporary uploads are closed before the response is sent. Applications must
  copy or process needed data during the endpoint call.

## Upgrade note

Install the updated Python requirements, then refresh existing project config
modules before rebuilding:

```bash
python -m pip install -r SyKit/requirements.txt
python SyKit init
python SyKit build
```

`python-multipart` is the only new runtime dependency. Existing endpoints stay
JSON-only unless one of their parameters is annotated as `Upload`.
