from .base import *
DEBUG = True

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
}
