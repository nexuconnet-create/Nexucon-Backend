"""
The field gateway: a folder watcher that pushes instrument exports.

An instrument with no radio leaves its capture as a file in a folder. That
folder is synced — OneDrive, Dropbox, a network share — and this service is
what closes the last metre: it notices a new export, sends it to
``telemetry/session/from-file/``, and the capture appears on the platform as a
reviewable, un-promoted session. Nobody opens an upload form.

**This module imports no Django models, on purpose.** It runs on a laptop in a
site office, under ``config.settings.gateway``, which carries no database
configuration and therefore no production credentials. If this file imported a
model, that stripped settings module could not load it and the laptop would
need the platform's database password to push a CSV. Everything here is
stdlib plus ``requests``; the platform is reached over HTTP like any other
client.

Four rules shape the behaviour, and each exists because of a specific way a
folder watcher goes wrong.

**A file is only sent once it has stopped changing.** A file appearing in a
synced folder is very often still being written — by the instrument, or by the
sync client materialising it. Sending it then produces a row-level rejection
that looks like a corrupt export when it is really just an early read. So a
candidate must hold the same size and modification time for
``settle_seconds`` before it is eligible. A file already older than that when
the gateway first sees it is adopted immediately, so a restart does not stall
on a backlog and ``--once`` is useful on a folder that already has files in
it. Windows cloud placeholders — a file the sync client has listed but not yet
downloaded — are skipped outright rather than adopted, because their size is
a fiction until they are fetched.

**At most once, keyed by content.** The ledger is a JSON file of SHA-256
digests that the platform has acknowledged. A digest is used rather than a
filename because a sync client re-downloading a file rewrites its name and its
mtime, and a gateway that keyed on those would cheerfully file the same
measurement twice. An entry is written only after the platform has confirmed
it holds those bytes. The ledger is one laptop's memory, not the platform's:
the server checks the same digest independently, so a lost or wiped ledger
costs a redundant upload, never a duplicate record.

**A refusal is not a retry.** A 4xx means the platform read the file and
rejected it — unknown column, no rows, no test type. The bytes will not import
next time either, so retrying every thirty seconds forever would only bury the
message. The refusal is recorded with its reason and the file is left alone,
which is also what makes the fix visible: the message names the column, the
operator records a mapping on the device, and ``--retry-rejected`` re-offers
the same bytes. A 5xx or a network failure is the opposite — the platform
never got a verdict — so the file stays queued and is tried again.

**Nothing here promotes anything.** The gateway ends at a session that is
ENDED and PENDING. Registry rows are written only at ``/end/``, by a person,
after review. A gateway that promoted would put an unreviewed parse straight
into a statutory record, which is the one thing the whole ingestion path is
built to prevent.
"""
import fnmatch
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Identifies the gateway to the platform, so its traffic is distinguishable
#: in a log from an inspector's browser.
USER_AGENT = 'nexucon-field-gateway/1.0'

#: Mirrors ``apps.telemetry.models.DEVICE_TOKEN_PREFIX``. Duplicated rather
#: than imported because importing the model is what this module must not do;
#: ``test_gateway.py`` asserts the two agree, so the copy cannot drift.
DEVICE_TOKEN_PREFIX = 'nxdev_'

#: The route this gateway pushes to, appended to ``api_url``.
SEND_PATH = '/api/v1/telemetry/session/from-file/'

#: Context fields the endpoint accepts alongside the file. Checked at config
#: load so a mistyped key in ``gateway.json`` is an error at startup rather
#: than a field that silently never arrives.
CONTEXT_FIELDS = (
    'test_type', 'structural_element', 'floor', 'test_location',
    'weather_condition', 'transducer_type',
)
INT_CONTEXT_FIELDS = ('transducer_frequency_khz',)

#: What the watcher treats as an export. Deliberately narrow: a folder that a
#: person also keeps notes in should not have its notes uploaded.
DEFAULT_PATTERNS = ('*.csv', '*.json')

#: Names a file carries while something is still writing it. A sync client
#: downloading a large export uses one of these, and none of them is a capture.
PARTIAL_SUFFIXES = ('.tmp', '.temp', '.part', '.partial', '.crdownload',
                    '.filepart', '.download')

