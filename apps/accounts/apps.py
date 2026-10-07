from django.apps import AppConfig

class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'

    def ready(self):
        # Registers the OpenAPI security schemes for this app's two custom
        # authenticators. drf-spectacular discovers an extension by importing
        # the module that defines it, and nothing else imports this one — so
        # without this line every operation in the schema warns that its
        # authenticator could not be resolved. See the module's docstring.
        from . import schema_extensions  # noqa: F401
