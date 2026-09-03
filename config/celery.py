import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

celery = app


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')


# ======================================================================
# Celery beat schedule (implementation plan §5 Weeks 2–4, 9)
#
#   * Trimble health checks     — plan §5 Week 1 integration alerting
#   * Trimble BIM background sync — plan §5 Week 2 (Celery/Redis sync)
#   * Evidence correlation pipeline — plan §5 Weeks 2–4
#   * Recurring anomaly detection — plan §5 Week 9 (compliance anomaly jobs)
# ======================================================================
app.conf.beat_schedule = {
    'trimble-health-checks': {
        'task': 'digital_eye.trimble_health_checks',
        'schedule': crontab(minute=0, hour='*/1'),  # hourly
    },
    'trimble-bim-sync': {
        'task': 'digital_eye.trimble_sync',
        'schedule': crontab(minute=30, hour='*/6'),  # every 6 hours
    },
    'evidence-correlate-projects': {
        'task': 'evidence.correlate_projects',
        'schedule': crontab(minute='*/15'),  # every 15 minutes
    },
    'evidence-recurring-anomaly-detection': {
        'task': 'evidence.detect_recurring_anomalies',
        'schedule': crontab(minute=0, hour=6),  # daily 06:00 UTC
    },
}
