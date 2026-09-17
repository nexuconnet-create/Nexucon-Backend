"""
Tests for the manual import pipeline (Inspector PWA Part 3.3).

Almost everything worth testing here is about the boundary between "the file
was accepted" and "the registry changed", because that boundary is where an
import goes wrong in ways nobody notices:

  * **A row that was rejected must not be in the registry.** Asserted by
    counting the target table, not by reading the batch's status — a batch can
    report FAILED while its rows are already written, which is the single
    worst outcome this pipeline can produce.
  * **A commit that rolls back must leave the batch saying so.** The status is
    written inside the transaction precisely so this holds; the rollback test
    patches a serializer to fail after validation and then re-reads the batch.
  * **The hash must describe the bytes on disk.** Asserted against a locally
    computed SHA-256 of the same bytes.

The file-backed tests run against a temporary MEDIA_ROOT, so nothing is left
behind in the repository's media directory.
"""
import csv
import hashlib
import io
import json
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.digital_eye.models import GPRSurvey, PUNDITReading, PUNDITTest
from apps.inspections.models import Finding, Inspection
from apps.projects.models import Project
from apps.scans.models import Defect, ScanSession

from .models import ImportBatch, ImportRecord
from .readers import ImportReadError, detect_import_type, normalise_key, read_rows
from .registry import REGISTRY
from .services import ImportService, ImportServiceError
from .templates import build_template

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='nexucon_import_tests_media_')

local_storage_settings = override_settings(
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    MEDIA_ROOT=_TEST_MEDIA_ROOT,
    MEDIA_URL='/media/',
)


class _Upload:
    """A minimal stand-in for a Django ``UploadedFile``.

    ``chunks()`` is what the service reads, and ``name`` is what it stores
    under. Using the real class would drag in a Django test client per file for
    no gain — the service never touches the request.
    """

    def __init__(self, content, name):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self._content = content
        self.name = name
        self.size = len(content)

    def chunks(self, chunk_size=8192):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]

    def read(self):
        return self._content


def csv_bytes(columns, rows):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode('utf-8')


# ----------------------------------------------------------------------
# Readers — bytes to rows
# ----------------------------------------------------------------------

class ReaderTests(TestCase):
    def test_the_bytes_decide_the_format_not_the_name(self):
        self.assertEqual(detect_import_type(b'[{"a": 1}]'), 'JSON')
        self.assertEqual(detect_import_type(b'  \n {"a": 1}'), 'JSON')
        self.assertEqual(detect_import_type(b'%PDF-1.7\n'), 'PDF')
        self.assertEqual(detect_import_type(b'TITLE,LAT\nx,1\n'), 'CSV')

    def test_keys_are_folded_so_case_and_spacing_do_not_matter(self):
        for spelling in ('STRUCTURAL ELEMENT', 'Structural Element',
                         'structural_element', ' structural  element '):
            self.assertEqual(normalise_key(spelling), 'structural_element')

    def test_a_csv_row_reports_its_physical_line(self):
        """The number in an error is the number the inspector can open the file
        and look at."""
        content = csv_bytes(['A', 'B'], [['1', '2'], ['3', '4']])
        rows, skipped = read_rows(content, 'CSV')
        self.assertEqual(skipped, 0)
        self.assertEqual([row.row_number for row in rows], [2, 3])

    def test_a_quoted_newline_does_not_shift_the_row_numbers(self):
        content = (b'A,B\n'
                   b'"line one\nline two",2\n'
                   b'"x",4\n')
        rows, _ = read_rows(content, 'CSV')
        self.assertEqual(rows[0].data['a'], 'line one\nline two')
        # The third record starts on physical line 4, and says so.
        self.assertEqual(rows[1].row_number, 4)

    def test_blank_rows_are_skipped_and_counted(self):
        """A row whose every value is blank is skipped and counted.

        An *empty line* is a different thing and is not counted, because it
        never reaches the reader as a record at all — which the physical line
        number of the row after it shows: the reader reports line 5 for what is
        the fifth line of the file. Nothing is lost by that, since an empty line
        holds no value to reject, but ``skipped_row_count`` must not imply it
        counted lines the reader never saw, or an inspector comparing the count
        against their spreadsheet would find it off by the number of blank
        lines.
        """
        content = b'A,B\n1,2\n,\n\n3,4\n'
        rows, skipped = read_rows(content, 'CSV')
        self.assertEqual(len(rows), 2)
        self.assertEqual(skipped, 1)
        self.assertEqual([row.row_number for row in rows], [2, 5])

    def test_two_columns_meaning_the_same_thing_are_refused(self):
        content = b'Title,title\nx,y\n'
        with self.assertRaises(ImportReadError) as ctx:
            read_rows(content, 'CSV')
        self.assertIn('two columns', str(ctx.exception))

    def test_a_headerless_file_is_refused(self):
        with self.assertRaises(ImportReadError):
            read_rows(b'', 'CSV')

    def test_non_utf8_text_is_refused_with_a_fixable_message(self):
        with self.assertRaises(ImportReadError) as ctx:
            read_rows(b'A,B\n\xff\xfe caf\xe9,2\n', 'CSV')
        self.assertIn('UTF-8', str(ctx.exception))

    def test_json_accepts_a_bare_list_and_a_wrapped_one(self):
        bare, _ = read_rows(b'[{"a": 1}, {"a": 2}]', 'JSON')
        wrapped, _ = read_rows(b'{"records": [{"a": 1}, {"a": 2}]}', 'JSON')
        self.assertEqual(len(bare), 2)
        self.assertEqual(len(wrapped), 2)
        self.assertEqual(bare[0].row_number, 1)

    def test_json_that_is_not_a_list_of_records_is_refused(self):
        with self.assertRaises(ImportReadError) as ctx:
            read_rows(b'{"summary": 4}', 'JSON')
        self.assertIn('list of records', str(ctx.exception))

    def test_json_with_a_syntax_error_names_the_line(self):
        with self.assertRaises(ImportReadError) as ctx:
            read_rows(b'[{"a": 1,}]', 'JSON')
        self.assertIn('line', str(ctx.exception))

    def test_a_pdf_is_refused_with_the_reason(self):
        with self.assertRaises(ImportReadError) as ctx:
            read_rows(b'%PDF-1.7\n...', 'PDF')
        self.assertIn('column contract', str(ctx.exception))


