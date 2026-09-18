"""
Provisioning the field gateway from the platform.

An instrument whose exports should be pushed without anyone opening an upload
form needs one thing on the gateway: a config file naming the instrument, the
folder its exports land in, and a credential to push with. The credential is
minted here, so this is also where the config is written — because the
alternative is a person reading a secret out of one screen and typing it into
another, and that step is the one that gets skipped, done wrong, or leaked.

**The token is never returned, logged, displayed or stored.** It exists inside
``enable`` for the length of one function call and is written straight into the
file the gateway reads. Nothing else in the platform ever holds it — which is
why every write mints a *new* credential rather than reusing one: the plaintext
is unrecoverable by design (``DeviceToken`` keeps only a digest), so there is
no earlier secret left to reuse.

**Written before the previous credential is revoked.** A crash between the two
steps leaves a config that works and one stale credential still live; the other
order leaves a config pointing at a revoked credential and a gateway that gets
401 on every file — which is the failure this module exists to remove. The
stale credential is the smaller problem, and the next ``enable`` cleans it up.

The file name is the device's UUID, and the gateway names each ledger from the
``device`` *inside* the config rather than from its filename — see
``gateway.GatewayConfig.from_file``. That pairing is what makes it impossible
for a rewritten config to hand one instrument's ledger to another, which would
drop a capture with no error anywhere.
"""
import json
import logging
import os
import tempfile

from django.conf import settings

from .export_import import UNSUPPORTED_DATA_TYPES
from .models import DeviceToken
from .services import DeviceTokenService, TelemetryError

logger = logging.getLogger(__name__)

#: What every platform-written credential is labelled. Used to find the
#: earlier one to revoke, so it is a constant and not a caller's choice.
PROVISIONED_LABEL = 'Gateway (provisioned)'

#: Device registry type → the telemetry data type a capture from it records.
#:
#: Only PUNDIT has a file contract that describes a whole capture, so only
#: those entries can actually be served by the gateway; the rest are listed so
#: the refusal can name the importer's own reason rather than a generic one.
#: A type with no entry at all — thermal, "other" — has no file story yet and
#: gets its own message.
DEVICE_DATA_TYPES = {
    'pundit': 'pundit',
    'gpr': 'gpr',
    'tersus_gnss': 'gnss',
    'scanner': 'scan',
    'slam': 'scan',
}


class GatewayConfigError(TelemetryError):
    """Provisioning could not be done, or could not be undone."""


