from django.apps import AppConfig


class DataImportConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.data_import'
    verbose_name = 'Manual data import'