# ----------------------------------------------------------------------
# Upload
# ----------------------------------------------------------------------

@local_storage_settings
class UploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='importer@nexucon.com', email='importer@nexucon.com',
            password='Password123!', first_name='Ngozi', last_name='Eze')
        self.project = Project.objects.create(name='Import Site', status='ACTIVE')

    def _upload(self, content, name='readings.csv', **kwargs):
        return ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(content, name), **kwargs)

    def test_the_stored_hash_equals_the_hash_of_the_same_bytes(self):
        content = csv_bytes(['A'], [['1']])
        batch, created = self._upload(content)
        self.assertTrue(created)
        self.assertEqual(batch.sha256_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(batch.file_size_bytes, len(content))

    def test_the_recorded_size_is_the_size_that_was_stored(self):
        from django.core.files.storage import default_storage
        content = csv_bytes(['A'], [['1'], ['2']])
        batch, _ = self._upload(content)
        self.assertEqual(default_storage.size(batch.storage_name), len(content))

    def test_the_uploader_name_is_snapshotted(self):
        batch, _ = self._upload(csv_bytes(['A'], [['1']]))
        self.assertEqual(batch.inspector_name, 'Ngozi Eze')
        self.user.first_name = 'Renamed'
        self.user.save()
        batch.refresh_from_db()
        self.assertEqual(batch.inspector_name, 'Ngozi Eze')

    def test_identical_bytes_uploaded_twice_reuse_the_pending_batch(self):
        content = csv_bytes(['A'], [['1']])
        first, _ = self._upload(content)
        second, created = self._upload(content, name='renamed.csv')
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ImportBatch.objects.count(), 1)

    def test_an_imported_batch_is_never_reused(self):
        """The same bytes uploaded again after a successful import is a new
        submission. Pointing it at the committed batch would let a second
        commit write the same file's rows twice."""
        content = csv_bytes(['SURVEY TITLE'], [['Survey A']])
        first, _ = self._upload(content, record_type='GPR')
        ImportService.validate(first)
        ImportService.commit(first)
        first.refresh_from_db()
        self.assertEqual(first.import_status, ImportBatch.STATUS_IMPORTED)

        second, created = self._upload(content, record_type='GPR')
        self.assertTrue(created)
        self.assertNotEqual(first.pk, second.pk)

    def test_a_different_project_gets_its_own_batch(self):
        other = Project.objects.create(name='Other Site', status='ACTIVE')
        content = csv_bytes(['A'], [['1']])
        self._upload(content)
        ImportService.upload(user=self.user, project=other,
                             uploaded_file=_Upload(content, 'x.csv'))
        self.assertEqual(ImportBatch.objects.count(), 2)

    def test_the_type_is_detected_from_the_bytes(self):
        json_batch, _ = self._upload(b'[{"a": 1}]', name='data.csv')
        self.assertEqual(json_batch.import_type, 'JSON')

    def test_a_claim_that_contradicts_the_bytes_is_refused(self):
        with self.assertRaises(ImportServiceError) as ctx:
            self._upload(b'[{"a": 1}]', import_type='CSV')
        self.assertIn('contents are JSON', str(ctx.exception))
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_an_unknown_record_type_is_refused(self):
        with self.assertRaises(ImportServiceError) as ctx:
            self._upload(csv_bytes(['A'], [['1']]), record_type='MAGIC')
        self.assertIn('not an importable record type', str(ctx.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ImportServiceError) as ctx:
            self._upload(b'')
        self.assertIn('empty', str(ctx.exception))

    @override_settings(IMPORT_MAX_UPLOAD_BYTES=32)
    def test_an_oversized_file_is_refused_rather_than_silently_cut(self):
        with self.assertRaises(ImportServiceError) as ctx:
            self._upload(csv_bytes(['A'], [['x' * 100]]))
        self.assertIn('larger than', str(ctx.exception))

    def test_a_pdf_uploads_successfully(self):
        """The spec's decision, confirmed before this was built: accept at
        upload, fail honestly at validation. Rejecting it here would leave the
        inspector with no batch to look at and nothing to appeal."""
        batch, created = self._upload(b'%PDF-1.7\nnot really a pdf\n',
                                      name='scan.pdf')
        self.assertTrue(created)
        self.assertEqual(batch.import_type, 'PDF')
        self.assertEqual(batch.import_status, ImportBatch.STATUS_PENDING)

    def test_an_inspection_from_another_project_is_refused(self):
        other = Project.objects.create(name='Elsewhere', status='ACTIVE')
        inspection = Inspection.objects.create(
            project=other, inspection_type='Foundation Inspection')
        with self.assertRaises(ImportServiceError) as ctx:
            self._upload(csv_bytes(['A'], [['1']]), inspection=inspection)
        self.assertIn('different project', str(ctx.exception))


# ----------------------------------------------------------------------
# Validate
# ----------------------------------------------------------------------

@local_storage_settings
class ValidateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='validator@nexucon.com', email='validator@nexucon.com',
            password='Password123!')
        self.project = Project.objects.create(name='Validate Site', status='ACTIVE')

    def _batch(self, columns, rows, record_type='GPR', **kwargs):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(csv_bytes(columns, rows), 'file.csv'),
            record_type=record_type, **kwargs)
        return batch

    def test_a_clean_file_validates(self):
        batch = self._batch(['SURVEY TITLE'], [['Zone A'], ['Zone B']])
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_VALIDATED)
        self.assertEqual(batch.record_count, 2)
        self.assertEqual(batch.valid_record_count, 2)
        self.assertEqual(batch.invalid_record_count, 0)

    def test_validation_writes_no_registry_rows(self):
        batch = self._batch(['SURVEY TITLE'], [['Zone A']])
        ImportService.validate(batch)
        self.assertEqual(GPRSurvey.objects.count(), 0)

    def test_one_bad_row_fails_the_whole_batch_and_names_its_line(self):
        batch = self._batch(
            ['SURVEY TITLE', 'ANTENNA FREQUENCY (MHZ)'],
            [['Zone A', '400'], ['Zone B', 'four hundred'], ['Zone C', '400']])
        ImportService.validate(batch)
        batch.refresh_from_db()

        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertEqual(batch.record_count, 3)
        self.assertEqual(batch.valid_record_count, 2)
        self.assertEqual(batch.invalid_record_count, 1)
        # Row 3 is the third line of the file — header is row 1.
        self.assertEqual(batch.validation_errors[0]['row'], 3)
        self.assertIn('ANTENNA FREQUENCY', batch.validation_errors[0]['message'])

    def test_nine_good_rows_and_one_bad_row_report_the_counts(self):
        rows = [[f'Zone {index}', '400'] for index in range(9)]
        rows.append(['Zone Bad', 'nope'])
        batch = self._batch(['SURVEY TITLE', 'ANTENNA FREQUENCY (MHZ)'], rows)
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertEqual(batch.valid_record_count, 9)
        self.assertEqual(batch.invalid_record_count, 1)
        self.assertEqual(GPRSurvey.objects.count(), 0)

    def test_a_missing_required_column_reports_every_offending_row(self):
        batch = self._batch(['SURVEY TITLE', 'SURVEY AREA'],
                            [['', 'Grid 1'], ['Zone B', 'Grid 2']])
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertEqual(batch.invalid_record_count, 1)
        self.assertEqual(batch.validation_errors[0]['row'], 2)
        self.assertIn('SURVEY TITLE', batch.validation_errors[0]['message'])
        # The rejected row is a record of the batch like any other, so the
        # inspector's per-row report shows row 2 with its reason rather than
        # only the rows that happened to survive.
        self.assertEqual(ImportRecord.objects.filter(batch=batch).count(), 2)
        rejected = ImportRecord.objects.get(batch=batch, row_number=2)
        self.assertEqual(rejected.validation_status, 'INVALID')
        self.assertIn('SURVEY TITLE', rejected.error_message)

    def test_records_are_replaced_not_appended_on_revalidation(self):
        batch = self._batch(['SURVEY TITLE'], [['Zone A'], ['Zone B']])
        ImportService.validate(batch)
        ImportService.validate(batch)
        self.assertEqual(ImportRecord.objects.filter(batch=batch).count(), 2)

    def test_raw_data_and_record_data_both_survive(self):
        """The difference between "the file said X" and "we read it as Y" is
        what shows whether the file was wrong or the parser was."""
        batch = self._batch(['SURVEY TITLE', 'DEPTH RANGE (M)'],
                            [['Zone A', '2.50']])
        ImportService.validate(batch)
        record = ImportRecord.objects.get(batch=batch)
        self.assertEqual(record.raw_data[0]['survey_title'], 'Zone A')
        self.assertEqual(record.raw_data[0]['depth_range_m'], '2.50')
        self.assertEqual(record.record_data['title'], 'Zone A')
        self.assertEqual(record.record_data['depth_range_m'], 2.5)

    def test_a_cleaned_up_field_labelled_blank_is_an_honest_absence(self):
        """A blank optional column is not a zero, and not an error."""
        batch = self._batch(['SURVEY TITLE', 'GRID SPACING (M)'],
                            [['Zone A', '']])
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_VALIDATED)
        record = ImportRecord.objects.get(batch=batch)
        self.assertNotIn('grid_spacing_m', record.record_data)

    def test_an_empty_file_fails_with_a_message_naming_the_template(self):
        batch = self._batch(['SURVEY TITLE'], [])
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('template', batch.validation_errors[0]['message'])

    def test_a_pdf_fails_at_validation_with_the_reason_and_no_rows(self):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(b'%PDF-1.7\n', 'scan.pdf'),
            record_type='GPR')
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertEqual(batch.record_count, 0)
        self.assertIn('column contract', batch.validation_errors[0]['message'])
        self.assertEqual(GPRSurvey.objects.count(), 0)

    def test_errors_are_capped_and_said_to_be_capped(self):
        rows = [['', '1.0'] for _ in range(12)]
        batch = self._batch(['SURVEY TITLE', 'DEPTH RANGE (M)'], rows)
        with override_settings(IMPORT_MAX_ERRORS=5):
            ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(len(batch.validation_errors), 5)
        self.assertTrue(batch.errors_truncated)
        # The count is the real one, not the capped list's length — a client
        # showing "5 problems" for a 12-row failure would understate it.
        self.assertEqual(batch.invalid_record_count, 12)

    def test_an_unknown_record_type_on_a_json_row_is_reported(self):
        content = json.dumps([
            {'record_type': 'UPV', 'structural_element': 'C4',
             'test_type': 'Pulse Velocity', 'path_length_l_mm': 300,
             'transit_time_t_us': 72.4},
            {'record_type': 'TELEPATHY', 'x': 1},
        ]).encode()
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(content, 'mixed.json'))
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.valid_record_count, 1)
        self.assertEqual(batch.invalid_record_count, 1)
        self.assertIn('TELEPATHY', batch.validation_errors[0]['message'])
        # And the file cannot be imported on the strength of the row that did
        # parse: one unreadable record fails the batch.
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertFalse(batch.can_commit)
        self.assertEqual(PUNDITTest.objects.count(), 0)

    def test_a_tampered_stored_file_is_refused(self):
        """The hash attests the bytes on disk. If they change, the validation
        says so instead of validating something nobody uploaded."""
        from django.core.files.storage import default_storage
        batch = self._batch(['SURVEY TITLE'], [['Zone A']])
        default_storage.delete(batch.storage_name)
        default_storage.save(batch.storage_name,
                             _Upload(b'SURVEY TITLE\nTampered\n', 'x'))
        ImportService.validate(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('does not match the hash', batch.validation_errors[0]['message'])


# ----------------------------------------------------------------------
# Commit
# ----------------------------------------------------------------------

@local_storage_settings
class CommitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='committer@nexucon.com', email='committer@nexucon.com',
            password='Password123!', first_name='Tunde', last_name='Bello')
        self.project = Project.objects.create(name='Commit Site', status='ACTIVE')

    def _validated(self, columns, rows, record_type='GPR', **kwargs):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(csv_bytes(columns, rows), 'file.csv'),
            record_type=record_type, **kwargs)
        ImportService.validate(batch)
        batch.refresh_from_db()
        return batch

    def test_committing_writes_real_rows_with_server_computed_fields(self):
        batch = self._validated(['SURVEY TITLE', 'DEPTH RANGE (M)'],
                                [['Zone A', '2.5'], ['Zone B', '3.0']])
        ImportService.commit(batch, _request(self.user))
        self.assertEqual(GPRSurvey.objects.count(), 2)
        survey = GPRSurvey.objects.get(title='Zone A')
        # Written by the serializer, not by a raw insert: the reference is
        # generated server-side, and the record is attributed to the uploader
        # exactly as the manual create endpoint attributes one.
        self.assertTrue(survey.survey_reference)
        self.assertEqual(survey.created_by, self.user)
        self.assertEqual(survey.operator, self.user)
        self.assertEqual(survey.project, self.project)

    def test_an_imported_record_carries_the_uploader_like_a_typed_one(self):
        """`created_by`/`operator` are the values `perform_create` passes to
        `serializer.save()`. Without them an imported survey shows a blank
        operator in the UI while a manually created one shows a name, which
        reads as "nobody did this" rather than as "this came from a file"."""
        batch = self._validated(
            list(REGISTRY['UPV'].columns),
            [['Column C4', 'Ground', 'Pulse Velocity', 'A', '300', '72.4', '',
              'Dry', '', '', '', '', '', '']],
            record_type='UPV')
        ImportService.commit(batch, _request(self.user))
        test = PUNDITTest.objects.get()
        self.assertEqual(test.created_by, self.user)
        self.assertEqual(test.operator, self.user)

    def test_the_records_point_at_what_they_wrote(self):
        batch = self._validated(['SURVEY TITLE'], [['Zone A']])
        ImportService.commit(batch, _request(self.user))
        record = ImportRecord.objects.get(batch=batch)
        self.assertEqual(record.target_model, 'digital_eye.GPRSurvey')
        survey = GPRSurvey.objects.get()
        self.assertEqual(record.target_id, str(survey.id))

    def test_the_status_and_the_rows_commit_together(self):
        batch = self._validated(['SURVEY TITLE'], [['Zone A']])
        ImportService.commit(batch, _request(self.user))
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_IMPORTED)
        self.assertIsNotNone(batch.imported_at)
        self.assertEqual(batch.valid_record_count, 1)

    def test_committing_twice_is_refused(self):
        batch = self._validated(['SURVEY TITLE'], [['Zone A']])
        ImportService.commit(batch, _request(self.user))
        batch.refresh_from_db()
        with self.assertRaises(ImportServiceError) as ctx:
            ImportService.commit(batch, _request(self.user))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(GPRSurvey.objects.count(), 1)

    def test_a_failed_batch_cannot_be_committed(self):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(csv_bytes(['SURVEY TITLE'], [['', 'x']]), 'f.csv'),
            record_type='GPR')
        ImportService.validate(batch)
        batch.refresh_from_db()
        with self.assertRaises(ImportServiceError) as ctx:
            ImportService.commit(batch, _request(self.user))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(GPRSurvey.objects.count(), 0)

    def test_a_serializer_that_fails_at_commit_rolls_everything_back(self):
        """The rollback test.

        Validation passed; a rule then rejected the row at write time. The whole
        import must be refused and the batch must still say VALIDATED — the one
        state a status written outside the transaction would get wrong.

        The failure is injected through ``validate()`` rather than by stubbing
        ``is_valid``, so it takes the path a real rule takes: DRF catches the
        ``ValidationError``, records it, and ``is_valid()`` returns False with
        ``errors`` populated — which is what the commit's error message reads.
        """
        from rest_framework import serializers as drf_serializers

        from apps.digital_eye.serializers import GPRSurveySerializer

        batch = self._validated(['SURVEY TITLE'], [['Zone A'], ['Zone B']])

        def rejects_zone_a(self, attrs):
            if attrs.get('title') == 'Zone A':
                raise drf_serializers.ValidationError(
                    'A survey titled "Zone A" is already recorded for this site.')
            return attrs

        with mock.patch.object(GPRSurveySerializer, 'validate', rejects_zone_a):
            with self.assertRaises(ImportServiceError) as ctx:
                ImportService.commit(batch, _request(self.user))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('Nothing was imported', str(ctx.exception))
        self.assertIn('already recorded', str(ctx.exception))

        # The first row passed and the second was refused: had the write not
        # been one transaction, the first would be in the registry now.
        self.assertEqual(GPRSurvey.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_VALIDATED)
        self.assertIsNone(batch.imported_at)


