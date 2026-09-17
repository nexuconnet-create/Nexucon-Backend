"""
Manual import models (Inspector PWA — Part 3.3, "Manual Import Schema").

Two tables, and the split between them is the whole design. A **batch** is one
uploaded file and what became of it; a **record** is one row inside that file
and what the platform made of it. Keeping them apart is what lets a 4,000-row
file report "3,998 valid, 2 rejected, here are the two" without either
re-reading the file or losing the fact that the other 3,998 were fine.

``import_batches`` and ``import_records`` follow the spec's columns. Three
departures, each load-bearing:

``project`` — not in the spec's table, and required here.
    A batch with no project cannot be scope-checked, and every record it
    carries writes into a project. The spec assumes the batch inherits the
    inspector's project; the platform cannot assume that, so it records it.

``inspector_name`` — a snapshot, not a join.
    Provenance has to survive the inspector's account being closed. A batch
    whose uploader is a bare foreign key reads "uploaded by —" the day the user
    row goes, which is exactly when provenance matters most.

``raw_data`` alongside ``record_data`` — the spec has only ``record_data``.
    "The file said transit time 42.1" and "we read that as 42.1 µs for point B"
    are different claims. When a column is misread, the pair is what shows
    whether the file was wrong or the parser was. One field cannot answer that,
    and guessing which happened is how a bad import gets blamed on the client.
"""
import datetime
import uuid

from django.conf import settings
from django.db import models


def generate_batch_reference():
    return f"IMP-{datetime.datetime.now().year}-{uuid.uuid4().hex[:6].upper()}"


class ImportBatch(models.Model):
    """One uploaded file, and what became of it."""

    IMPORT_CSV = 'CSV'
    IMPORT_JSON = 'JSON'
    IMPORT_PDF = 'PDF'
    IMPORT_TYPE_CHOICES = [
        (IMPORT_CSV, 'CSV'),
        (IMPORT_JSON, 'JSON'),
        (IMPORT_PDF, 'PDF'),
    ]

    STATUS_PENDING = 'PENDING'
    STATUS_VALIDATED = 'VALIDATED'
    STATUS_IMPORTED = 'IMPORTED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_VALIDATED, 'Validated'),
        (STATUS_IMPORTED, 'Imported'),
        (STATUS_FAILED, 'Failed'),
    ]

    #: The lifecycle is strictly forward: PENDING → VALIDATED → IMPORTED, with
    #: FAILED reachable from PENDING or VALIDATED. Nothing moves back out of
    #: IMPORTED — a committed batch's rows are in the registry, and re-opening
    #: it would let a second commit write them again.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch_reference = models.CharField(
        max_length=32, unique=True, editable=False, default=generate_batch_reference)

    inspector = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='import_batches',
        help_text='The user who uploaded the file',
    )
    inspector_name = models.CharField(
        max_length=255, blank=True, default='',
        help_text=(
            'Snapshot of the uploader\'s name at upload time. A snapshot rather '
            'than a join so provenance survives the account being closed.'
        ),
    )

    project = models.ForeignKey(
        'projects.Project', on_delete=models.CASCADE,
        related_name='import_batches',
        help_text='The project every record in this file is written against',
    )
    inspection = models.ForeignKey(
        'inspections.Inspection', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='import_batches',
        help_text=(
            'The inspection this file belongs to, when it belongs to one. '
            'Findings use it; instrument readings do not.'
        ),
    )

    import_type = models.CharField(
        max_length=8, choices=IMPORT_TYPE_CHOICES,
        help_text=(
            'What the file actually is, detected from its bytes rather than '
            'taken from the filename or the client\'s claim.'
        ),
    )
    record_type = models.CharField(
        max_length=20, blank=True, default='',
        help_text=(
            'The record kind this file carries (UPV, GPR, SLAM, FINDING). A '
            'single-kind file declares it once here; a mixed JSON file leaves '
            'it blank and each record carries its own.'
        ),
    )

    file_name = models.CharField(max_length=200)
    file_size_bytes = models.BigIntegerField(default=0)
    #: 1000, not the spec's 500: a presigned R2 URL carries the bucket, the key,
    #: the signature and the credential, and routinely runs past 500 characters.
    #: A column that truncates the URL to the file it describes is worse than a
    #: wide column.
    file_url = models.CharField(max_length=1000, blank=True, default='')
    storage_name = models.CharField(
        max_length=1000, blank=True, default='',
        help_text='The storage backend key, kept so the stored file can be re-read',
    )
    sha256_hash = models.CharField(
        max_length=64, blank=True, default='',
        help_text=(
            'SHA-256 over the stored bytes, computed in the same pass that '
            'writes them — so the hash attests the file that exists, not one '
            'the client says it sent.'
        ),
    )

    import_status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    validation_errors = models.JSONField(
        default=list, blank=True,
        help_text='Row-level problems from the last validation pass',
    )
    errors_truncated = models.BooleanField(
        default=False,
        help_text=(
            'True when the error list was capped. A 50,000-row bad file must '
            'not produce a 50,000-entry response — but the caller has to know '
            'it was capped rather than assume it saw everything.'
        ),
    )

    record_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            'Records the file was read as, including ones that were rejected. '
            'A row rejected before a record could be formed still counts, so '
            'that valid + invalid always equals the number of rows the '
            'inspector can see in their own file.'
        ),
    )
    valid_record_count = models.PositiveIntegerField(default=0)
    invalid_record_count = models.PositiveIntegerField(
        default=0,
        help_text='Records that failed validation. Any at all fails the batch.',
    )
    skipped_row_count = models.PositiveIntegerField(
        default=0,
        help_text=(
            'Rows whose every value is blank — skipped, and not records. A '
            'physically empty line is not counted here: it never reaches the '
            'reader as a row.'
        ),
    )

    validated_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Import batch'
        verbose_name_plural = 'Import batches'
        indexes = [
            models.Index(fields=['inspector', 'import_status']),
            models.Index(fields=['project', 'import_status']),
            models.Index(fields=['sha256_hash']),
        ]

    def __str__(self):
        return f'{self.batch_reference} — {self.file_name} ({self.import_status})'

    @property
    def imported(self):
        return self.import_status == self.STATUS_IMPORTED

    @property
    def can_commit(self):
        """Only a fully-validated batch may be committed.

        Not FAILED, not PENDING — an unattempted or partly-invalid file has no
        rows worth writing, and committing one would put a file into the
        registry that the platform already told the inspector it had rejected.
        """
        return self.import_status == self.STATUS_VALIDATED


