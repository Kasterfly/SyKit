# SyKit 0.12.1 - Test Client Dependency Patch

SyKit 0.12.1 declares the HTTP transport required by Starlette's test client.

## Fixed

- Added `httpx2>=2.0.0,<3.0` to the runtime requirements, matching the
  dependency range supported by Starlette 1.3.1.
- Clean installs can import and use `starlette.testclient.TestClient` without
  relying on an older `httpx` package already present in the environment.
- The background-task runtime integration test now runs consistently in local
  and GitHub CI environments.

No SyKit API or runtime behavior changed in this patch.
