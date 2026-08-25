import os
from django.conf import settings
from .base import BaseEmailProvider
from .resend_provider import ResendEmailProvider
from .smtp_provider import SMTPEmailProvider

class EmailProviderFactory:
    """
    Factory for resolving the active transactional email provider.
    """
    @staticmethod
    def get_provider() -> BaseEmailProvider:
        backend_type = getattr(settings, 'EMAIL_PROVIDER_TYPE', os.environ.get('EMAIL_PROVIDER_TYPE', 'resend')).lower()
        if backend_type == 'smtp':
            return SMTPEmailProvider()
        return ResendEmailProvider()