# ----------------------------------------------------------------------
# Record types
# ----------------------------------------------------------------------

@local_storage_settings
class UpvImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='upv@nexucon.com', email='upv@nexucon.com',
            password='Password123!')
        self.project = Project.objects.create(name='UPV Site', status='ACTIVE')

    def _import(self, rows):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(csv_bytes(list(REGISTRY['UPV'].columns), rows),
                                  'upv.csv'),
            record_type='UPV')
        ImportService.validate(batch)
        batch.refresh_from_db()
        if batch.import_status == ImportBatch.STATUS_VALIDATED:
            ImportService.commit(batch, _request(self.user))
        return batch

    def test_consecutive_rows_for_one_element_become_one_test(self):
        batch = self._import([
            ['Column C4', 'Ground', 'Pulse Velocity', 'A', '300', '72.4', '',
             'Dry', '54', 'Direct', 'Grid 4', 'Dry', '', ''],
            ['Column C4', 'Ground', 'Pulse Velocity', 'B', '300', '74.1', '',
             'Dry', '54', 'Direct', 'Grid 4', 'Dry', '', ''],
            ['Beam B2', 'First', 'Pulse Velocity', 'A', '400', '96.2', '',
             'Dry', '54', 'Direct', 'Grid B', 'Dry', '', ''],
        ])
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_IMPORTED)
        self.assertEqual(PUNDITTest.objects.count(), 2)
        self.assertEqual(batch.record_count, 2)

        first = PUNDITTest.objects.get(structural_element='Column C4')
        self.assertEqual(first.readings.count(), 2)
        self.assertEqual(first.readings.order_by('point_label')[0].point_label, 'A')
        # The test spans two source rows, and the record says so.
        record = ImportRecord.objects.get(batch=batch, row_number=2)
        self.assertEqual(record.row_end, 3)

    def test_the_serializer_computes_velocity_and_grade_not_the_file(self):
        """Nothing in the file can set a velocity — it is derived from the path
        length and the transit time by the same code the manual form uses."""
        self._import([
            ['Column C4', 'Ground', 'Pulse Velocity', 'A', '300', '72.4', '',
             'Dry', '54', 'Direct', 'Grid 4', 'Dry', '', ''],
        ])
        test = PUNDITTest.objects.get()
        self.assertIsNotNone(test.pulse_velocity_ms)
        self.assertTrue(test.quality_grade)
        # 300 mm over 72.4 µs, in m/s — the unit `pulse_velocity_ms` is named
        # for. Asserted from the physics rather than from the field's previous
        # value, so a units change is a failure and not a new expectation.
        self.assertAlmostEqual(test.pulse_velocity_ms, 0.3 / 72.4e-6, places=1)
        self.assertAlmostEqual(test.velocity_km_s, 0.3 / 72.4e-6 / 1000, places=3)

    def test_a_blank_transit_time_is_reported_against_its_own_row(self):
        batch = self._import([
            ['Column C4', 'Ground', 'Pulse Velocity', 'A', '300', '72.4', '',
             'Dry', '', '', '', '', '', ''],
            ['Beam B2', 'First', 'Pulse Velocity', 'A', '400', '', '',
             'Dry', '', '', '', '', '', ''],
        ])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertEqual(batch.invalid_record_count, 1)
        self.assertEqual(batch.validation_errors[0]['row'], 3)
        self.assertIn('TRANSIT TIME', batch.validation_errors[0]['message'])
        self.assertEqual(PUNDITTest.objects.count(), 0)

    def test_an_element_split_into_two_blocks_is_refused(self):
        """Almost always a sorting accident, and splitting it silently would
        file the same element as two tests."""
        batch = self._import([
            ['Column C4', 'Ground', 'Pulse Velocity', 'A', '300', '72.4', '',
             'Dry', '', '', '', '', '', ''],
            ['Beam B2', 'First', 'Pulse Velocity', 'A', '400', '96.2', '',
             'Dry', '', '', '', '', '', ''],
            ['Column C4', 'Ground', 'Pulse Velocity', 'B', '300', '74.1', '',
             'Dry', '', '', '', '', '', ''],
        ])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('two separate blocks', batch.validation_errors[0]['message'])

    def test_crack_depth_needs_both_transit_times(self):
        batch = self._import([
            ['Column C4', 'Ground', 'Crack Depth', 'A', '300', '108.6', '',
             'Dry', '', '', '', '', '', ''],
        ])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('uncracked', batch.validation_errors[0]['message'])