#: Windows cloud-storage placeholder attributes. A file listed by OneDrive but
#: not yet downloaded reports its real size while reading returns nothing, so
#: it must never be adopted on the strength of its mtime.
_ATTRIB_OFFLINE = 0x1000
_ATTRIB_RECALL_ON_OPEN = 0x40000
_ATTRIB_RECALL_ON_DATA_ACCESS = 0x400000

#: Sweep outcomes, named so the command and the tests cannot disagree about
#: spelling.
SENT = 'sent'
DUPLICATE = 'duplicate'
WOULD_SEND = 'would-send'
REJECTED = 'rejected'
DEFERRED = 'deferred'
FAILED = 'failed'
SKIPPED = 'skipped'


class GatewayError(Exception):
    """The gateway cannot run as configured. The message is for the operator."""


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

@dataclass
class GatewayConfig:
    """One site's gateway, as read from its JSON file.

    ``device`` is the platform's UUID for the ``FieldDevice`` row, not the
    instrument's printed serial — the endpoint identifies a device by its
    primary key, and the credential it is authenticated with must belong to
    that same row. Sending the serial here produces a 400 that is easy to
    misread as "the gateway is broken".
    """

    api_url: str
    device_token: str
    device: str
    watch_dir: str
    ledger_path: str
    project: str = ''
    data_type: str = 'pundit'
    context: dict = field(default_factory=dict)
    patterns: tuple = DEFAULT_PATTERNS
    settle_seconds: float = 20.0
    interval_seconds: float = 30.0
    timeout_seconds: float = 120.0
    #: Whether the watched folder is allowed to be absent. False suits a
    #: synced folder that is always there, where a missing one means the path
    #: is wrong and the operator needs telling. True suits a folder that comes
    #: and goes — a USB stick, or a share that is not always mounted — where
    #: waiting is the whole point and stopping would be a false alarm. It is a
    #: declaration, not a guess: nothing infers this from the path.
    wait_for_watch_dir: bool = False

    @property
    def send_url(self):
        return self.api_url.rstrip('/') + SEND_PATH

    @classmethod
    def from_file(cls, path):
        """Read and validate ``gateway.json``.

        Every problem raises here, at startup, naming the key — a gateway
        that starts with a bad config and then does nothing is far worse than
        one that refuses to start, because the site believes it is working.
        """
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            raise GatewayError(f'No gateway config at {path}.')
        except json.JSONDecodeError as exc:
            raise GatewayError(
                f'{path} is not valid JSON: {exc.msg} (line {exc.lineno}). '
                f'Check for a trailing comma or an unquoted path — Windows '
                f'paths need their backslashes doubled.')

        if not isinstance(raw, dict):
            raise GatewayError(f'{path} must hold a JSON object.')

        for key in ('api_url', 'device_token', 'device', 'watch_dir'):
            if not str(raw.get(key) or '').strip():
                raise GatewayError(f'{path} is missing "{key}".')

        api_url = str(raw['api_url']).strip()
        if SEND_PATH.strip('/') in api_url:
            # An easy slip, and a confusing one: the doubled path 404s, and a
            # 404 is retried as a transient failure rather than reported as a
            # refusal, so the log fills with a reason that names the symptom
            # and not the cause.
            raise GatewayError(
                f'"api_url" must be the platform\'s address alone; the gateway '
                f'appends {SEND_PATH} itself. Got: {api_url!r}. Use just the '
                f'scheme and host — for example "https://api.nexucon.net".')

        device_token = str(raw['device_token']).strip()
        if not device_token.startswith(DEVICE_TOKEN_PREFIX):
            raise GatewayError(
                f'"device_token" must be a device credential issued by the '
                f'platform — it starts with "{DEVICE_TOKEN_PREFIX}". This is '
                f'not the instrument\'s serial number and not a login token.')

        device = str(raw['device']).strip()
        try:
            uuid.UUID(device)
        except ValueError:
            raise GatewayError(
                f'"device" must be the platform\'s UUID for this instrument, '
                f'not its printed serial. Open the device in Nexucon and copy '
                f'the id from the address bar. Got: {device!r}')

        project = str(raw.get('project') or '').strip()
        if project:
            try:
                uuid.UUID(project)
            except ValueError:
                raise GatewayError(f'"project" must be a UUID. Got: {project!r}')

        context = raw.get('context') or {}
        if not isinstance(context, dict):
            raise GatewayError('"context" must be an object of field: value.')
        unknown = sorted(set(context) - set(CONTEXT_FIELDS)
                         - set(INT_CONTEXT_FIELDS))
        if unknown:
            raise GatewayError(
                f'"context" has field(s) the platform does not accept: '
                f'{", ".join(unknown)}. Accepted: '
                f'{", ".join(CONTEXT_FIELDS + INT_CONTEXT_FIELDS)}.')

        watch_dir = os.path.abspath(os.path.expanduser(str(raw['watch_dir'])))
        ledger_path = raw.get('ledger_path')
        if ledger_path:
            ledger_path = os.path.abspath(os.path.expanduser(str(ledger_path)))
        else:
            # Beside the config, not inside the watched folder — a ledger in
            # the watched folder is one sync client away from being uploaded,
            # and it would then be re-downloaded to every machine on the share.
            ledger_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                                       'gateway-ledger.json')

        patterns = raw.get('patterns') or DEFAULT_PATTERNS
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, (list, tuple)) or not patterns:
            raise GatewayError('"patterns" must be a non-empty list of globs.')

        wait_for_watch_dir = raw.get('wait_for_watch_dir', False)
        if not isinstance(wait_for_watch_dir, bool):
            raise GatewayError(
                f'"wait_for_watch_dir" must be true or false, not '
                f'{wait_for_watch_dir!r}. Set it to true when the watched '
                f'folder comes and goes — a USB stick, or a network share '
                f'that is not always mounted — so the gateway waits for it '
                f'instead of stopping.')

        return cls(
            api_url=str(raw['api_url']).strip(),
            device_token=device_token,
            device=device,
            watch_dir=watch_dir,
            ledger_path=ledger_path,
            project=project,
            data_type=str(raw.get('data_type') or 'pundit').strip(),
            context={str(k): v for k, v in context.items()},
            patterns=tuple(str(p) for p in patterns),
            settle_seconds=_positive(raw, 'settle_seconds', 20.0),
            interval_seconds=_positive(raw, 'interval_seconds', 30.0),
            timeout_seconds=_positive(raw, 'timeout_seconds', 120.0),
            wait_for_watch_dir=wait_for_watch_dir,
        )

    def context_form_fields(self):
        """The context, as the form fields the endpoint expects."""
        out = {}
        for key in CONTEXT_FIELDS:
            value = self.context.get(key)
            if value is not None and str(value).strip():
                out[key] = str(value).strip()
        for key in INT_CONTEXT_FIELDS:
            value = self.context.get(key)
            if value is not None and str(value).strip():
                out[key] = str(value).strip()
        return out


