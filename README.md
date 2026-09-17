# Nexucon Backend

Django 4.2 / Django REST Framework backend for the Nexucon civic-tech and
non-destructive-testing (NDT) oversight platform. It serves the Government
Command Center, the Inspector PWA, the Client Portal, the Professional Hub and
the Public Transparency portal from one API under `/api/v1/`.

The governing rule of this codebase is simple and it is enforced in review:
**the platform never invents a value.** Where a fact has not been recorded, the
API says so — `null`, `''`, an empty list, a 404 with a reason — and it never
substitutes a placeholder, a sample, a random number or a plausible default.
Every contract in this document that mentions a nullable field exists to make
that rule checkable.

---

## Contents

- [Stack](#stack)
- [Applications](#applications)
- [Running it](#running-it)
- [Testing](#testing)
- [Configuration](#configuration)
- [Management commands](#management-commands)
- [API contracts](#api-contracts)
  - [The response envelope](#the-response-envelope)
  - [Absent states, per endpoint](#absent-states-per-endpoint)
  - [Telemetry sessions](#telemetry-sessions)
  - [Manual import](#manual-import)
  - [Offline sync queue](#offline-sync-queue)
  - [Geofence enforcement](#geofence-enforcement)
  - [File evidence](#file-evidence)
  - [Project scoping](#project-scoping)
- [Documentation](#documentation)

---

## Stack

| Layer | Choice |
| :--- | :--- |
| Framework | Django — `requirements/base.txt` declares `>=4.2,<5.0` (see the note below), Django REST Framework, drf-spectacular |
| Auth | SimpleJWT (bearer header **or** HttpOnly cookie), TOTP 2FA, API keys for machine clients |
| Database | PostgreSQL (+ PostGIS when `ENABLE_GIS=True`); SQLite for development and tests |
| Queue | Celery + Redis (`CELERY_BROKER_URL`), optional — sync processing is synchronous by design |
| Storage | Local filesystem, Cloudflare R2, or Cloudinary (`STORAGE_PROVIDER`) |
| Schema | drf-spectacular → `/api/v1/schema/`; Swagger UI at `/api/v1/schema/swagger-ui/` (also aliased to `/swagger/` and `/docs/`), ReDoc at `/redoc/` |
| Email | Resend |

## Applications

28 local apps are installed. `apps.common` is shared infrastructure (base
models, permissions, pagination, hashing); the other 27 are domain apps.

| Domain | Apps |
| :--- | :--- |
| Identity & access | `accounts`, `government`, `settings` |
| Projects & regulatory | `projects`, `applications`, `permits`, `approvals`, `compliance`, `audit`, `analytics` |
| Field execution | `inspections`, `evidence`, `scans`, `telemetry`, `data_import`, `sync` |
| Instrumentation | `digital_eye`, `processing` |
| Records & reporting | `documents`, `reports`, `bim`, `storage` |
| People & public | `stakeholders`, `notifications`, `emergency`, `public_portal`, `monitoring` |

### The ingestion layer

`telemetry`, `data_import` and `sync` are one layer with three entry points, and
they are deliberately separate apps rather than one:

- **`telemetry`** takes a live instrument stream (GPR, PUNDIT/UPV, SLAM, GNSS,
  thermal) packet by packet, hash-chains it, and at session end **promotes** it
  into the statutory registries that already exist in `digital_eye` and `scans`.
  It is an *envelope*, not a second set of sensor tables.
- **`data_import`** takes a file a person filled in, validates every row, and
  commits the whole file or none of it.
- **`sync`** is the journal a reconnecting PWA replays through. Its `entity_type`
  includes `TELEMETRY`, which is why it is a peer of `telemetry` rather than a
  child — the two have opposite dependency directions.

## Running it

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements/development.txt

cp .env.example .env                              # then fill it in — see Configuration
python manage.py migrate
python manage.py seed_rbac                        # roles and permissions — required
python manage.py runserver
```

`manage.py` defaults to **`config.settings.production`**, which raises at import
time without `DJANGO_ALLOWED_HOSTS` and a real `DJANGO_SECRET_KEY`. For local
work set the settings module explicitly:

```bash
# bash
export DJANGO_SETTINGS_MODULE=config.settings.development
# PowerShell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.development'
```

Development settings use SQLite (`db.sqlite3`) and permissive CORS. Or use
`docker compose up`, which wires PostgreSQL and Redis.

## Testing

The suite is Django's own test runner — **no pytest, no factories, no
`conftest.py`**. Each app has one `tests.py` that builds its rows explicitly with
`objects.create()` and authenticates with `force_authenticate`, so a test says
in full what it needs.

```bash
# bash
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test
DJANGO_SETTINGS_MODULE=config.settings.development python manage.py test apps.inspections -v 2
```

```powershell
$env:DJANGO_SETTINGS_MODULE = 'config.settings.development'
python manage.py test
```

**Run the tests with the project interpreter, not whichever `python` is on your
PATH.** The suite is verified against the versions in `requirements.txt`
(Django 4.2.30, DRF 3.17.2) and the platform deploys 4.2.x, so a machine that
happens to resolve `python` to a different interpreter is running the suite
against a Django nobody deploys. Say `.venv/Scripts/python.exe` (Windows) or
`.venv/bin/python` explicitly if you are not sure.

Two conventions worth knowing before you add a test:

- **Storage is hermetic.** Tests that touch a `FileField` override `STORAGES`
  and `MEDIA_ROOT` to a `tempfile.mkdtemp()` at module scope (see
  `apps/scans/tests.py`), so nothing is written into the working tree.
- **Helpers that live in the top-level `common/` package are tested in
  `apps/common/tests.py`.** `common/` is a plain Python package, not an
  installed app, so Django's discovery never reaches it.

Before opening a PR:

```bash
python manage.py makemigrations --check --dry-run   # must print "No changes detected"
python manage.py check
```

## Configuration

Everything is environment-driven; `config/settings/base.py` reads it all in one
place. The settings that change *behaviour* rather than wiring:

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` | Which settings module `manage.py` loads |
| `DJANGO_SECRET_KEY` | *(required in production)* | Signing key |
| `DJANGO_ALLOWED_HOSTS` | `*` in dev, **required** in production | Comma-separated hostnames |
| `DJANGO_DEBUG` | `True` in dev, `False` in production | Debug mode |
| `ENABLE_GIS` | `False` | Switches the DB engine to PostGIS and enables spatial fields |
| `DATABASE_URL` / `DATABASE_*` | SQLite if unset | PostgreSQL connection |
| `SECURE_SSL_REDIRECT` | `True` | Force HTTPS |
| `CORS_ALLOWED_ORIGINS` | `FRONTEND_URL` | Extra allowed browser origins |
| `STORAGE_PROVIDER` | *(local)* | `cloudflare_r2` or `cloudinary` for media |
| `CELERY_BROKER_URL` | *(unset)* | Enables Celery; unset means tasks run eagerly |
| `DEFAULT_GEOFENCE_RADIUS_M` | `50` | Platform fallback radius — **not** a recorded project attribute |
| `GEOFENCE_ENFORCEMENT` | `warn` | `off` \| `warn` \| `strict` — see below |
| `SYNC_MAX_RETRIES` | `5` | Failures before a queue item is reported `exhausted` |
| `SYNC_CLAIM_TIMEOUT_SECONDS` | `300` | How long a dead worker's claim blocks another |
| `IMPORT_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Upload ceiling |
| `IMPORT_MAX_ROWS` | `20000` | Row ceiling; refused, never silently truncated |
| `IMPORT_MAX_ERRORS` | `200` | Errors returned per validation, with `errors_truncated` |
| `EVIDENCE_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Evidence upload ceiling |
| `EVIDENCE_VERIFY_MAX_BYTES` | `26214400` (25 MiB) | Above this, verification reports `null` rather than re-reading |

Integration credentials (`RESEND_API_KEY`, `CLOUDFLARE_R2_*`, `TRIMBLE_*`,
`GOOGLE_MEETING_*`, `GOOGLE_MAPS_API_KEY`) live in the same env file. `.env` and
`new_info.txt` are gitignored — keep them that way.

## Management commands

| Command | Purpose |
| :--- | :--- |
| `seed_rbac` | Creates the platform's roles and permissions. Idempotent; run after every migrate on a fresh database. |
| `audit_project_scope_escalation` | **Read-only.** Reports which projects each inspector can reach, and which matched more than one user. Run before any change to project scoping. |
| `backfill_project_assigned_inspector_user` | Dry-run by default. Resolves each project's `assigned_inspector` text to a real user. Refuses to write when it would lose or orphan assignments unless `--force`. |
| `purge_example_template_data` | Removes rows that were seeded as examples. |
| `purge_fabricated_demo_data` | Removes fabricated demo rows. |
| `backfill_evidence_confidence` | Populates missing evidence confidence from the source record. |
| `cold_store_inactive_projects` | Archives inactive projects. |
| `generate_compliance_checks` | Materialises compliance checks for projects. |

---

## API contracts

### The response envelope

Every DRF response is wrapped:

```json
{ "success": true, "message": "...", "data": { }, "errors": null }
```

Errors invert it — `success: false`, `data: null`, and `errors` keyed by field
with a list of messages. A validation failure is always `400` with that shape,
never a bare string.

### Absent states, per endpoint

Three distinct absences, and they are never collapsed into one another:

| Value | Means |
| :--- | :--- |
| `null` | The fact is not recorded, or does not apply. `false` would be a claim. |
| `''` / `[]` / `{}` | The collection is genuinely empty — nothing has been recorded yet. |
| `404` with a `detail` | The resource does not exist for this caller, including "does not exist yet". |

A nullable boolean is load-bearing wherever it appears: `last_verify_ok: null`
is "no verification has run", which is a different report from
`last_verify_ok: false` ("verification ran and failed"). A client that renders
the two the same way is showing "failed" for "not checked yet".

`GET /api/v1/government/inspectors/me/` returns **404**, not an empty object,
when no accreditation is recorded — an absent badge must not render as a blank
one. The same rule applies to `agency`, `district` and `role` on the inspector
dashboard: they are `null` until recorded, never a default string.

### Telemetry sessions

`/api/v1/telemetry/` — `session/start/`, `session/<uuid:session_id>/data/`,
`session/<uuid:session_id>/end/`, `session/<uuid:session_id>/status/`,
`sessions/`, `devices/`.

A session has **two independent status axes**, and conflating them is the
mistake this design exists to prevent:

- **`status`** — `OPEN` | `ENDED` | `ABORTED`. Is the device still streaming?
- **`sync_status`** — `PENDING` | `SYNCED` | `FAILED`. Have the packets reached
  the statutory registry?

`OPEN` + `PENDING` is the only combination a live device has. `ENDED` +
`PENDING` means capture finished and promotion has not been attempted;
`ENDED` + `FAILED` means promotion was attempted and rejected, and
`sync_error` says why. There is no "SYNCED to a server" state, because
**telemetry is not pushed anywhere** — `SYNCED` means the packets were
**promoted into the platform's own statutory registries** (`digital_eye.GPRSurvey`,
`digital_eye.PUNDITTest`, `scans.ScanSession`, `digital_eye.GnssSurvey`, …) and
the corresponding `EvidenceRecord` rows were written.

Promotion is all-or-nothing. One invalid row fails the whole session with
`sync_status='FAILED'` and **nothing written** — a partly-promoted survey is a
registry that disagrees with itself. The session's `data_payload` is the
normalised envelope built at `/end`; `sha256_hash` attests it.

Fields that are absent on purpose:

| Field | Absent when |
| :--- | :--- |
| `session_start` | The device reported no clock. It is **not** backfilled with the row's `created_at` — a fabricated start time is a fabricated measurement. |
| `session_end` | The session is still open. Nothing ended it, so nothing may claim it did. |
| `chain_valid` | `null` when `packet_count == 0`. An empty chain proves nothing, and reporting it intact is a claim about content that does not exist. |
| `promoted` | `null` when nothing was promoted — never `{}`, which would read "promoted, nothing found". |
| `open_session_reference` (in `devices/`) | The device is idle. That is a fact about the device, not a failure to look it up. |

Appending a packet returns the `chain_hash` the *next* packet must name as its
predecessor. A client that loses that response re-reads `status/` rather than
guessing a sequence number.

### Manual import

`/api/v1/import/` — `upload/`, `record-types/`, `batches/`,
`templates/<str:record_type>/`, `<uuid:batch_id>/validate/`,
`<uuid:batch_id>/commit/`, `<uuid:batch_id>/status/`.

Both spellings are registered with and without a trailing slash: the client's
spec lists them without, and `APPEND_SLASH` only rescues a GET, so a POST to the
spec's exact spelling would 404 in production.

Three stages, and each does exactly one thing:

**1. Upload** stores the bytes and attests them. It does **not** parse. The
hash is computed in the same pass that writes the file, so it attests the bytes
that exist rather than the bytes the client says it sent. A client-supplied
`sha256` or `file_size_bytes` is checked against the stored file and a mismatch
refuses the upload naming both values. Re-uploading identical bytes for the same
inspector and project returns the existing `PENDING`/`FAILED` batch with
`deduplicated: true`; an `IMPORTED` batch is never reused.

**2. Validate** parses every row with **no early exit** — the inspector sees
every problem in one pass — and validates each row through *the same serializer
the manual create endpoint uses*, so validation and persistence can never
disagree. It replaces rather than appends its `ImportRecord` rows, so running it
twice is idempotent.

> **The 9-valid-1-invalid rule.** A batch with nine good rows and one bad row is
> `FAILED` with `valid_record_count=9`. There is no partial import: all-or-nothing
> is the invariant, and the counts exist so the inspector can see how close the
> file was. `errors` is capped at `IMPORT_MAX_ERRORS`; when capped,
> `errors_truncated` is `true` so nobody mistakes "these are the problems" for
> "these are the first 200 of them". `record_count` includes rows rejected before
> a record could be formed, so `valid + invalid` always equals the rows the
> inspector can count in their own file.

**3. Commit** re-validates inside the transaction — closing the window where a
serializer rule changed between validate and commit — then writes the rows
**and** `import_status='IMPORTED'` inside the *same* `transaction.atomic()`
block. That single block is the most important line in the pipeline: writing the
status outside it would let a rollback leave a batch claiming `IMPORTED` with
zero rows in the registry. Committing twice is a `409`.

The lifecycle is strictly forward — `PENDING → VALIDATED → IMPORTED`, with
`FAILED` reachable from `PENDING` or `VALIDATED`. Nothing leaves `IMPORTED`.

`import_type` is detected **from the file's bytes**, not the filename or the
client's claim. A PDF is accepted at upload and then fails validation with a
message explaining that a PDF import needs a column contract agreed with the
client — a heuristic table extraction that mapped the wrong column to transit
time would silently corrupt a statutory registry, so it is refused rather than
guessed at.

`raw_data` and `record_data` are both stored: "the file said transit time 42.1"
and "we read that as 42.1 µs for point B" are different claims, and when a
column is misread the pair is what shows whether the file was wrong or the
parser was.

### Offline sync queue

`/api/v1/sync/` — `queue/`, `process/`, `status/`.

**The idempotency contract**, in full:

| Situation | Result |
| :--- | :--- |
| New `client_item_id` | Queued. |
| Same `client_item_id`, **same payload hash** | `deduplicated: true`. A retry, not a second write. |
| Same `client_item_id`, **different payload hash** | **`409`, stored payload untouched.** |
| Item already `SYNCED`, replayed | Target row count unchanged — idempotency is enforced at the target as well as the queue. |
| Item past `SYNC_MAX_RETRIES` | Still in the queue, still in `status/` with `exhausted: true`. |

`client_item_id` is unique **per inspector**, so one client cannot collide with
or overwrite another's queued work. `payload_hash` is computed server-side from
the canonical payload and never accepted from a client — a client-supplied hash
would let a replayed request declare itself identical to a different payload.
The 409 case is the one worth being loud about: quietly accepting it is how a
replay becomes data loss.

The queue replays **oldest first** (`ordering = ['queued_at', 'id']`). `id` is a
monotonic integer rather than a UUID precisely so that items captured in the
same offline burst, sharing a timestamp to the millisecond, still replay in
capture order — an `UPDATE` replayed before its `CREATE` is a corruption, not a
hiccup.

`POST process/` is **synchronous**: the PWA needs the answer at the moment it
reconnects. It claims each item with a conditional
`UPDATE … WHERE sync_status IN ('PENDING','FAILED')` and checks the rowcount —
without that, two concurrent calls double-apply the same item. A claim left
behind by a worker that died is reclaimable after `SYNC_CLAIM_TIMEOUT_SECONDS`;
otherwise that item would be stuck forever, which is the same data loss as
dropping it, only quieter.

`last_error` is a message, never a stack trace — it is returned to a mobile
client. `GET status/` returns per-status counts, `oldest_pending_at`, and
`last_synced_at = max(synced_at) or null`. `last_synced_at` is **null until
something has actually synced**; it is not the time of the request.

Actions a given `entity_type` cannot perform are refused with `400` **at
enqueue** — an Inspector issuing `DELETE` fails there and then. A queue that
accepts work it can never do is a trap.

### Geofence enforcement

`POST /api/v1/inspections/<uuid:id>/execution/checkin/` measures the distance
from the inspector's reported position to the project's recorded coordinates and
records the result. `POST …/execution/checkout/` closes the visit. Neither
changes `inspection.status`: the spec records no transition, and inventing one
would be fabrication.

`gps_verified` is `true` **only** when all three hold — the project has
coordinates recorded, the point is within the effective radius, and the reported
accuracy does not exceed that radius. Everything else is `false` with a
`geofence_reason`:

| `geofence_reason` | Meaning |
| :--- | :--- |
| `WITHIN_RADIUS` | Verified. |
| `OUTSIDE_RADIUS` | Measured, and outside. `geofence_distance_m` carries the distance. |
| `PROJECT_COORDINATES_NOT_RECORDED` | The project has no site coordinates. Unverifiable, not failed. |
| `ACCURACY_NOT_REPORTED` | The device sent no accuracy. Treated as unverifiable — an unquantified fix cannot be checked against a 50 m radius. |
| `DEVICE_ACCURACY_EXCEEDS_RADIUS` | A ±200 m fix inside a 50 m radius is a false attestation. |
| `''` (empty) | Never evaluated. `geofence_state` is `''`, never a default of `WITHIN`. |

`radius_source` is returned on every check and is either `'project'` or
`'platform_default'`. `DEFAULT_GEOFENCE_RADIUS_M` is a platform *policy*
fallback, not a recorded attribute — `Project.geofence_radius_m` is nullable
precisely so that no existing row claims a geofence nobody entered, and the UI
can never present the platform default as though the project had set it.

**`GEOFENCE_ENFORCEMENT` is the rollout switch:**

| Value | Behaviour |
| :--- | :--- |
| `off` | Nothing is evaluated; `gps_verified` is always `false`. |
| `warn` | **Default.** Measure, record, return the distance — but allow check-in. Ships before every project's site coordinates are backfilled, without the flag claiming more than it knows. |
| `strict` | Refuse a check-in that is outside the radius *or* that cannot be verified at all. Flip to this once the coordinate backfill is complete. |

Fields on `Inspection` that are `null`/`''` mean the visit never reached that
step: `check_out_time` absent means the inspector has not checked out,
`geofence_state == ''` means no evaluation ever ran, and `gps_verified=false`
with an empty `geofence_state` on a legacy row means the old code asserted
verification it never performed. Those rows are not trusted;
`manage.py` does not silently rewrite them.

### File evidence

`/api/v1/evidence/` — `upload/`, `<uuid:pk>/verify/`, `<uuid:pk>/`,
`inspection/<uuid:inspection_id>/`.

Verification re-reads the stored bytes and hashes them, which is the only way to
answer "is this still the file that was uploaded?". `file_bytes_ok` has three
states and they are all meaningful:

- `true` — the stored bytes match the recorded hash.
- `false` — they do not.
- `null` — no file is attached, or the file is larger than
  `EVIDENCE_VERIFY_MAX_BYTES`. Above the ceiling the endpoint reports the reason
  rather than claiming a `true` it did not check or a `false` it did not find.

`EvidenceRecord.file` is a separate `EvidenceFile` row rather than a `FileField`
on the record, because `compute_hash()` is defined over the JSON payload.
Putting bytes in the same row would make `/verify` ambiguous about what it
verified, and a record with no payload would hash `{}` and "verify" cleanly — a
false attestation.

`file_url` is `null` when the storage backend cannot produce one (a private
bucket with no signing configured). The file is still stored and still
verified; the honest answer to "where can I read it?" is that this deployment
has not been told how to hand it out.

### Project scoping

`common.permissions.scoped_projects(user)` is the single scoping helper — every
queryset that must not leak across projects goes through it. Superusers, state
HQ and agency heads see everything. An inspector sees the projects they are
**assigned to** — the `assigned_inspector_user` foreign key, or a project they
conducted a real inspection on — unioned with their district.

`Project.assigned_inspector` is a **display mirror** of that foreign key. It is
deliberately not matched as a fallback: matching the text would restore a
privilege escalation for every row the mirror had not caught up with, and an
inspector could grant themselves a project by typing their own name into it.
Access follows the user, which also means it does not move when a profile name
is edited.

Two commands exist around this and both are safe to run:

```bash
python manage.py audit_project_scope_escalation            # read-only
python manage.py backfill_project_assigned_inspector_user  # dry-run by default
```

The backfill resolves a name only when it matches **exactly one** active user —
by full name, falling back to email. A name two people share cannot say which of
them was meant, so it resolves to nobody and is reported for a human to decide.

## Documentation

- [`backend_frontend_system_architecture.md`](backend_frontend_system_architecture.md)
  — how the frontend portals map onto backend apps, the API data contracts, and
  a parity scorecard that states what is built and what is not.
- `GET /api/v1/schema/` — the OpenAPI document; `/api/v1/schema/swagger-ui/`
  renders it. It is generated, so it cannot drift from the code.