class GatewayConfigService:
    """Turning gateway sync on and off for one instrument.

    Enabling writes a config the gateway process reads; it does not start,
    configure or reach anything. The gateway picks the file up on its next
    sweep, which is why nothing here needs to know where the gateway is
    running or whether it is running at all.
    """

    @classmethod
    def enable(cls, device, *, actor):
        """Provision ``device`` for the field gateway. Returns the config path.

        Named ``enable`` rather than ``provision`` because that is what the
        officer does; the config file is how it happens.
        """
        directory = config_dir()
        path = os.path.join(directory, f'{device.id}.json')
        data_type = cls._data_type_for(device)

        if cls._is_current(path, device, data_type):
            # Already in the state that was asked for. Re-minting would revoke
            # a credential the running gateway is still using, and its next
            # sweep would be refused with a 401 that nobody at the site could
            # account for.
            cls._mark_enabled(device)
            return path

        # Read before minting, so the new credential is not in the set this is
        # about to revoke.
        superseded = list(DeviceToken.objects.filter(
            device=device, label=PROVISIONED_LABEL, revoked_at__isnull=True))

        token, raw = DeviceTokenService.issue(
            device=device, label=PROVISIONED_LABEL, issued_by=actor)
        try:
            _write_atomically(path, cls._config_for(device, raw, data_type))
        except OSError as exc:
            # Nothing holds the plaintext now, and nothing will: the config it
            # was minted for does not exist. Revoking it means a failed
            # enable leaves no live secret behind, which is the only tidy
            # outcome available at this point.
            DeviceTokenService.revoke(token, revoked_by=actor)
            raise GatewayConfigError(
                f'The gateway config could not be written to {directory} '
                f'({exc}). Nothing was provisioned, and the credential minted '
                f'a moment ago has been revoked. Check that the config '
                f'directory is writable by the application.',
                status_code=503)

        cls._create_inbox(device)
        for old in superseded:
            DeviceTokenService.revoke(old, revoked_by=actor)

        cls._mark_enabled(device)
        logger.info('Gateway sync enabled for %s (%s); config written to %s',
                    device.device_id, device.id, path)
        return path

    @classmethod
    def disable(cls, device, *, actor):
        """Stop syncing ``device``: revoke its credential and remove its config.

        Idempotent, and deliberately not conditional on the file existing — an
        instrument whose config was deleted by hand while the flag still said
        enabled is exactly the state this has to be able to clear.
        """
        path = os.path.join(config_dir(), f'{device.id}.json')
        for old in DeviceToken.objects.filter(
                device=device, label=PROVISIONED_LABEL, revoked_at__isnull=True):
            DeviceTokenService.revoke(old, revoked_by=actor)

        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as exc:
                raise GatewayConfigError(
                    f'The credential has been revoked, but the config at '
                    f'{path} could not be removed ({exc}). The instrument is '
                    f'no longer able to push; delete the file to stop the '
                    f'gateway reporting it.')
            logger.info('Gateway sync disabled for %s; %s removed',
                        device.device_id, path)

        if device.gateway_enabled:
            device.gateway_enabled = False
            device.save(update_fields=['gateway_enabled', 'updated_at'])
        return path

    # ------------------------------------------------------------------

    @classmethod
    def _is_current(cls, path, device, data_type):
        """Whether the file on disk already says what we would write.

        Everything the config carries is compared *except* the credential: its
        plaintext is unrecoverable by design, so there is nothing to compare it
        against, and a config whose token cannot be checked is one that must
        not be rewritten blindly.

        This is not an optimisation. The config embeds the instrument's
        project, so an instrument reassigned to another site would otherwise
        keep pushing its captures to the old one, with the panel saying sync
        was on and nothing anywhere saying it was pointed at the wrong job. A
        file that will not parse, or that names another project or folder, is
        not current and is rewritten.
        """
        current = _read_json(path)
        if current is None:
            return False
        return all(current.get(key) == value
                   for key, value in cls._shape_for(device, data_type).items())

    @classmethod
    def _mark_enabled(cls, device):
        if not device.gateway_enabled:
            device.gateway_enabled = True
            device.save(update_fields=['gateway_enabled', 'updated_at'])

    @classmethod
    def _data_type_for(cls, device):
        """The data type a capture from this instrument records, or a refusal.

        Checked at provisioning time rather than left to the gateway, because
        a file the platform refuses is never offered again: a mis-provisioned
        instrument would fill its folder with refusals and look, from the site,
        exactly like a gateway that is not running.
        """
        data_type = DEVICE_DATA_TYPES.get(device.device_type)
        if data_type is None:
            raise GatewayConfigError(
                f'{device.get_device_type_display()} captures cannot be pushed '
                f'from a file: the platform has no file contract for this kind '
                f'of instrument yet. Send the capture through the telemetry '
                f'console, or ask Nexucon to agree a column layout for its '
                f'export.')
        if data_type in UNSUPPORTED_DATA_TYPES:
            raise GatewayConfigError(UNSUPPORTED_DATA_TYPES[data_type])
        return data_type

    @classmethod
    def _shape_for(cls, device, data_type):
        """Everything the config carries except the credential."""
        return {
            'api_url': platform_url(),
            'device': str(device.id),
            # Empty is meaningful and is not the same as absent: it says this
            # instrument is not on a project yet. The endpoint needs one to
            # open a session, and a file arriving before it is assigned is
            # refused with that stated, rather than being sent to a project
            # chosen on the instrument's behalf.
            'project': str(device.assigned_project_id or ''),
            'watch_dir': inbox_dir_for(device),
            'data_type': data_type,
            # True, not false. The site may not have pointed its sync client at
            # the folder yet, and under a restart policy the alternative is a
            # process that exits every minute for a folder nobody has created.
            # The gateway logs the folder's absence once and waits.
            'wait_for_watch_dir': True,
        }

    @classmethod
    def _config_for(cls, device, token, data_type):
        """The JSON the gateway reads. ``token`` is the only secret in it."""
        shape = cls._shape_for(device, data_type)
        shape['device_token'] = token
        return shape

    @classmethod
    def _create_inbox(cls, device):
        """Make the instrument's own folder, best effort.

        So the path the officer is told to point their sync client at is a
        folder that exists. Failure is logged and not raised: the config is
        already written and valid, the gateway waits for the folder, and the
        sync client would have created it anyway.
        """
        path = inbox_dir_for(device)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            logger.warning(
                'Could not create the gateway inbox %s (%s). The config is '
                'written and the gateway is waiting for it; the sync client '
                'will create it when it first uploads.', path, exc)


