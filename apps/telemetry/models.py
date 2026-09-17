"""
Telemetry — the live-sensor ingestion envelope.

The Inspector PWA's Part 3 specifies two ingestion paths into the platform: a
device streaming a live session, and a person uploading a file. Both exist to
feed the *same* statutory registries the manual entry forms already write to
(apps.digital_eye.GPRSurvey / PUNDITTest / GnssSurvey, apps.scans.ScanSession).

This app is therefore an **envelope**, not a second registry. A
``TelemetrySession`` records that a device was running, on which project, under
whose accreditation, and holds the append-only ``TelemetryPacket`` log of what
it sent. At ``/end`` the session is *promoted*: its packets are replayed
through the very same serializers the manual entry forms use, which is what
creates the real GPR survey / PUNDIT test / GNSS survey rows and normalises
them into the Evidence Registry.

Why there is no ``gpr_data`` / ``upv_data`` / ``slam_data`` / ``thermal_data``
table here, despite the spec listing columns for them:

    Those tables already exist, under their statutory names. A parallel set
    would fork the write path — two places a GPR anomaly could live, two
    answers to "what did this survey find?" — and would make
    ``EvidenceRecord``'s uniqueness on ``(source_model, source_id)``
    meaningless, because the same physical measurement would arrive under two
    different source models depending on how it was captured. The spec's
    columns are all present; they are simply carried by the models that were
    already responsible for them.
"""
import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_session_ref():
    return f"TEL-{uuid.uuid4().hex[:10].upper()}"


