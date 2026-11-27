# Compose cold-build latency (FAR-2826)

**Status:** Accepted and implemented
**Date:** 2026-11-26
**Related:** FAR-2785 (the sibling change that made the default stack run from the
image; its `docs/compose-first-boot-performance.md` Metric C is the number this
follows up), FAR-2808 (compose restart policies, merged into the same file)

## Context

A cold `docker compose build` of the root stack was reported at **2805 s
(~47 min)**. That figure was already known not to be the dependency install:
`backend/Dockerfile` uses BuildKit cache mounts for uv and pip, so the package
layers are reused. The cost is the two steps that run **per image**, and the
stack built the same image three times.

`backend`, `saq-runner` and `saq-system` are the same Dockerfile and the same
`./backend` build context, differing only in `command:`. Each carried its own
`build: ./backend`, so Compose produced three ~2 GB images and repeated both
per-image costs three times.

## Measurement protocol

One Windows host (Docker Desktop, engine 29.7.2, linux/amd64), one stack at a
time, base commit `ecd445feec`, with:

- `--no-cache` for the cold single-image builds, so no layer of the target image
  is reused. The shared BuildKit **cache mounts** (uv, pip, apt) were **not**
  cleared — other work on this host depends on them — so dependency *downloads*
  can still be served from those mounts. Stated per row below.
- `docker compose down -v` before every acceptance boot, so Postgres initdb and
  the full Alembic chain are part of every boot.
- Metric A = first `200` on `/healthz/ready` minus the backend container's
  `StartedAt`; Metric B = the same `200` minus the `up` command's start.
- `./scripts/smoke-compose.sh` (login, checkpointer, checkpoint tables,
  credential round-trip) against the same boot.

### Results

| # | Command | Cache state | Total | of which `chown -R /app` | of which export/unpack |
|---|---|---|---|---|---|
| M1 | `docker compose build --no-cache backend` (before) | no image layer cached; BuildKit dependency mounts warm; host otherwise idle | **2024 s** | 897 s | 601 s |
| M2 | `docker compose build --no-cache saq-runner` (before) | same, but a concurrent host job shared I/O for the first ~6 min | **2683 s** | 1431 s | 649 s |

M1 and M2 are the **same work** — the second and third copies of one image —
so their spread (2024–2683 s, driven by disk contention) is the noise band. Both
agree on the shape: two per-image steps account for ~74 % of the build.

Full M1 breakdown (`--no-cache`, one image):

| Step | Time |
|---|---|
| `apt-get install postgresql-client` | 108.3 s |
| `pip install uv` | 59.3 s |
| `uv sync` (dependency layer) | 216.8 s |
| `uv sync` (project layer) | 7.8 s |
| **`useradd … && chown -R appuser:appuser /app`** | **897.1 s** |
| **export + unpack into the image store** | **601.5 s** |
| provenance | 8.9 s |
| internal / context load | ~124 s |

### What the numbers revise

