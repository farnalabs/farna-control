# Compose first-boot latency (FAR-2785)

**Status:** Accepted and implemented
**Date:** 2026-11-26
**Related:** FAR-2771 (the validation that produced the first numbers), FAR-2777 /
FAR-2780 / FAR-2782 (earlier boot fixes), FAR-973 (the Vite bind-mount hang this
is the Python sibling of)

## Context

A cold `docker compose up -d` of the root `docker-compose.yml` took minutes
before `GET /healthz/ready` answered `200` on a Windows / Docker Desktop host
(the reported case: ~12 minutes). That window reads as "broken" to a new user
because Docker Desktop's port proxy accepts on 8000 and then resets, so a client
sees "connection closed unexpectedly" rather than "starting".

The shipped compose bind-mounted `./backend/src` over the image's `/app/src`
for the three Python services (`backend`, `saq-runner`, `saq-system`). The image
already contains the code **and** the virtualenv, and the project is installed
editable: `uv sync` writes a `.pth` that puts `/app/src` on `sys.path`, and the
venv holds no copy of `farna` under `site-packages`. So the bind mount replaced
the image's `/app/src` with the host tree, and **every** Python import in the
container - including the three separate interpreter starts: role bootstrap,
`alembic upgrade heads`, and `uvicorn` - read source files over Docker Desktop's
bind-mount filesystem instead of the image's overlay layer. Any bytecode baked
into the image was shadowed by the host directory.

The mount existed for live editing: change a host file, restart the container.
On a slow bind-mount filesystem that convenience cost minutes per cold boot.

## Measurement protocol

Every number below was measured on one Windows host (Docker Desktop, engine
29.7.2, linux/amd64), one stack at a time, against base commit `c711d45c84`,
with:

- `docker compose down -v` before every run (fresh `pgdata`, so the DB role
  bootstrap and the full Alembic migration chain are part of every boot), then
  `docker compose up -d`;
