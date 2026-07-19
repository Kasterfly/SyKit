# SyKit 0.8.0 - Deploy Update

SyKit 0.8.0 adds docker support to the build. Full guide:
`docs/deploy.md`.

## Added

- **`docker` setting:** with `"docker": true`, every build also writes
  `Dockerfile`, `compose.yaml`, and `.dockerignore` into `built/`,
  generated from the config: the image exposes `host-port`, Compose
  publishes it and passes `SYKIT_SESSION_SECRET` through from the host
  (and refuses to start without it), and the sqlite state files stay
  out of the image. The files are regenerated on every build.
- **Loopback warning:** enabling docker with a loopback `host-ip`
  prints a warning to set `"0.0.0.0"`, since the app would otherwise be
  unreachable from outside the container.
- **`docs/deploy.md`:** docker usage, Compose, Docker Swarm notes
  (shared session stores for replicas, secrets for the session secret,
  per-container rate limits), state-in-containers guidance, and
  non-docker deployment.

## Upgrade notes

- The toggle is off by default; nothing changes for existing apps.
- Multi-replica deployments should use a shared `session-store` (for
  example the `postgres-sessions` package) or stay on cookie sessions;
  the sqlite session store is single-machine.