@local_storage_settings
class SlamImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='slam@nexucon.com', email='slam@nexucon.com',
            password='Password123!')
        self.project = Project.objects.create(name='SLAM Site', status='ACTIVE')
        self.session = ScanSession.objects.create(
            project=self.project, scanner_id='SCN-001', status='completed')

    def _import(self, rows):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(csv_bytes(list(REGISTRY['SLAM'].columns), rows),
                                  'slam.csv'),
            record_type='SLAM')
        ImportService.validate(batch)
        batch.refresh_from_db()
        if batch.import_status == ImportBatch.STATUS_VALIDATED:
            ImportService.commit(batch, _request(self.user))
        return batch

    def test_a_defect_lands_against_its_scan_session(self):
        batch = self._import([
            [str(self.session.id), 'crack', 'HIGH', 'OPEN', '12.4', '3.1', '0.8',
             'G4', 'Ground', 'Diagonal crack', '0.88'],
        ])
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_IMPORTED)
        defect = Defect.objects.get()
        self.assertEqual(defect.session, self.session)
        # Case is presentation, not content.
        self.assertEqual(defect.severity, 'high')
        self.assertEqual(defect.type, 'crack')
        self.assertEqual(defect.confidence_score, 0.88)

    def test_an_unknown_session_is_refused_and_the_message_lists_the_real_ones(self):
        batch = self._import([
            ['11111111-1111-1111-1111-111111111111', 'crack', 'high', 'OPEN',
             '', '', '', '', '', '', ''],
        ])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        message = batch.validation_errors[0]['message']
        self.assertIn('was not found in this project', message)
        self.assertIn(str(self.session.id), message)
        self.assertEqual(Defect.objects.count(), 0)

    def test_a_confidence_of_ninety_is_refused_not_divided(self):
        batch = self._import([
            [str(self.session.id), 'crack', 'high', 'OPEN', '', '', '', '', '',
             '', '90'],
        ])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('0.9, not 90', batch.validation_errors[0]['message'])