class TelemetrySession(models.Model):
    """
    One continuous capture from one device.

    ``status`` and ``sync_status`` are two independent axes, not one:

      * ``status``      — is the device still streaming? (OPEN / ENDED / ABORTED)
      * ``sync_status`` — have the packets reached the statutory registry?
                          (PENDING / SYNCED / FAILED)

    They are separate because every combination is real. A session can be
    ENDED+PENDING (the device stopped, the phone has not reconnected yet), or
    ENDED+FAILED (promotion was attempted and a row was rejected), or even
    OPEN+PENDING (still streaming, nothing promoted yet — the only state in
    which a session may accept more packets). Collapsing them into one column
    would make "ended but not yet synced" unrepresentable, which is the normal
    state of the whole offline-first workflow this exists to support.
    """

    DATA_TYPE_CHOICES = [
        ('gpr', 'Ground Penetrating Radar'),
        ('pundit', 'PUNDIT Ultrasonic NDT'),
        ('gnss', 'GNSS / RTK Survey'),
        ('scan', '3D Scan / SLAM'),
    ]

    STATUS_OPEN = 'OPEN'
    STATUS_ENDED = 'ENDED'
    STATUS_ABORTED = 'ABORTED'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open — device streaming'),
        (STATUS_ENDED, 'Ended — capture complete, not yet promoted'),
        (STATUS_ABORTED, 'Aborted — capture abandoned'),
    ]

    # How the capture reached the platform. This is a property of the
    # *capture*, not of the device: the same PUNDIT unit can be carried to a
    # site with no signal and synced three days later, and the two arrivals
    # are different claims about the same measurement. Empty means the
    # transport was not recorded — which is the honest answer for every
    # session captured before this field existed, and for any client that
    # does not report one. It is deliberately never defaulted to a guess.
    TRANSPORT_BLE = 'BLE'
    TRANSPORT_WIFI = 'WIFI'
    TRANSPORT_CLOUD = 'CLOUD'
    TRANSPORT_FILE = 'FILE'
    TRANSPORT_MANUAL = 'MANUAL'
    TRANSPORT_CHOICES = [
        (TRANSPORT_BLE, 'Bluetooth — instrument to app'),
        (TRANSPORT_WIFI, 'Direct Wi-Fi — instrument to network'),
        (TRANSPORT_CLOUD, 'Cloud push — gateway to platform'),
        (TRANSPORT_FILE, 'Export file — instrument to file to app'),
        (TRANSPORT_MANUAL, 'Manual entry'),
    ]

    SYNC_PENDING = 'PENDING'
    SYNC_SYNCED = 'SYNCED'
    SYNC_FAILED = 'FAILED'
    SYNC_STATUS_CHOICES = [
        (SYNC_PENDING, 'Pending — not yet promoted'),
        (SYNC_SYNCED, 'Synced — rows written to the registry'),
        (SYNC_FAILED, 'Failed — promotion rejected, see sync_error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_reference = models.CharField(max_length=40, unique=True,
                                         default=generate_session_ref)

    # PROTECT, not SET_NULL: the device serial *is* the measurement's
    # provenance. Deleting a device must not silently orphan a session into
    # "captured by something, at some point", which is exactly the claim a
    # statutory record cannot make.
    device = models.ForeignKey(
        'digital_eye.FieldDevice', on_delete=models.PROTECT,
        related_name='telemetry_sessions',
        help_text='The physical instrument this session was captured on',
    )
    project = models.ForeignKey(
        'projects.Project', on_delete=models.CASCADE,
        related_name='telemetry_sessions',
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='telemetry_sessions',
    )
    operator_name = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Snapshot of the operator at capture time — survives user deletion',
    )

    data_type = models.CharField(max_length=30, choices=DATA_TYPE_CHOICES, db_index=True)
    transport = models.CharField(
        max_length=10, choices=TRANSPORT_CHOICES, blank=True, default='',
        db_index=True,
        help_text='How the capture arrived; blank means it was not recorded',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=STATUS_OPEN, db_index=True)
    sync_status = models.CharField(max_length=20, choices=SYNC_STATUS_CHOICES,
                                   default=SYNC_PENDING, db_index=True)

    # Nullable and deliberately *not* defaulted to created_at. `created_at` is
    # when the server first heard of this session; `session_start` is when the
    # instrument says it started. A device with no clock sends nothing, and
    # back-filling the server receipt time would turn "unknown" into a
    # recorded measurement time that nobody measured.
    session_start = models.DateTimeField(
        null=True, blank=True,
        help_text='Device-reported start time; null when the device reported none',
    )
    session_end = models.DateTimeField(
        null=True, blank=True,
        help_text='Device-reported end time; null while the session is open',
    )

    packet_count = models.PositiveIntegerField(default=0)

    # Session-level inputs shared by every packet (survey title, area,
    # structural element, transducer geometry ...). What the operator entered
    # once for the capture rather than per row.
    session_config = models.JSONField(
        default=dict, blank=True,
        help_text='Session-wide inputs applied to the promoted record',
    )

    # The normalised envelope built at /end, and the digest that attests it.
    # Empty until promotion — never a placeholder built at start.
    data_payload = models.JSONField(null=True, blank=True)
    sha256_hash = models.CharField(max_length=64, blank=True, default='')

    # The raw instrument export, when the capture arrived as a file. Kept
    # deliberately: the packets are a *derived interpretation* of these bytes,
    # so if the parser is ever wrong the original is the only ground truth
    # left. Blank for every session that did not come from a file — never a
    # placeholder naming a file that does not exist.
    source_file_name = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Original filename of the imported export, as the operator chose it',
    )
    source_file_sha256 = models.CharField(
        max_length=64, blank=True, default='',
        help_text='SHA-256 of the stored bytes, so the parse can be re-checked',
    )
    source_file_storage_name = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Storage key of the retained export',
    )

    sync_error = models.TextField(
        blank=True, default='',
        help_text='Why promotion was rejected, in the operator\'s terms',
    )
    promoted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['device', 'status']),
            models.Index(fields=['project', 'data_type']),
        ]

    def __str__(self):
        return f"{self.session_reference} ({self.get_data_type_display()})"

    @property
    def is_open(self):
        return self.status == self.STATUS_OPEN

    def verify_chain(self):
        """Recompute the packet chain from stored content.

        Returns True only if every packet's stored ``chain_hash`` still equals
        what its content produces, in sequence order. A single edited or
        reordered row makes this False — that is the whole point of storing
        the chain rather than trusting the rows.
        """
        from common.hashing import chain_hash

        previous = ''
        for packet in self.packets.order_by('sequence'):
            expected = chain_hash(previous, packet.sequence, packet.payload)
            if packet.chain_hash != expected:
                return False
            previous = packet.chain_hash
        return True