# ----------------------------------------------------------------------
# Settings, read at call time
# ----------------------------------------------------------------------

def config_dir():
    """Where the platform writes gateway configs.

    Read on every call rather than at import so ``override_settings`` in a
    test works, and so a misconfigured deployment fails at the request that
    needed it rather than at process start.
    """
    directory = (getattr(settings, 'GATEWAY_CONFIG_DIR', '') or '').strip()
    if not directory:
        raise GatewayConfigError(
            'Platform gateway provisioning is switched off on this '
            'deployment: GATEWAY_CONFIG_DIR is not set, so there is nowhere '
            'for an instrument\'s config to be written. A gateway configured '
            'here but never written to would look like it was working.',
            status_code=503)
    if not os.path.isdir(directory):
        # Deliberately not created. Docker creates a mount point for an empty
        # named volume, so an *absent* directory means the volume is not
        # mounted — and creating it here would put the config on the
        # container's own ephemeral filesystem, where the gateway never sees it
        # and nothing anywhere says so. The gateway refuses the same state for
        # the same reason; see ``gateway.GatewaySupervisor``.
        raise GatewayConfigError(
            f'The gateway config directory {directory} does not exist. If it '
            f'is a volume, the volume is not mounted.', status_code=503)
    return directory


def inbox_dir_for(device):
    """The folder this instrument's exports must be synced into.

    Expressed as the *gateway container* sees it, which is not necessarily
    where the API process would put it: the value of ``GATEWAY_INBOX_DIR`` is
    written into the config verbatim. In this deployment both containers mount
    the same host directory at ``/inbox``, so one setting describes both sides.
    """
    root = (getattr(settings, 'GATEWAY_INBOX_DIR', '') or '').strip()
    if not root:
        raise GatewayConfigError(
            'GATEWAY_INBOX_DIR is not set, so the platform cannot say which '
            'folder this instrument\'s exports should be synced into.',
            status_code=503)
    return os.path.join(root, device.device_reference)


def platform_url():
    """The platform's address as the gateway must reach it.

    Inside the deployment that is the compose service (``http://web:8000``),
    not the public hostname: one less network hop, and no dependency on public
    DNS or on TLS terminating correctly for the gateway to be able to push.
    """
    return (getattr(settings, 'GATEWAY_API_URL', '') or '').strip()


# ----------------------------------------------------------------------

def _read_json(path):
    """The config at ``path``, or ``None`` if there is not a readable one.

    Not an error path: an unreadable config is one this service will overwrite,
    and one the gateway reports as a per-device failure in the meantime.
    """
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomically(path, payload):
    """Write ``payload`` to ``path`` so a reader never sees it half-written.

    The gateway re-parses every config on every tick, so a torn write is not
    hypothetical: it would be read as invalid JSON and stop that instrument
    until the next rewrite. ``os.replace`` is atomic within a filesystem, and
    the temporary file is created in the destination directory so the two are
    always on the same one.

    The directory is not created here. ``config_dir()`` has already refused a
    config directory that does not exist, and creating one at this depth would
    undo that check at exactly the moment it matters.

    ``0600`` because this file holds a live credential. ``mkstemp`` already
    creates it that way; the mode is set explicitly so the intent survives a
    remake of this function rather than depending on a default.
    """
    directory = os.path.dirname(path) or '.'
    handle = tempfile.NamedTemporaryFile(
        'w', encoding='utf-8', dir=directory, prefix='.gateway-',
        suffix='.tmp', delete=False)
    try:
        with handle:
            os.chmod(handle.name, 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise
