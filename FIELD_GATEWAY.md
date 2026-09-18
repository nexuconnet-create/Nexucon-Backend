# The field gateway

Getting a PUNDIT capture onto the platform without anyone opening an upload
form.

The instrument exports as it always does — into a folder. That folder is
synced (OneDrive, Dropbox, a network share, or simply a folder on the site
machine). This gateway watches that folder, notices a new export once it has
stopped changing, and sends it to the platform. The capture then appears in
the inspector app as a closed, **un-promoted** session, waiting to be reviewed.

Nothing reaches the registry on its own. Promotion is still a person pressing
**Promote** after looking at what was parsed — see *What this deliberately does
not do* below.

---

## The instrument: Proceq Pundit PL-200

Confirmed with the client on 18 Sep 2026. What follows is from Proceq's
operating instructions for the unit — not from having handled one, so the
things marked **not yet seen** are exactly that.

**It has no radio.** No Bluetooth, no Wi-Fi, no cloud upload. The touchscreen
unit offers a USB host port (mouse, keyboard, a USB stick), a USB device port
(probes and a PC), and Ethernet — and the Ethernet port is for firmware updates
only. So of the three transports originally asked about, the PL-200 has none of
them. **This gateway is not a workaround for a missing radio; for this
instrument it is the only automatic step that exists.**

**Two ways a measurement can leave the unit**, and which one applies decides how
much of the chain is automatic:

| Route | How | Cost |
|---|---|---|
| **PL-Link → CSV** | USB cable to a PC, PL-Link software, select the object, *Export as CSV file(s)*, save into this watch folder. | The platform reads CSV for certain. But **a person clicks Export every time** — the instructions describe only that one interactive dialog, with no batch, script or auto-export mode. |
| **Straight to a USB stick** | Put a stick in the unit's USB host port, tick the files, press Download. They arrive named `PM-…`. | No PC, and no clicking beyond Download. But those files are the instrument's own format, **and we have not yet seen one** — they may be something the platform cannot read. |

If the second route turns out to be readable, the site chain loses the PC and
PL-Link entirely: the stick goes into the site laptop and the gateway sends what
appears on it. Until a `PM-…` file has actually been opened and looked at,
assume neither.

> **If you take the USB-stick route, set `"wait_for_watch_dir": true`.**
> A stick is unplugged most of the time, so the folder is absent, and by
> default an absent folder stops the gateway. See *A folder that comes and
> goes* below for why that default is right everywhere else and dangerous
> here.

**We still have not seen a single real export**, by either route. Every column
name in the mapping example below is an illustration of the *shape* of a
mapping, not an observation of this instrument's output.

### Two things to check against the first real file

Both were found by reading the platform's importer against what the PL-200 is
documented to do. Neither can be settled without the file.

**1. Set the instrument to millimetres and microseconds before recording a
mapping, and check it stays that way.** The platform's contract carries the
unit *in the column name* — `PATH LENGTH L (MM)`, `TRANSIT TIME T (US)` — and
nothing downstream re-checks it. There is no unit column, and the number is
read as a plain float. So an instrument set to inches would have its values
stored as millimetres, silently, and the pulse velocity worked out from them
would be wrong by a factor of 25 — the headline number of the whole test,
wrong, with nothing on the record to show it happened. Confirm the setting on
the unit itself, and note it in the site's paperwork.

**2. A title or metadata block above the column headings will be refused.**
The importer treats **line 1 as the header row**. Instrument exports often put
a few lines of context above the table — object name, date, operator, serial —
and if PL-Link does, the platform will read *those* as the column names and
refuse the file for having columns it does not recognise, naming fields like
`object` or `pundit_pl_200`. The refusal is correct — it declines to guess —
but the message will not say the real cause, and **a column mapping cannot
rescue it**, because the mapping only renames columns the reader has already
found. If the first real export looks like this, that is a fix on the platform
side, not something to work around at the site.

Two related things that are already handled: only `STRUCTURAL ELEMENT` and
`TEST TYPE` are required, and both can come from the gateway's `context` block
for an instrument whose export carries neither; and the `PM-…` route above
does not go through any of this at all, since it is not CSV.

---

## What you need before you start

Three things, and you cannot skip any of them.

| | Where to get it |
|---|---|
| **The device's platform UUID** | Open the instrument's device record in Nexucon and copy the id. It looks like `4d1f…-…-…`. **This is not the serial number printed on the unit.** |
| **A device credential** | Issued by an officer for that instrument: `POST /api/v1/telemetry/device-tokens/` with the device's id and a label like `"site laptop"`. The response carries `token` — an `nxdev_…` string. **It is shown once and cannot be recovered**; if it is lost, issue another. |
| **The export folder** | The folder the instrument writes into, or the synced folder its exports land in. |

> **The credential and the device UUID must belong to the same instrument.**
> The platform refuses a credential that tries to push as a different device
> rather than silently correcting it — a gateway quietly filing one
> instrument's readings under another is the exact failure this check exists
> to catch.

---

## Setting it up

1. **Copy the repository to the site machine** and create its virtualenv:

   ```bat
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   No `.env` file is needed and none should be placed here. The gateway runs
   under `config.settings.gateway`, which has **no database configured** —
   this machine holds no credential to the registry, only a device token for
   its own instrument.

2. **Create `gateway.json`** beside `run_gateway.bat`:

   ```bat
   copy gateway.example.json gateway.json
   notepad gateway.json
   ```

   Fill in `api_url`, `device_token` and `device`, and point `watch_dir` at
   the export folder. Every other key is optional and explained in the
   example file.

   Keep this file out of version control — it holds the credential. The
   repository's `.gitignore` already excludes `*.json`, so a copy beside the
   repository root is covered; a copy placed anywhere else is not.

3. **Check it before trusting it.** From the repository folder:

   ```bat
   run_gateway.bat --once --dry-run
   ```

   This reports what a real run would send and sends nothing. Drop a test
   export into the watch folder first if it is empty, and confirm the gateway
   sees it:

   ```
   Gateway dry run: C:\Nexucon\exports
     pushing to https://api.nexucon.net/api/v1/telemetry/session/from-file/
     as device 4d1f…
     ledger C:\Nexucon\gateway-ledger.json
     would send site-export.csv — 412 bytes, sha256 9c1a4f2b8e07…
   Sweep complete: 1 would-send.
   ```

   If it says `waiting` instead, the file was written too recently — the
   gateway waits for a file to stop changing before sending it. Run it again
   after `settle_seconds` (20 by default).

4. **Run it for real, once**:

   ```bat
   run_gateway.bat --once
   ```

   ```
     sent      site-export.csv — imported as TMS-100B1226F2
   Sweep complete: 1 sent.
   ```

   Open the inspector app → **Sync → Telemetry**. The session is there, closed
   and un-promoted.

5. **Leave it running.** Either on the platform (below) or on a PC at the site
   (*Leaving it running on a site PC*).

---

## Running it on the platform

The gateway can run here, on the server, rather than on a PC at the site.
`docker-compose.yml` has a `gateway` service for it: nothing is installed at
the site, and no credential for the registry ever leaves the server.

What it does **not** remove is the network hop. The instrument has no radio,
so the export still has to get from the unit to `gateway-inbox/` on the VPS —
synced there by whatever the site already uses (rclone, OneDrive, a share).
**The gateway removes the upload form, not the journey.** A site with no
reliable link is still better served by the PC, and the two can run at once:
both push to the same endpoint and the platform deduplicates by content.

Three settings are specific to this shape, and each fails in its own way if
missed:

| Key | Value | If you get it wrong |
|---|---|---|
| `watch_dir` | `/inbox` — the container's view of `./gateway-inbox`, **not** a Windows path | The gateway waits or stops, depending on `wait_for_watch_dir`. |
| `ledger_path` | `/state/gateway-ledger.json` | It defaults to *beside the config*, and that directory is mounted read-only, so the gateway refuses to start. |
| `api_url` | `http://web:8000` — the compose service name, straight to gunicorn | See the `ALLOWED_HOSTS` note below. |

```json
{
  "api_url": "http://web:8000",
  "device_token": "nxdev_…",
  "device": "4d1f…",
  "watch_dir": "/inbox",
  "ledger_path": "/state/gateway-ledger.json"
}
```

`api_url` never leaves the host — it is the compose network, not the public
API. It relies on `web` being in the `web` service's `DJANGO_ALLOWED_HOSTS`.
The compose default includes it, but a `DJANGO_ALLOWED_HOSTS` set in `.env`
**replaces** that default, and if `web` is not in it every push is refused
with a 400 that says `Invalid HTTP_HOST header`.

