"""Temporary: reproduce the wizard's "let the server decide" path."""
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.data_import.services import ImportService
from apps.data_import.tests import _Upload, local_storage_settings
from apps.projects.models import Project

User = get_user_model()
PATH = os.path.join(settings.BASE_DIR, '..', 'sample-data',
                    'pundit_bulk_10_elements_2_floors_v2.csv')


@local_storage_settings
class ReproTest(TestCase):
    def test_the_server_decides_the_type(self):
        user = User.objects.create_superuser(
            username='repro@x.com', email='repro@x.com', password='Password123!')
        project = Project.objects.create(name='Repro Site', status='ACTIVE')
        content = open(PATH, 'rb').read()

        batch, created = ImportService.upload(
            user=user, project=project,
            uploaded_file=_Upload(content, 'v2.csv'),
            record_type='')          # <- what "let the server decide" sends
        print('IMPORT_TYPE STORED:', repr(batch.import_type))
        print('RECORD_TYPE STORED:', repr(batch.record_type))

        ImportService.validate(batch)
        batch.refresh_from_db()
        print('STATUS :', batch.import_status)
        print('COUNTS :', batch.record_count, 'read /',
              batch.valid_record_count, 'valid /',
              batch.invalid_record_count, 'invalid')
        for error in (batch.validation_errors or [])[:3]:
            print('ERROR  :', error)