def _positive(raw, key, default):
    """A seconds value that must be a positive number when present."""
    if key not in raw or raw[key] is None:
        return default
    try:
        value = float(raw[key])
    except (TypeError, ValueError):
        raise GatewayError(f'"{key}" must be a number of seconds.')
    if value <= 0:
        raise GatewayError(f'"{key}" must be greater than zero.')
    return value


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------

class Ledger:
    """Which bytes the platform has already confirmed it holds.

    Two sections, because the two outcomes mean different things. ``sent`` is
    finished business and is never revisited. ``rejected`` is a refusal the
    platform gave a verdict on: retrying is pointless, but the reason is kept
    so the operator can see what to fix, and ``--retry-rejected`` clears them
    once it has been fixed.
    """

    VERSION = 1

    def __init__(self, path):
        self.path = path
        self.sent = {}
        self.rejected = {}

    @classmethod
    def load(cls, path):
        ledger = cls(path)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return ledger
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt ledger is not recoverable by guessing. Refusing to run
            # is the safe direction: the alternative is an empty ledger that
            # silently re-sends every file in the folder.
            raise GatewayError(
                f'The ledger at {path} could not be read ({exc}). Move it '
                f'aside to start a fresh one — the platform will refuse any '
                f'file it already holds, so nothing is recorded twice.')
        if isinstance(data, dict):
            ledger.sent = data.get('sent') or {}
            ledger.rejected = data.get('rejected') or {}
        return ledger

    def verify_writable(self):
        """Prove the ledger can be written, before anything is sent.

        ``save()`` writes a temporary file in the ledger's directory and then
        replaces the ledger with it, so being able to create that temporary
        file is exactly the capability a later save needs. ``os.replace`` asks
        nothing further: it consults the directory's permissions, not the
        target file's.

        This exists because the failure it prevents is far nastier than its
        cause. Without it, a gateway whose ledger cannot be written starts
        cleanly, sends its first file successfully — the capture really is on
        the platform — and only then finds it cannot record that it did. Under
        a supervisor set to restart it, that is a container or a scheduled task
        looping forever over one file, and the last line of the log is about
        the ledger rather than about the send that worked. The same class of
        problem the config validation refuses at startup, so it is refused
        here too.
        """
        directory = os.path.dirname(self.path) or '.'
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                'w', encoding='utf-8', dir=directory, prefix='.ledger-',
                suffix='.tmp', delete=False)
            handle.close()
            os.unlink(handle.name)
        except OSError as exc:
            raise GatewayError(
                f'The ledger at {self.path} could not be written ({exc}). The '
                f'gateway will not start, because a gateway that sends without '
                f'recording what it sent re-sends its whole folder on every '
                f'restart. Point "ledger_path" at a directory this process can '
                f'write to.')

    def knows(self, digest):
        """Has the platform confirmed these exact bytes?"""
        return digest in self.sent

    def rejection(self, digest):
        """The platform's refusal for these bytes, if it gave one."""
        return self.rejected.get(digest)

    def record_sent(self, digest, *, name, session_reference, status):
        self.sent[digest] = {
            'name': name,
            'at': _now_iso(),
            'session_reference': session_reference or '',
            'status': status,
        }
        # A file that has now been accepted is no longer a refusal, whatever
        # an earlier attempt said.
        self.rejected.pop(digest, None)

    def record_rejected(self, digest, *, name, status, detail):
        self.rejected[digest] = {
            'name': name,
            'at': _now_iso(),
            'status': status,
            'detail': detail,
        }

    def clear_rejected(self):
        """Forget every refusal, so those files are offered again."""
        count = len(self.rejected)
        self.rejected = {}
        return count

    def save(self):
        """Write the ledger atomically.

        Through a temporary file and ``os.replace``, because a laptop closed
        mid-write must not leave a truncated ledger: a JSON fragment would
        either fail to parse on the next start — stopping the gateway
        entirely — or, worse, lose the record of a file that was already sent.
        """
        payload = {
            'version': self.VERSION,
            'updated_at': _now_iso(),
            'sent': self.sent,
            'rejected': self.rejected,
        }
        directory = os.path.dirname(self.path) or '.'
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                'w', encoding='utf-8', dir=directory, prefix='.ledger-',
                suffix='.tmp', delete=False)
            try:
                with handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                _quiet_remove(handle.name)
                raise
        except OSError as exc:
            raise GatewayError(
                f'The ledger at {self.path} could not be written ({exc}). '
                f'The gateway will not send anything, because it could not '
                f'record what it had sent.')


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ----------------------------------------------------------------------
# Sweeping
# ----------------------------------------------------------------------