Create `gateway-config/gateway.json` on the server, put it in `.gitignore`'s
shadow like every other `*.json`, then:

```bash
docker compose up -d --build
docker compose logs -f gateway
```

> **`--build` is not optional here.** Every other service bind-mounts the
> repository over `/app`, so a code change appears without rebuilding.
> `gateway` deliberately does not (see below), so it runs the code baked into
> the image — a `docker compose up -d gateway` after a `git pull` would
> restart it on the *old* code.

The log is `docker compose logs gateway`; there is no `logs\gateway.log`
here, because Docker already captures stdout. Nothing else changes — the
ledger, the settle rule and the refusals behave exactly as described below.

**Why it has no bind mount, unlike everything else.** Mounting `.:/app` is
what puts `.env` — and with it the database password — inside the container.
`config.settings.gateway` sets `DATABASES = {}`, so Django could not reach
the registry even holding one; not mounting it means the container does not
hold it either, so that settings module's claim stays true rather than merely
enforced. Its `SECRET_KEY` is set to a string of its own for the same reason:
this process serves no HTTP and signs nothing, so a compromise of this
container should not yield the platform's signing key.

**If the container restarts in a loop**, it is almost always the config: the
first line of the log says `No gateway config at /app/gateway-config/gateway.json.`
until that file exists.

---

## Leaving it running on a site PC (Task Scheduler)

Create **one** task. Task Scheduler → *Create Task* (not *Basic Task*, which
does not offer restart-on-failure):

**General**
- Run whether the user is logged on or not. The gateway must survive a
  logout.
- Tick **Run with highest privileges** only if the export folder needs it;
  otherwise leave it off.
- Tick **Hidden**.

**Triggers** → New → **At startup**. Add a second trigger, **At log on**, so a
machine that starts without a logon still works and vice versa.

**Actions** → New → *Start a program*, and fill in all three boxes:

| Box | Value |
|---|---|
| Program/script | `C:\Nexucon\run_gateway.bat` |
| Add arguments | *(leave empty)* |
| Start in | `C:\Nexucon` — the repository folder, the one containing `manage.py` |

> The **Start in** box is not optional. Task Scheduler starts tasks in
> `C:\Windows\System32`, and the gateway resolves its app paths relative to
> the working directory.

**Settings**
- Tick **If the task fails, restart every**: `1 minute`, up to `999` times.
- Untick **Stop the task if it runs longer than…** — this task is meant to run
  forever.
- Untick **Stop the task if the computer switches to battery power** if this
  is a laptop that might be unplugged.

Once a day, check `logs\gateway.log`. Delete it when it gets large; a site
that sends a few files a day will not need to for months.

---

## A folder that comes and goes

By default the gateway **stops** if `watch_dir` is not there. That is right for
the common case — a OneDrive folder or a permanent share is always present, so
an absent one means the path is wrong, and a gateway that runs quietly while
sending nothing is indistinguishable from a working one.

It is wrong for a folder that is legitimately absent. A USB stick is unplugged
most of the time, and a share is not always mounted. There, stopping is wrong
twice over: nothing is missed by waiting, and — because the task is set to
restart on failure — **the restart budget is finite.** At the documented
*restart every 1 minute, up to 999 times*, a machine left without its stick
spends the whole budget in about 17 hours and then the task is dead until
somebody notices.

So it is a declaration, not a guess:

```json
"watch_dir": "E:\\",
"wait_for_watch_dir": true
```

With `true`, an absent folder is waited for instead of fatal. It is reported
once per absence, and the summary line says the folder is not there rather than
claiming there was nothing to send — those are different claims and only one of
them is true.

Leave it `false` unless the folder really does come and go. Setting it `true`
on a mistyped path turns a loud failure into a silent one.

---

## Telling whether it is working

`logs\gateway.log` is append-only and every line is one of six things.

