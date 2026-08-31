import os
import django
import sys

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.scans.models import ScanSession
from apps.reports.services import ReportService

def main():
    try:
        session = ScanSession.objects.get(id='fd8623c8-e10f-4d42-a541-8c345a90745c')
        report = ReportService.generate_qaqc_report(session)
        print("Report generated successfully:", report)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