@local_storage_settings
class FindingImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='finding@nexucon.com', email='finding@nexucon.com',
            password='Password123!')
        self.project = Project.objects.create(name='Finding Site', status='ACTIVE')
        self.inspection = Inspection.objects.create(
            project=self.project, inspection_type='Foundation Inspection')

    def _import(self, rows, **kwargs):
        batch, _ = ImportService.upload(
            user=self.user, project=self.project,
            uploaded_file=_Upload(
                csv_bytes(list(REGISTRY['FINDING'].columns), rows), 'findings.csv'),
            record_type='FINDING', **kwargs)
        ImportService.validate(batch)
        batch.refresh_from_db()
        if batch.import_status == ImportBatch.STATUS_VALIDATED:
            ImportService.commit(batch, _request(self.user))
        return batch

    def test_a_finding_lands_against_the_named_inspection(self):
        batch = self._import([
            [self.inspection.inspection_reference, 'Honeycombing to C4',
             'Exposed aggregate over 300mm.', 'HIGH', 'STRUCTURAL',
             'Hack off and re-cast the cover', '2026-10-01', 'yes'],
        ])
        batch.refresh_from_db()
        self.assertEqual(batch.import_status, ImportBatch.STATUS_IMPORTED)
        finding = Finding.objects.get()
        self.assertEqual(finding.inspection, self.inspection)
        self.assertEqual(finding.project, self.project)
        self.assertEqual(finding.severity, 'HIGH')
        # A free-text column, because the model field is free text. A yes/no
        # here would store the literal string "yes" as the corrective action.
        self.assertEqual(finding.corrective_action_required,
                         'Hack off and re-cast the cover')
        self.assertTrue(finding.requires_reinspection)
        self.assertEqual(str(finding.resolution_deadline), '2026-10-01')

    def test_the_reinspection_flag_is_not_invented_from_the_action_text(self):
        """The column is independent: a described action with the flag left
        blank follows the model's own default rather than a rule the manual
        form does not apply."""
        self._import([[self.inspection.inspection_reference, 'Loose balustrade',
                       'Movement at first floor.', 'HIGH', 'SAFETY',
                       'Re-fix the balustrade to the slab edge', '', '']])
        finding = Finding.objects.get()
        self.assertFalse(finding.requires_reinspection)

    def test_the_batch_inspection_is_used_when_the_row_names_none(self):
        self._import([['', 'Loose balustrade', 'Movement at first floor.', 'HIGH',
                       'SAFETY', 'Re-fix the balustrade', '', '']],
                     inspection=self.inspection)
        self.assertEqual(Finding.objects.get().inspection, self.inspection)

    def test_a_finding_with_no_inspection_anywhere_is_refused(self):
        batch = self._import([['', 'Loose balustrade', 'Movement.', 'HIGH',
                               'SAFETY', 'Re-fix the balustrade', '', '']])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('belongs to an inspection',
                      batch.validation_errors[0]['message'])
        self.assertEqual(Finding.objects.count(), 0)

    def test_an_inspection_from_another_project_cannot_be_named(self):
        other = Project.objects.create(name='Other', status='ACTIVE')
        foreign = Inspection.objects.create(
            project=other, inspection_type='Foundation Inspection')
        batch = self._import([[foreign.inspection_reference, 'X', 'Y', 'HIGH',
                               'SAFETY', '', '', '']])
        self.assertEqual(batch.import_status, ImportBatch.STATUS_FAILED)
        self.assertIn('was not found in this project',
                      batch.validation_errors[0]['message'])


# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------

class TemplateTests(TestCase):
    def test_the_csv_template_headers_are_the_registry_columns(self):
        for record_type, entry in REGISTRY.items():
            content, filename, content_type = build_template(record_type, 'csv')
            header = content.decode('utf-8-sig').splitlines()[0]
            parsed = next(csv.reader([header]))
            self.assertEqual(parsed, list(entry.columns), record_type)
            self.assertEqual(content_type, 'text/csv')
            self.assertIn(record_type.lower(), filename)

    def test_the_template_round_trips_through_the_reader(self):
        """The file the client downloads is a file this module can read back.

        The header is fed through the reader to produce the canonical keys, and
        the registry's own ``required`` names must appear among them — that is
        the whole point of rendering the template from ``RecordType.columns``,
        and it is the one thing a hand-edited column list would break.
        """
        for record_type, entry in REGISTRY.items():
            content, _f, _c = build_template(record_type, 'csv')
            rows, skipped = read_rows(content, 'CSV')
            # Only the two sample rows, and they read cleanly.
            self.assertEqual(len(rows), 2, record_type)
            self.assertEqual(skipped, 0, record_type)
            keys = set(rows[0].data)
            for required in entry.required:
                self.assertIn(normalise_key(required), keys,
                              f'{record_type} requires {required}, which the '
                              'template does not render as a column')

    def test_the_json_template_names_its_record_type(self):
        content, filename, content_type = build_template('UPV', 'json')
        parsed = json.loads(content)
        self.assertEqual(parsed['record_type'], 'UPV')
        self.assertEqual(len(parsed['records']), 2)
        self.assertEqual(content_type, 'application/json')
        self.assertTrue(filename.endswith('.json'))

    def test_pdf_is_refused_naming_the_formats_that_work(self):
        with self.assertRaises(ValueError) as ctx:
            build_template('UPV', 'pdf')
        message = str(ctx.exception)
        self.assertIn('CSV', message)
        self.assertIn('JSON', message)

    def test_an_unknown_record_type_is_refused(self):
        with self.assertRaises(ValueError):
            build_template('MAGIC', 'csv')


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

