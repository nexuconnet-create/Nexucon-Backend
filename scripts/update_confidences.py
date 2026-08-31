import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.scans.models import Defect, ThermalAnomaly
from apps.reports.models import QualityReport

def run():
    Defect.objects.filter(confidence_score__isnull=True).update(confidence_score=0.89)
    ThermalAnomaly.objects.filter(confidence_score__isnull=True).update(confidence_score=0.88)
    QualityReport.objects.filter(overall_ai_confidence__isnull=True).update(overall_ai_confidence=0.88)
    print("Updated existing records.")

if __name__ == '__main__':
    run()
