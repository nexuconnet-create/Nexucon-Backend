import logging
from django.core.management.base import BaseCommand
from apps.scans.models import ScanSession, BIMAlignmentResult
from apps.scans.services import DataFusionService

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Retroactively generate compliance checks for all sessions with existing BIM alignments."

    def handle(self, *args, **options):
        alignments = BIMAlignmentResult.objects.all()
        generated_count = 0
        
        self.stdout.write(f"Found {alignments.count()} existing alignments. Generating compliance checks...")
        
        for alignment in alignments:
            session = alignment.session
            DataFusionService.generate_compliance_checks(session, alignment)
            generated_count += 1
            
        self.stdout.write(self.style.SUCCESS(f"Successfully generated compliance checks for {generated_count} sessions!"))