@dataclass
class SweepResult:
    """What happened to one candidate file in one sweep."""

    path: str
    action: str
    detail: str = ''
    status: int = None


class GatewayRunner:
    """Watches a folder and pushes what settles in it.

    The transport is injectable so the sweep logic — the settle rule, the
    ledger, the retry policy — can be tested without a server, which is where
    the interesting failures live.
    """

    def __init__(self, config, transport=None, dry_run=False):
        self.config = config
        self.dry_run = dry_run
        self.ledger = Ledger.load(config.ledger_path)
        # Before any file is sent, not when the first one has been. See
        # Ledger.verify_writable — discovering this after a successful send is
        # the one ordering that loses the record of a capture that arrived.
        self.ledger.verify_writable()
        self._transport = transport or _post_export
        #: path → (size, mtime, first_observed_at, settled). Populated by the
        #: sweep that first sees a file and used to prove it has stopped
        #: changing.
        self._observed = {}
        #: True when the watched folder was absent at the last look. Only
        #: reachable when ``wait_for_watch_dir`` is set; kept as state so the
        #: absence is logged on the transition rather than every sweep.
        self.watch_dir_missing = False

    # -- one pass ------------------------------------------------------

    def sweep(self, now=None):
        """Look once. Returns a ``SweepResult`` per candidate file."""
        now = time.time() if now is None else now
        results = []
        for path in self._candidates():
            results.append(self._consider(path, now))
        # Forget files that have gone, so a long-running gateway does not
        # accumulate an entry per export a whole project's worth of work.
        self._observed = {p: v for p, v in self._observed.items()
                          if os.path.exists(p)}
        return results

    def _consider(self, path, now):
        name = os.path.basename(path)

        try:
            stat = os.stat(path)
        except OSError as exc:
            return SweepResult(path, SKIPPED, f'could not be read: {exc}')

        if _is_cloud_placeholder(stat):
            return SweepResult(
                path, DEFERRED,
                'not downloaded from cloud storage yet')

        if stat.st_size == 0:
            # A zero-byte file is a sync placeholder or an export that failed
            # to write. Neither is a capture, and sending it would produce a
            # rejection about an empty file that hides the real problem.
            return SweepResult(path, SKIPPED, 'empty file')

        settled, why = self._settled(path, stat, now)
        if not settled:
            return SweepResult(path, DEFERRED, why)

        try:
            content = _read_bytes(path, stat)
        except OSError as exc:
            return SweepResult(path, SKIPPED, f'could not be read: {exc}')

        digest = hashlib.sha256(content).hexdigest()

        if self.ledger.knows(digest):
            entry = self.ledger.sent.get(digest) or {}
            return SweepResult(
                path, SKIPPED,
                f'already sent as {entry.get("session_reference") or "a session"}')

        rejection = self.ledger.rejection(digest)
        if rejection:
            return SweepResult(
                path, REJECTED,
                f'{rejection.get("detail", "refused earlier")} '
                f'(refused {rejection.get("at", "earlier")}; fix it and run '
                f'with --retry-rejected to offer this file again)')

        if self.dry_run:
            # The real ledger was consulted, so this answers the question an
            # operator actually has — "what would a real run do right now?" —
            # and nothing is sent or written.
            return SweepResult(
                path, WOULD_SEND,
                f'{len(content)} bytes, sha256 {digest[:12]}…')

        return self._send(path, name, content, digest)

    def _settled(self, path, stat, now):
        """``(is_settled, why_not)`` for one file.

        The rule is stability, not age: a file is ready when its size and
        modification time have not moved for ``settle_seconds``. A file the
        gateway has never seen before is adopted outright *if* it is already
        older than that, which is what makes ``--once`` and a restart useful
        instead of merely patient.

        Once a file has been judged settled it *stays* settled until it
        changes. Re-deciding on every sweep would be wrong in a way that is
        easy to miss: a file refused by the platform, or left queued by a 5xx,
        would go quiet for a fresh window after every attempt — so a file
        needing attention would report "still being written" and a file
        already on the platform would report the same instead of "already
        sent". The verdict belongs to the bytes, not to the sweep count.
        """
        key = (stat.st_size, stat.st_mtime)
        previous = self._observed.get(path)

        if previous is None:
            age = now - stat.st_mtime
            adopted = age >= self.config.settle_seconds
            self._observed[path] = (stat.st_size, stat.st_mtime, now, adopted)
            if adopted:
                return True, ''
            return False, (f'waiting for it to stop changing '
                           f'({age:.0f}s old, needs '
                           f'{self.config.settle_seconds:.0f}s)')

        if (previous[0], previous[1]) != key:
            # Something wrote to it since the last look. The clock restarts,
            # and last sweep's verdict with it.
            self._observed[path] = (stat.st_size, stat.st_mtime, now, False)
            return False, 'still being written'

        if previous[3]:
            return True, ''

        stable_for = now - previous[2]
        if stable_for < self.config.settle_seconds:
            return False, (f'waiting for it to stop changing '
                           f'({stable_for:.0f}s of '
                           f'{self.config.settle_seconds:.0f}s)')
        self._observed[path] = (stat.st_size, stat.st_mtime, previous[2], True)
        return True, ''

    def _send(self, path, name, content, digest):
        fields = {
            'device': self.config.device,
            'data_type': self.config.data_type,
        }
        if self.config.project:
            fields['project'] = self.config.project
        fields.update(self.config.context_form_fields())

        try:
            response = self._transport(
                url=self.config.send_url,
                token=self.config.device_token,
                filename=name,
                content=content,
                fields=fields,
                timeout=self.config.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — any transport failure is transient
            # Deliberately not ledgered: the platform never gave a verdict, so
            # the file must be tried again rather than quietly dropped.
            logger.warning('gateway: %s not sent (%s)', name, exc)
            return SweepResult(path, FAILED, f'could not reach the platform: {exc}')

        status = getattr(response, 'status_code', None)
        body = _response_json(response)
        detail = str(body.get('detail') or '').strip()

        if status in (200, 201):
            # 201: the platform created the session. 200: it already held these
            # exact bytes and answered with the session the first attempt made.
            # Both mean the file is in, which is the only thing the ledger
            # claims — so both are recorded, and a gateway whose response was
            # lost in transit stops retrying.
            reference = str(body.get('session_reference') or '')
            duplicate = bool((body.get('import_stats') or {}).get('duplicate'))
            self.ledger.record_sent(digest, name=name,
                                    session_reference=reference, status=status)
            self.ledger.save()
            if duplicate:
                return SweepResult(
                    path, DUPLICATE,
                    f'the platform already held these bytes as {reference}',
                    status=status)
            return SweepResult(
                path, SENT,
                f'imported as {reference}' if reference else 'imported',
                status=status)

        if status is not None and 400 <= status < 500 and status not in (408, 429):
            # A verdict. The bytes will not import next time either, so record
            # the reason and stop — the message is the fix instruction, and
            # retrying it every sweep would bury it under its own repetition.
            message = detail or _first_field_error(body) or f'HTTP {status}'
            self.ledger.record_rejected(digest, name=name, status=status,
                                        detail=message)
            self.ledger.save()
            logger.error('gateway: %s was refused by the platform: %s',
                         name, message)
            return SweepResult(path, REJECTED, message, status=status)

        # 5xx, 408, 429, or no status at all: the platform did not decide.
        logger.warning('gateway: %s not accepted (HTTP %s) %s',
                       name, status, detail)
        return SweepResult(
            path, FAILED,
            detail or f'the platform answered HTTP {status}; will retry',
            status=status)

    # -- candidates ----------------------------------------------------

    def _candidates(self):
        if not os.path.isdir(self.config.watch_dir):
            if not self.config.wait_for_watch_dir:
                raise GatewayError(
                    f'The watched folder {self.config.watch_dir} does not exist. '
                    f'Point "watch_dir" at the folder the instrument exports '
                    f'into.')
            self._note_watch_dir_gone()
            return []

        if self.watch_dir_missing:
            self.watch_dir_missing = False
            logger.info('gateway: %s is back; watching it again',
                        self.config.watch_dir)

        ledger_name = os.path.basename(self.config.ledger_path)
        found = []
        for name in sorted(os.listdir(self.config.watch_dir)):
            if name == ledger_name or name.startswith('.'):
                continue
            if name.lower().endswith(PARTIAL_SUFFIXES):
                continue
            lowered = name.lower()
            if not any(_matches(lowered, pattern) for pattern in self.config.patterns):
                continue
            path = os.path.join(self.config.watch_dir, name)
            if os.path.isfile(path):
                found.append(path)
        return found

    def _note_watch_dir_gone(self):
        """Record, once per absence, that the watched folder is not there.

        On the transition rather than every sweep: a gateway waiting for a USB
        stick to be plugged in would otherwise write the same warning every
        thirty seconds for a week, and a log nobody can read is not a log.
        """
        if not self.watch_dir_missing:
            self.watch_dir_missing = True
            logger.warning(
                'gateway: %s is not there at the moment; waiting for it to '
                'appear ("wait_for_watch_dir" is set)', self.config.watch_dir)


def _matches(lowered_name, pattern):
    return fnmatch.fnmatch(lowered_name, str(pattern).lower())


def _is_cloud_placeholder(stat):
    """Is this a file the cloud client has listed but not downloaded?

    OneDrive and its kin report the real size for a placeholder while reading
    returns nothing. Without this check such a file looks perfectly settled —
    old mtime, plausible size — and would be sent as an empty upload.
    """
    attributes = getattr(stat, 'st_file_attributes', 0)
    return bool(attributes & (_ATTRIB_OFFLINE | _ATTRIB_RECALL_ON_OPEN
                              | _ATTRIB_RECALL_ON_DATA_ACCESS))


def _read_bytes(path, stat):
    """Read the file, refusing if it changed since it was measured."""
    with open(path, 'rb') as handle:
        content = handle.read()
    if len(content) != stat.st_size:
        # Written to between the settle check and the read. Refusing is the
        # point: the alternative is uploading a half-written capture and
        # calling it a corrupt export.
        raise OSError('the file changed while it was being read')
    return content


def _response_json(response):
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
        return {}
    return body if isinstance(body, dict) else {}


def _first_field_error(body):
    """DRF's per-field errors, flattened into one sentence for the log."""
    for key, value in body.items():
        if key == 'detail':
            continue
        if isinstance(value, (list, tuple)) and value:
            return f'{key}: {value[0]}'
        if isinstance(value, str):
            return f'{key}: {value}'
    return ''


def _post_export(*, url, token, filename, content, fields, timeout):
    """The one place ``requests`` is touched.

    Imported here rather than at module scope so the whole sweep logic — the
    settle rule, the ledger, the retry policy — is importable and testable
    without the HTTP client present.
    """
    import requests

    return requests.post(
        url,
        headers={
            'Authorization': f'Device {token}',
            'User-Agent': USER_AGENT,
        },
        files={'file': (filename, content, _content_type(filename))},
        data=fields,
        timeout=timeout,
    )


def _content_type(filename):
    lowered = filename.lower()
    if lowered.endswith('.json'):
        return 'application/json'
    if lowered.endswith('.csv'):
        return 'text/csv'
    return 'application/octet-stream'


# ----------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------

def run(config, *, once=False, dry_run=False, retry_rejected=False,
        on_result=None, on_sweep=None):
    """Sweep once, or sweep every ``interval_seconds`` until interrupted.

    ``dry_run`` reports what a real run would do and does none of it: nothing
    is sent and nothing is written to the ledger, so a dry run cannot make the
    real run skip work it has not actually done.

    ``on_sweep`` is called with the runner after each pass. It exists so the
    command can report state that belongs to the sweep rather than to any one
    file — at present, whether the watched folder was there at all.
    """
    runner = GatewayRunner(config, dry_run=dry_run)
    report = on_result or _log_result

    if retry_rejected:
        cleared = runner.ledger.clear_rejected()
        if cleared:
            runner.ledger.save()
            logger.info('gateway: %s previously refused file(s) will be '
                        'offered again', cleared)

    while True:
        for result in runner.sweep():
            report(result)

        if on_sweep is not None:
            on_sweep(runner)

        if once:
            return
        _sleep(config.interval_seconds)


def _log_result(result):
    name = os.path.basename(result.path)
    if result.action == SENT:
        logger.info('gateway: %s %s', name, result.detail)
    elif result.action == DUPLICATE:
        logger.info('gateway: %s already on the platform — %s', name, result.detail)
    elif result.action == WOULD_SEND:
        logger.info('gateway: %s would be sent (%s)', name, result.detail)
    elif result.action == REJECTED:
        logger.error('gateway: %s REFUSED — %s', name, result.detail)
    elif result.action == FAILED:
        logger.warning('gateway: %s %s', name, result.detail)
    else:
        logger.info('gateway: %s %s (%s)', name, result.action, result.detail)


def _sleep(seconds):
    time.sleep(seconds)