Metric C in the sibling doc attributed the cold build to "export/unpack of three
~2 GB images (~360 s each)". That is half the story. `chown -R appuser:appuser
/app` is **larger than the export** — 897–1431 s against 601–649 s — because it
copy-on-write duplicates the entire `/app` tree (the 645 MB virtualenv and the
42 MB source) into a new layer just to change ownership: it is the 700 MB layer
in `docker history`. Both steps run once per image, so today both run three times.

The image store told the same story before the change: three images of the same
content, `farna-control-backend:latest`, `farna-control-saq-runner:latest` and
`farna-control-saq-system:latest`, each **2.09 GB** with a different image ID —
6.3 GB of disk for one logical image.

### After the change

| # | Command | Cache state | Result |
|---|---|---|---|
| M3 | `docker compose up -d --build` (acceptance, tag already present) | all backend layers CACHED; frontend rebuilt | exit 0, up 490 s, ready 200, smoke 5/5 |
| M4 | `docker compose up -d --build` (acceptance, shared tag **removed** first: the first-run path) | backend layers CACHED, image absent | see below |

M3 boot detail: backend container start → first `200` = **196.5 s** (cold
Postgres initdb included), `up` command → first `200` = 681 s of which 490 s was
the build. All three Python services ran the **same image ID**
(`sha256:8f0915a3a6…`), one 2.03 GB entry in the store instead of three.
`docker compose ps` showed every service up, and readiness reported SAQ workers
live on both queues (`runs` and `system`), which is the runtime proof that the
shared image plus each service's own `command:` still yields one API and two
different workers.

M4 (the first-run path, shared tag absent):

| Metric | Result |
|---|---|
| `docker compose up -d --build` | **exit 0**, 101.5 s (backend layers CACHED; only backend, frontend and watchdog built) |
| backend container → first `200` | **214.5 s** |
| `up` command → first `200` | **308.8 s** |
| `./scripts/smoke-compose.sh` | **5/5 PASS** |

M4 is the shape of a first run: Compose attempted the pull for the two worker
services first (see *Known trade-off*), failed it harmlessly, built the three
images that have a `build:`, and started all seven services. The build itself is
fast here only because M1–M3 left every backend layer cached; a machine with no
layer cache pays M1 once.


## Decision

Build the backend image once and share it:

- `backend` keeps `build: ./backend` and gains
  `image: farna-control-backend:latest`;
- `saq-runner` and `saq-system` reference the same `image:` and carry **no**
  `build:`;
- each keeps its distinct `command:`.

Verified invariants: `docker-compose.dev.yml` still merges
`./backend/src:/app/src` into all three services (checked with
`docker compose -f docker-compose.yml -f docker-compose.dev.yml config`), so live
editing is unchanged, and
`backend/tests/architecture/test_compose_single_backend_image.py` pins the new
shape (one image, one `build:`, local-only tag, workers keep their `command:`).

### Why not "same tag plus `build:` on all three"

That variant removes the one wart below, because no service references an image
it does not build. It was rejected on measurement, not taste: `--no-cache`
disables BuildKit's cross-service cache reuse, so under the cold-build protocol
above each of the three `build:` blocks would be a **full** rebuild — three
`chown` layers (897 s each) and three exports. The variant only looks free when
the cache is already warm, which is precisely the case a cold build is not.

## Options considered and rejected

1. **Shrink the image instead (the ticket's second candidate).** Measured from
   `docker history --no-trunc`: the 700 MB `chown -R` layer is pure duplication
   of `/app`, and it is the single largest layer in a 2.09 GB image. Moving
   `useradd` above the `COPY`s and switching them to `COPY --chown=appuser:appuser`
   would remove that layer entirely and cut roughly a third of the image. Not
   done here: it changes the runtime ownership of a shipped artifact and wants
   its own measured change on a separate branch; recorded as the recommended
   follow-up, with the numbers now in hand.
2. **Pull the published image instead of building (the ticket's third
   candidate).** Evaluated with real registry numbers. `publish-images.yml`
   already builds `ghcr.io/farnalabs/farna-control/backend` from
   `backend/Dockerfile` with context `backend` — the identical artifact — and
   tags it `type=sha` plus `latest` on `main`. The linux/amd64 manifest is
   **382.9 MB compressed** (13 layers) versus a 2024 s cold build and a 2.09 GB
   local store; the all-in-one image is 417.3 MB compressed (2.14 GB on disk).
   So pulling is dramatically cheaper in compute. It is not adopted as the
   *default*, because the dev compose exists to run the working tree: a
   published image cannot contain an un-pushed edit, and `latest` is a released
   gate rather than "my checkout". It also trades a local build for a trust
   dependency on GHCR provenance. The right shape is an explicit opt-in overlay
   (a `docker-compose.pull.yml` pinning a `sha-` tag), tracked separately.
3. **A BuildKit cache mount for the build itself.** Not applicable: the
   dominant steps are `chown` (a filesystem write) and export/unpack (the image
   store), neither of which a cache mount addresses.

## Known trade-off

On a machine that has never built the stack, Compose resolves the two
worker services before it builds `backend`, so `up` prints two benign
`pull access denied for farna-control-backend` lines and then builds and starts
everything normally. It is cosmetic and only on the first build; after the image
exists, no pull is attempted. It is the price of the minimal diff and of keeping
the cold-build cost at one pass. (Verified: `up` still exits 0 and all services
start — see M4.)

## Consequences

- A cold `docker compose build` runs the backend Dockerfile **once** instead of
  three times: two per-image steps of 897–1431 s and 601–649 s are paid once, so
  the backend-family share of the build drops by roughly two thirds.
- The image store holds one 2.03 GB backend image instead of three 2.09 GB
  copies.
- `docker compose build backend` remains the way to (re)build the shared image;
  `docker compose build saq-runner` alone is no longer meaningful (it has no
  `build:` and would try to pull). `docker compose build` and
  `docker compose up --build` both build what is needed.
- The tag is a plain local name, so two checkouts on one host that build
  concurrently share it. That is the same class of collision the derived
  `<project>-<service>` names had (project name is the directory name), and it
  is why the tag is deliberately not a registry-qualified reference.

## Not measured / not acted on

- **`docker-compose.local.yml` has the same duplication**: `saq-runner` and
  `saq-system` each carry `build: ./backend` (two images, not three). It is a
  different stack with no `backend` service to anchor the build on, and it was
  out of scope here; the same change applies to it.
- **Whole-stack `--no-cache` build was not re-measured end to end.** The budget
  was two cold single-image builds plus the acceptance boots. The saving is
  arithmetic from M1/M2 (the two per-image steps are identical work) rather than
  a second 47-minute run.
- **Non-Windows hosts** were not measured. The `chown` cost is a
  copy-on-write write to the image layer and the export cost is the image store,
  both of which are faster on a native Linux filesystem; the direction of the
  win is the same, its size is not measured.
- **Two worker-log errors were observed on a fresh volume during acceptance**
  and are unrelated to this change (neither is reachable from image naming):
  a one-off `relation "system_config" does not exist` from `saq-runner` in the
  first minute (a boot race against the migration chain; it stopped recurring),
  and a persistent `permission denied for table checkpoints` in `saq-system`'s
  `stale_run_recovery` sweep. `pg_class.relacl` confirms the cause of the
  latter: `checkpoints` and `checkpoint_migrations` are the only tables with no
  grants — LangGraph creates them at runtime — so any role other than the owner
  is denied. Reported separately.
