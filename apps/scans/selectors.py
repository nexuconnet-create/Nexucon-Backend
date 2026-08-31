from django.shortcuts import get_object_or_404
from apps.scans.models import ScanSession, Defect, ThermalAnomaly, BIMAlignmentResult

class ScanSelector:
    """
    Read-only query selector layer for Scan sessions and related resources.
    """
    @staticmethod
    def get_session(session_id: str) -> ScanSession:
        return get_object_or_404(ScanSession.objects.select_related('project', 'metadata'), id=session_id)

    @staticmethod
    def get_project_scans(project_id: str):
        return ScanSession.objects.select_related('project', 'metadata').filter(project__id=project_id)

    @staticmethod
    def get_defects(session_id: str):
        return Defect.objects.select_related('session').filter(session__id=session_id)

    @staticmethod
    def get_thermal_anomalies(session_id: str):
        return ThermalAnomaly.objects.select_related('session').filter(session__id=session_id)

    @staticmethod
    def get_bim_alignment(session_id: str):
        return BIMAlignmentResult.objects.select_related('session').filter(session__id=session_id).first()
