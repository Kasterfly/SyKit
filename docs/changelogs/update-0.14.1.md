# SyKit 0.14.1 - CI Test Dependency Patch

SyKit 0.14.1 fixes the failed unit-test matrix from the 0.14.0 release without
changing framework runtime behavior or the frozen compatibility candidate.

## Fixed

The six Linux and Windows unit-test jobs installed only `requirements.lock`
before running the complete development test suite. One background-task
integration test launches a generated runtime probe with Starlette's
`TestClient`, which requires the development-only `httpx2` transport. Those
jobs therefore failed before the probe could exercise SyKit.

The unit-test matrix now caches and installs `requirements-dev.lock`. Coverage,
browser E2E, audit, and lint jobs already used the development lock.

## Runtime dependencies remain unchanged

`httpx2` remains development-only. Generated applications and production
containers do not use Starlette's test client, so the generated-container CI
job continues to install only `requirements.lock`. This preserves the smaller
runtime dependency surface introduced in 0.14.0.

## Regression coverage

CI workflow tests now verify both sides of the dependency boundary:

- The unit-test matrix must install `requirements-dev.lock`.
- The generated-container job must install `requirements.lock` and must not
  install the development lock.

No application migration or rebuild is required for this patch.