| Line | Means |
|---|---|
| `sent  export.csv — imported as TMS-…` | The capture is on the platform. |
| `already  export.csv — the platform already held these bytes as TMS-…` | A resend of something already imported. Nothing was filed twice. |
| `waiting  export.csv — 4s of 20s` | Normal. The file is still settling. |
| `retrying  export.csv — …` | The platform could not be reached or errored. It will be tried again on the next sweep. Nothing is lost. |
| `WARNING … is not there at the moment; waiting for it to appear` | Normal **only** if `wait_for_watch_dir` is set. The folder has gone (a stick was unplugged). Written once per absence, not once per sweep. |
| `REFUSED  export.csv — …` | **Someone has to act.** See below. |

On the platform side, `GET /api/v1/telemetry/sessions/` lists every session,
and every import writes an audit event (`telemetry.session.file_import`, or
`…_resend` for a duplicate).

---

## When a file is REFUSED

A refusal means the platform **read the file and would not import it**. The
bytes will not import next time either, so the gateway stops retrying and
reports the reason once per sweep instead of burying it. The message names the
fix.

The common case is a column the platform does not recognise:

```
REFUSED  raw.csv — raw.csv has column(s) this platform does not recognise:
distance_mm, location, time_us. Nothing was imported, because guessing which
column is which would risk recording a wrong value as a measurement. Accepted
columns are: STRUCTURAL ELEMENT, FLOOR, TEST TYPE, … This device has no column
mapping recorded. If this is the instrument's own column naming, record a
mapping on the device; otherwise send Nexucon the export itself and the layout
can be agreed.
```

This is the platform being honest rather than clever: it will not guess that
`Distance (mm)` means `PATH LENGTH L (MM)`, because a wrong guess would put a
wrong number into a statutory registry with nothing on the record to show it
happened.

**The fix is to tell it.** Record the instrument's own column names against
the device — a `PATCH` on the device record, or Django admin:

```
PATCH /api/v1/digital-eye/devices/<device-uuid>/
{
  "column_mapping": {
    "Location":      "point",
    "Distance (mm)": "path_length_l_mm",
    "Time (us)":     "transit_time_t_us"
  }
}
```

The keys are the instrument's headers **exactly as they appear in the file**;
the values are platform columns, and a wrong value is refused on the form
rather than failing later at a site. Then re-offer the same file:

```bat
run_gateway.bat --retry-rejected
```

The mapping lives on the platform, not in `gateway.json`, on purpose. It is
read on every import, so fixing it fixes it once — not on every laptop — and
the export file itself is stored exactly as the instrument wrote it, because
the retained bytes are the only thing that can settle a disputed parse.

**A mapping is never a guess.** A column the mapping does not name is still
refused by name. If a file carries both `Distance (mm)` and `PATH LENGTH L
(MM)`, the import is refused as ambiguous rather than picking one.

---

## What this deliberately does not do

- **It does not promote anything.** The gateway ends at a closed, un-promoted
  session. Registry rows are written only at `/end/`, by a person, after
  review. A gateway that promoted would put an unreviewed parse straight into
  a statutory record.
- **It does not rewrite the export.** The file is uploaded byte-for-byte as
  the instrument wrote it. All interpretation happens on the platform, where
  it is auditable and where a correction applies to every site at once.
- **It does not delete or move anything in the watch folder.** The gateway
  only reads. A file it has sent stays where it is, and is skipped on later
  sweeps by its content digest.
- **It does not need a radio.** There is no Bluetooth, Wi-Fi or vendor-cloud
  adapter here — and for the PL-200 there is nothing to adapt to, because the
  unit has none (see *The instrument* above). This layer is where such an
  adapter would slot in if a future instrument offers one; every instrument
  that can write an export file is served by this without the platform knowing
  its make or model.
- **It does not run as a Celery task.** `config/celery.py` declares a beat
  schedule, but no beat service is deployed, so nothing scheduled there has
  ever run. The gateway is a command on the site machine instead.

---

## The one thing that can go wrong quietly

The gateway's ledger (`gateway-ledger.json`) remembers which bytes have been
sent, keyed by SHA-256 — so a sync client re-downloading a file under a new
name does not get it uploaded twice. **It is a convenience, not the
guarantee.** The platform checks the same digest independently and answers a
resend with the session the first import created, which is why a laptop with a
deleted ledger re-uploads a file but never files it twice.

If the ledger is ever unreadable, the gateway **refuses to start** rather than
beginning with an empty one — starting empty would silently re-send every file
in the folder. The error says to move it aside; the platform will refuse
anything it already holds, so nothing is recorded twice.