- the host-side Python `__pycache__` under `backend/src` removed before every
  run, so each boot is bytecode-cold (a first-ever boot's condition);
- **Metric A** = first `200` on `/healthz/ready` minus the backend container's
  `StartedAt` (`docker inspect -f '{{.State.StartedAt}}'`), polling every 5 s;
- **Metric B** = import phase = `(first-200 time − uptime_seconds)` minus the
  entrypoint's `Starting uvicorn...` log timestamp. `uptime_seconds` starts when
  `farna/api/routes/health.py` is first imported, i.e. near the end of the
  `farna.api.main` import chain;
- `./scripts/smoke-compose.sh` (login, checkpointer, checkpoint tables,
  credential round-trip) run against the same boot.

The host was **not** idle-only in the port sense: another session held a
`kubectl -n farna-control port-forward` on host `8000` (`::1` then `127.0.0.1`)
for part of the window. Polling and the smoke check were pinned to
`127.0.0.1:18000` via a measurement-only port override so no request could reach
that foreign deployment. The measured quantity (container start to the
container's own first `200`) is independent of which host port is published.

### Results (seconds)

| Run | Case | A: container → first 200 | B: import phase | pre-uvicorn (bootstrap + migrations) | lifespan / uptime | smoke |
|---|---|---|---|---|---|---|
| baseline 1 | host page cache cold (right after the image build) | **1500.2** | **814.2** | 412.6 | 273.4 | not run |
| baseline 2b | host page cache warm | **1222.8** | **711.0** | 340.6 | 171.2 | PASS |
| candidate 1 | bind mount removed, warm | **181.2** | **68.9** | 77.4 | 34.9 | PASS |
| candidate 1b | bind mount removed, warm | **182.5** | **64.1** | 83.6 | 34.8 | PASS |

Noise band (the two valid baselines, both bytecode-cold, differing only in host
page-cache warmth): **A 1223–1500 s, B 711–814 s**. The two candidate runs agree
to within 1 % (A 181–183 s, B 64–69 s), so the effect is far larger than the
noise.

> One further baseline attempt was discarded as **invalid**: a foreign
> `kubectl port-forward` bound `localhost:8000` mid-run and answered the poll,
> so the "first 200" came from another deployment (its `saq_workers` detail named
> Kubernetes pod hostnames, not this stack's containers). It is excluded rather
> than reported as a data point. The measurement harness now fails loudly if the
> answering app's uptime exceeds the container's own age.

**Metric C (build).** `docker compose build` of the five images took
**2805 s (~47 min)** once, with the BuildKit dependency layers already cached
(`uv.lock` and `pyproject.toml` unchanged), so the cold-build cost here is
dominated by Docker Desktop exporting/unpacking three ~2 GB backend-derived
images (~700 s each), not by the dependency install. The candidate change needs
no rebuild: the image already bakes the code.

### What changed, measured

Removing the `./backend/src` bind mount cut the import phase from ~711–814 s to
~64–69 s (about 12 minutes to about 1 minute) and the whole cold boot from
1223–1500 s to ~182 s. It also shortened the pre-uvicorn phase (340–413 s to
77–84 s) and lifespan (171–273 s to ~35 s): those phases each start their own
Python interpreter and import `farna` (role bootstrap, Alembic, and the lazy
imports inside the lifespan), so they were paying the same bind-mount tax.

## Decision

Remove the `./backend/src` bind mount from the three Python services in the
default `docker-compose.yml`, so the container imports the code and the venv
from the image. Move the mount into an **explicit** `docker-compose.dev.yml`
override for live editing.

The `./frontend/src/manifest.yaml` bind mount **stays**. The backend image cannot
bake it: its build context is `./backend`, which cannot reach
`../frontend/src/manifest.yaml`. The runtime serves that file (the `/manifest`
route, the assistant primer, the documentation indexer), and a missing file
degrades to an empty manifest, which changes product behaviour. It is a single
small file read once and cached in-process, so it is not part of the boot cost.
(Verified in the built image: `/app/manifest.yaml` does not exist without the
mount.)

### How a contributor runs from source afterwards

Default (fast) path - runs the image's code, no live edit:

```bash
docker compose up -d
```

Live-edit path - re-adds the host mount, edit + restart picks up changes with no
rebuild:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

`docker-compose.dev.yml` is deliberately a separate file, **not**
`docker-compose.override.yml`: Compose auto-loads `docker-compose.override.yml`
for everyone, which would silently restore the slow default for every install.
An explicit `-f` keeps read-from-image the default and makes live editing a
conscious choice.

## Options considered and rejected

1. **Named volume at `/app/src`, populated from the image at start** (the
   ticket's option 2). Rejected: it needs an init sidecar or an entrypoint copy
   that recopies the whole tree on every container start (trading a slow first
   import for a slow copy), and it removes the live-edit behaviour entirely
   while adding moving parts. The explicit dev-override bind mount keeps
   live-edit with no new machinery.
2. **`python -m compileall -q /app/src` at build** (the ticket's option 3).
   Not needed after option 1: with imports on the image's overlay layer the
   remaining import phase is ~1 minute, and bytecode written at first boot goes
   to the container's writable layer, not the host. It would cost a full rebuild
   (measured ~47 min on this host) to shave a fraction of that ~1 minute, so it
   was not measured here. `PYTHONDONTWRITEBYTECODE` is not set in the image, so
   bytecode is written normally.
3. **A BuildKit cache mount for the dependency install** (the ticket's option
   4). **Already present**: `backend/Dockerfile` uses
   `--mount=type=cache,target=/root/.cache/uv` and `.../pip` on the dependency
   steps. No change needed; the cold-build cost is image export/unpack, not the
   dependency install.
4. **Cut import-time work (lazy imports, bind the listener before startup
   finishes, a "starting" state the proxy does not reset)** (the ticket's option
   4/5). Out of scope and higher risk: it changes application startup ordering,
   which this change must not do. The bind-mount removal already removes the
   dominant cost. The "connection reset while booting" UX is a separate concern.
5. **A `depends_on` / healthcheck ordering tweak so the frontend waits for the
   API** (the ticket's option 6). Not a boot-time win and not needed for the
   measured metric; left alone.

## Consequences

- The default stack runs the code baked into the image. A host edit is not
  picked up until the image is rebuilt, **unless** the contributor opts into
  `docker-compose.dev.yml`.
- With the mount gone, Python writes `__pycache__` inside the container layer
  rather than into the contributor's checkout, so the host tree stays clean.
- A new architecture test
  (`backend/tests/architecture/test_compose_default_runs_from_image.py`) pins
  that the default compose does not bind-mount `/app/src` and that the dev
  override still does, so a future edit cannot silently reintroduce the slow
  default or break live editing.

## Not measured / not acted on

- **Cold-build time** is reported (Metric C) but not optimised here: it is a
  separate axis (image export/unpack on this host), and this change needs no
  rebuild. Followed up separately in
  [`compose-cold-build-performance.md`](compose-cold-build-performance.md)
  (FAR-2826), which found that Metric C's cost was paid three times for one
  image and that `chown -R appuser /app` - not the export - is the largest
  single step.
- **Cold-start DB race.** One boot during measurement had the backend exit `1`
  with `ConnectionRefusedError` to `db:5432`: `pg_isready` reported the DB
  healthy while Postgres was still cycling through its initdb restart, so
  `depends_on: service_healthy` released the backend into a refused connection
  and, with no `restart:` policy, it stayed down. This is a pre-existing
  cold-first-boot race (the same family as FAR-2780), unrelated to the import
  change; it is reported here, not fixed.
- **Non-Windows hosts** were not measured. The mechanism (imports over Docker
  Desktop's bind-mount filesystem) is host-dependent; on a Linux host with a
  native bind mount the baseline would be smaller, and the win proportionally
  smaller but still positive (imports move off the bind mount and onto the image
  layer).
