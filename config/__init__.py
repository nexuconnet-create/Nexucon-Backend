from .celery import app as celery_app, celery

__all__ = ('celery_app', 'celery')