class TelemetryPacket(models.Model):
    """
    One append-only reading within a session.

    Append-only is enforced by ``UniqueConstraint(session, sequence)`` plus the
    absence of any update path: packets are never edited after receipt, because
    the row is the evidence of what the instrument sent. If a packet is wrong,
    the session is aborted and a new one is started — the same rule the audit
    ledger follows.

    This log is the one addition beyond the spec's column list, and it is
    load-bearing. The spec's shape — an accumulating ``data_payload`` on the
    session — is a read-modify-write on every append: two concurrent posts lose
    one of the writes, and when a row is malformed there is no way to say
    *which* row, or to replay it. A packet row is the unit that can be
    validated, rejected by position, and replayed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(TelemetrySession, on_delete=models.CASCADE,
                                related_name='packets')
    sequence = models.PositiveIntegerField(
        help_text='Device-assigned position within the session, starting at 1',
    )
    payload = models.JSONField(
        help_text='The measurement as the device sent it, unmodified',
    )

    previous_hash = models.CharField(max_length=64, blank=True, default='')
    chain_hash = models.CharField(max_length=64, blank=True, default='')

    recorded_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Device-reported capture time for this row; null when not reported',
    )
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['session', 'sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'sequence'],
                name='uniq_telemetry_packet_session_sequence',
            ),
        ]

    def __str__(self):
        return f"{self.session.session_reference} #{self.sequence}"


# ----------------------------------------------------------------------
# Device credentials — the machine legs (Wi-Fi / cloud gateway)
# ----------------------------------------------------------------------

#: Marks the credential as a device token rather than a platform API key
#: (`nx_live_`), so a leaked string is identifiable from sight alone.
DEVICE_TOKEN_PREFIX = 'nxdev_'


def generate_device_token():
    """A fresh device secret. Never stored — only its digest is."""
    return f"{DEVICE_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def hash_device_token(raw):
    """SHA-256 of the token, hex. Same scheme ``APIKeyGateway`` uses.

    A plain digest rather than a slow KDF is correct *here* and only here: the
    secret is 256 bits of CSPRNG output, so there is no low-entropy guess to
    slow down, and the digest is what makes lookup an indexed equality test
    instead of a scan over every token comparing in Python.
    """
    return hashlib.sha256(str(raw).encode('utf-8')).hexdigest()


class DeviceToken(models.Model):
    """A credential one physical instrument uses to push to the platform.

    Scoped to a single ``FieldDevice`` on purpose. A shared "site gateway" key
    would make every capture on that site attributable to whichever device
    happened to be named in the request body, which is the same as recording
    no provenance at all — and the device serial is the one field a statutory
    reading cannot be wrong about.

    The plaintext is returned exactly once, at issue, and never stored; the
    row keeps only the digest and a display prefix. Revocation is
    ``revoked_at``, checked on every request rather than only at the door, so
    revoking a token takes effect on the very next call.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # PROTECT, matching TelemetrySession.device: deleting the instrument must
    # not silently leave live credentials pointing at nothing.
    device = models.ForeignKey(
        'digital_eye.FieldDevice', on_delete=models.PROTECT,
        related_name='tokens',
        help_text='The one instrument this credential may push as',
    )
    label = models.CharField(
        max_length=255,
        help_text='What this credential is for, in the issuer\'s words',
    )
    #: `nxdev_` plus the first characters of the secret, for recognising a
    #: token in a list without holding anything that can be used to push.
    key_prefix = models.CharField(max_length=20)
    hashed_key = models.CharField(max_length=64, unique=True, db_index=True)

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='issued_device_tokens',
        help_text='The officer who provisioned this device; the token acts as them',
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Null means no expiry was set — not that it never expires',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-issued_at']
        indexes = [models.Index(fields=['device', 'revoked_at'])]

    def __str__(self):
        return f"{self.device.device_id} ({self.label})"

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_active(self):
        """Usable right now. Issuer status is checked separately, by the
        authenticator, because deactivating a user must not require walking
        every token they ever issued."""
        return not self.is_revoked and not self.is_expired

    @classmethod
    def resolve(cls, raw):
        """The active token matching ``raw``, or None.

        None covers every rejection — unknown, revoked, expired — because the
        caller must not be able to tell a mistyped token from a revoked one.
        """
        if not raw:
            return None
        token = (cls.objects
                 .select_related('device', 'issued_by')
                 .filter(hashed_key=hash_device_token(raw))
                 .first())
        if token is None or not token.is_active:
            return None
        return token

    def note_use(self):
        """Record that the credential was used, at most once a minute."""
        now = timezone.now()
        if not self.last_used_at or (now - self.last_used_at).total_seconds() > 60:
            DeviceToken.objects.filter(pk=self.pk).update(last_used_at=now)
            self.last_used_at = now