class ImportRecord(models.Model):
    """One row of an uploaded file, and what the platform made of it."""

    STATUS_PENDING = 'PENDING'
    STATUS_VALID = 'VALID'
    STATUS_INVALID = 'INVALID'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_VALID, 'Valid'),
        (STATUS_INVALID, 'Invalid'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name='records')

    row_number = models.PositiveIntegerField(
        help_text='The first row of the source file this record came from')
    row_end = models.PositiveIntegerField(
        default=0,
        help_text=(
            'The last source row this record came from. Equal to row_number for '
            'a one-row record; wider for a grouped one (a UPV test is several '
            'consecutive reading rows and lands as a single registry row).'
        ),
    )

    record_type = models.CharField(max_length=20)
    raw_data = models.JSONField(
        default=list, blank=True,
        help_text='The source rows verbatim, before any interpretation',
    )
    record_data = models.JSONField(
        default=dict, blank=True,
        help_text='The record as the platform interpreted it, ready for its serializer',
    )

    validation_status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    error_message = models.TextField(
        blank=True, default='',
        help_text='Why this row was rejected, as a message a person can act on',
    )

    target_model = models.CharField(max_length=100, blank=True, default='')
    target_id = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['row_number', 'id']
        verbose_name = 'Import record'
        verbose_name_plural = 'Import records'
        indexes = [
            models.Index(fields=['batch', 'validation_status']),
        ]

    def __str__(self):
        return (f'{self.record_type} row {self.row_number} — '
                f'{self.validation_status}')

    @property
    def row_label(self):
        if self.row_end and self.row_end != self.row_number:
            return f'rows {self.row_number}-{self.row_end}'
        return f'row {self.row_number}'