def _request(user):
    """A request stand-in for service calls made outside a view."""
    from rest_framework.test import APIRequestFactory

    request = APIRequestFactory().post('/')
    request.user = user
    return request


@local_storage_settings
class ImportAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='api_import@nexucon.com', email='api_import@nexucon.com',
            password='Password123!', first_name='Amaka', last_name='Obi')
        self.project = Project.objects.create(name='API Import Site',
                                              status='ACTIVE')
        self.client.force_authenticate(self.user)

    def _upload(self, content, name='file.csv', record_type='GPR', **extra):
        body = {'file': _Upload(content, name), 'project': str(self.project.id),
                'record_type': record_type}
        body.update(extra)
        return self.client.post(reverse('import-upload'), body, format='multipart')

    def test_the_spec_routes_resolve(self):
        batch_id = '11111111-1111-1111-1111-111111111111'
        self.assertEqual(reverse('import-upload'), '/api/v1/import/upload/')
        self.assertEqual(reverse('import-validate', args=[batch_id]),
                         f'/api/v1/import/{batch_id}/validate/')
        self.assertEqual(reverse('import-commit', args=[batch_id]),
                         f'/api/v1/import/{batch_id}/commit/')
        self.assertEqual(reverse('import-status', args=[batch_id]),
                         f'/api/v1/import/{batch_id}/status/')
        self.assertEqual(reverse('import-template', args=['UPV']),
                         '/api/v1/import/templates/UPV/')

    def test_the_specs_exact_spellings_resolve_too(self):
        """The spec lists its routes without a trailing slash. A POST to the
        documented spelling must not 404."""
        batch_id = '11111111-1111-1111-1111-111111111111'
        self.assertEqual(reverse('import-validate-noslash', args=[batch_id]),
                         f'/api/v1/import/{batch_id}/validate')
        self.assertEqual(reverse('import-template-noslash', args=['UPV']),
                         '/api/v1/import/templates/UPV')

    def test_upload_returns_201_then_200_when_deduplicated(self):
        self.assertEqual(self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
                         .status_code, status.HTTP_201_CREATED)
        response = self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['deduplicated'])
        self.assertEqual(ImportBatch.objects.count(), 1)

    def test_upload_refuses_a_claim_that_contradicts_the_bytes(self):
        response = self._upload(b'[{"a": 1}]', import_type='CSV')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('contents are JSON', response.data['detail'])

    def test_upload_into_another_agencys_project_is_a_404(self):
        outsider = User.objects.create_user(
            username='outsider_import@nexucon.com',
            email='outsider_import@nexucon.com', password='Password123!')
        self.client.force_authenticate(outsider)
        response = self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_full_lifecycle_over_http(self):
        upload = self._upload(csv_bytes(
            ['SURVEY TITLE', 'DEPTH RANGE (M)'], [['Zone A', '2.5']]))
        batch_id = upload.data['id']

        validate = self.client.post(
            reverse('import-validate', args=[batch_id]), {}, format='json')
        self.assertEqual(validate.status_code, status.HTTP_200_OK)
        self.assertEqual(validate.data['import_status'], 'VALIDATED')
        self.assertTrue(validate.data['can_commit'])
        self.assertEqual(GPRSurvey.objects.count(), 0)

        commit = self.client.post(
            reverse('import-commit', args=[batch_id]), {}, format='json')
        self.assertEqual(commit.status_code, status.HTTP_200_OK)
        self.assertEqual(commit.data['import_status'], 'IMPORTED')
        self.assertEqual(GPRSurvey.objects.count(), 1)

        detail = self.client.get(reverse('import-status', args=[batch_id]))
        self.assertEqual(detail.data['import_status'], 'IMPORTED')
        self.assertEqual(detail.data['errors'], [])

    def test_committing_twice_over_http_is_a_409(self):
        upload = self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
        batch_id = upload.data['id']
        self.client.post(reverse('import-validate', args=[batch_id]), {},
                         format='json')
        self.client.post(reverse('import-commit', args=[batch_id]), {},
                         format='json')
        again = self.client.post(reverse('import-commit', args=[batch_id]), {},
                                 format='json')
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(GPRSurvey.objects.count(), 1)

    def test_a_failed_validation_reports_its_rows_over_http(self):
        upload = self._upload(csv_bytes(
            ['SURVEY TITLE', 'DEPTH RANGE (M)'],
            [['Zone A', '2.5'], ['Zone B', 'deep']]))
        batch_id = upload.data['id']
        response = self.client.post(
            reverse('import-validate', args=[batch_id]), {}, format='json')
        self.assertEqual(response.data['import_status'], 'FAILED')
        self.assertEqual(response.data['valid_record_count'], 1)
        self.assertEqual(response.data['invalid_record_count'], 1)
        self.assertEqual(response.data['errors'][0]['row'], 3)
        self.assertFalse(response.data['can_commit'])

    def test_another_inspectors_batch_is_a_404_not_a_403(self):
        """A 403 would confirm the batch exists, which is a fact that belongs
        to the person who uploaded it."""
        upload = self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
        batch_id = upload.data['id']

        other = User.objects.create_superuser(
            username='nosy_import@nexucon.com', email='nosy_import@nexucon.com',
            password='Password123!')
        self.client.force_authenticate(other)
        for name in ('import-validate', 'import-commit', 'import-status'):
            response = self.client.post(reverse(name, args=[batch_id]), {},
                                        format='json') if name != 'import-status' \
                else self.client.get(reverse(name, args=[batch_id]))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, name)

    def test_the_template_downloads(self):
        response = self.client.get(reverse('import-template', args=['UPV']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('attachment', response['Content-Disposition'])
        header = response.content.decode('utf-8-sig').splitlines()[0]
        self.assertEqual(next(csv.reader([header])),
                         list(REGISTRY['UPV'].columns))

    def test_a_pdf_template_is_refused_naming_csv_and_json(self):
        response = self.client.get(
            reverse('import-template', args=['UPV']), {'format': 'pdf'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('CSV', response.data['detail'])
        self.assertIn('JSON', response.data['detail'])

    def test_an_unknown_template_type_is_refused(self):
        response = self.client.get(reverse('import-template', args=['MAGIC']))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_record_types_lists_what_the_registry_holds(self):
        response = self.client.get(reverse('import-record-types'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({row['record_type'] for row in response.data},
                         set(REGISTRY))
        upv = next(row for row in response.data if row['record_type'] == 'UPV')
        self.assertEqual(upv['columns'], list(REGISTRY['UPV'].columns))
        self.assertTrue(upv['groups_consecutive_rows'])

    def test_batches_lists_only_the_callers_own(self):
        self._upload(csv_bytes(['SURVEY TITLE'], [['A']]))
        response = self.client.get(reverse('import-batch-list'))
        self.assertEqual(len(response.data), 1)

        other = User.objects.create_superuser(
            username='other_batches@nexucon.com',
            email='other_batches@nexucon.com', password='Password123!')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(reverse('import-batch-list')).data, [])

    def test_batches_rejects_an_unknown_status_filter(self):
        response = self.client.get(reverse('import-batch-list'),
                                   {'status': 'MAYBE'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_anonymous_caller_is_refused(self):
        self.client.force_authenticate(None)
        for name in ('import-batch-list', 'import-record-types'):
            response = self.client.get(reverse(name))
            self.assertIn(response.status_code,
                          (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_the_template_route_is_not_shadowed_by_the_uuid_routes(self):
        """The exact bug that `scans/urls.py` and `inspections/urls.py` both
        record as fixed: a literal segment swallowed by a converter route. The
        `uuid:` converter cannot match "templates", but the assertion is kept
        because the fix is one careless edit away from being undone."""
        response = self.client.get('/api/v1/import/templates/GPR')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('SURVEY TITLE', response.content.decode('utf-8-sig'))
