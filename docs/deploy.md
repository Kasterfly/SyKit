# Deploying

SyKit builds a self-contained `built/` folder; deploying means running
`python main.py` there with `SYKIT_SESSION_SECRET` set. This page covers
the docker toggle, Compose, and Swarm notes.

## Docker

Set `"docker": true` in `src/sykit/config.json` and rebuild. The build
then also writes three files into `built/`:

- `Dockerfile`: `python:3.12-slim`, installs `requirements.txt`, exposes
  `host-port`, runs `python main.py`.
- `compose.yaml`: builds the image, publishes the port, passes
  `SYKIT_SESSION_SECRET` through from the host environment (Compose
  refuses to start without it), probes the configured `health-path`,
  allows one minute for graceful shutdown, and restarts unless stopped.
- `.dockerignore`: keeps `__pycache__` and the local sqlite state files
  out of the image.

They are regenerated on every build; do not edit them in place. Then:

```bash
cd built
SYKIT_SESSION_SECRET="a-long-random-string" docker compose up --build
```

Two settings matter in containers:

- `"host-ip"` must be `"0.0.0.0"`; the build warns when docker is
  enabled with a loopback address, because the app would be unreachable
  from outside the container.
- `"allowed-hosts"` must include the hostname clients use to reach the
  container (for a published local port, the defaults already cover
  `localhost`).
- `"health-path"` is used by the generated Compose healthcheck. The
  default `/healthz` works without further configuration.

## State in containers

The container filesystem is disposable, so anything sqlite-backed in
the app folder disappears with it:

- Sessions: keep the default signed cookies, or point `session-store`
  at a database (the `postgres-sessions` package) instead of the sqlite
  file.
- API keys: point `apikey-store` at a mounted volume
  (`"sqlite:/data/keys.db"` plus a volume for `/data`), or ship the key
  file deliberately.
- Background calls: point `task-store` at a mounted volume
  (`"sqlite:/data/tasks.db"`) for one replica, or use a shared database
  store for multiple replicas. The default queue is disposable in a
  container.
- Rate limits are per-container by design; that is usually fine.

## Graceful shutdown

On SIGTERM, task runners stop claiming new calls and wait for in-flight calls
to finish. Generated Compose files set `stop_grace_period: 1m` instead of the
short container default. Increase the platform's termination grace above the
longest legitimate task. If a hard kill still interrupts a call, its queue
lease expires so another worker can recover it. See
[Background Tasks](background-tasks.md#delivery-and-failures).

## Uploaded media

The generated `built/static/` directory is the compiled frontend, not a
writable media store. A rebuild replaces it, and multiple replicas do not
share it. Keep uploaded files outside `built/`.

For local media, write to a dedicated mounted directory with generated file
names, then let a trusted reverse proxy serve only the paths meant to be
public. Files that need authorization should go through an authenticated
endpoint instead. For multiple replicas or durable cloud deployments, copy
uploads to object storage while the endpoint is running. An object-storage
package can provide that integration without changing SyKit's multipart API.

Set the reverse proxy's request-body limit intentionally. It may be lower than
SyKit's `max-request-bytes`, but should not be higher by accident. See
[Uploads](uploads.md) for validation and temporary-file lifetime rules.

## Docker Swarm and multiple replicas

The generated compose file works as a stack file:

```bash
docker stack deploy -c compose.yaml myapp
```

Notes for more than one replica (Swarm or otherwise):

- Use a shared `session-store` (for example `postgres-sessions`);
  cookie sessions also work since they need no server state. The sqlite
  store is single-machine.
- Prefer a Swarm/compose secret for `SYKIT_SESSION_SECRET` over an
  environment variable in the stack file; every replica must use the
  same value or sessions bounce between replicas.
- `site-wide` and `per-key` rate limits are per-container: each replica
  keeps its own sqlite bucket file. Front the service with a proxy
  limiter if you need one global budget.
- Every replica must use the same `task-store` for one shared queue and
  single-runner scheduled jobs. Independent sqlite files create independent
  queues. A shared store still provides at-least-once delivery, so task
  effects must be idempotent.
- Behind the ingress or any reverse proxy, the app sees the proxy's
  address; see the reverse-proxy section in
  [Configuration](configuration.md) before trusting forwarded headers.

## Without docker

Nothing in `built/` requires docker: copy the folder to the server,
install `requirements.txt` into a virtualenv, set the secret, and run
`python main.py` under your process manager of choice.
