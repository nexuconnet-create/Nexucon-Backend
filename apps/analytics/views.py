from rest_framework import viewsets, status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from .models import (
    GeneratedReport, DepartmentPerformanceMetric,
    OfficerPerformanceRecord, RiskAssessmentAlert
)
from .serializers import (
    GeneratedReportSerializer, DepartmentPerformanceMetricSerializer,
    OfficerPerformanceRecordSerializer, RiskAssessmentAlertSerializer
)
from .services import AnalyticsService

class GeneratedReportViewSet(viewsets.ModelViewSet):
    queryset = GeneratedReport.objects.all()
    serializer_class = GeneratedReportSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        fmt = self.request.query_params.get('format')
        rep_type = self.request.query_params.get('type')
        search = self.request.query_params.get('search')

        if fmt and fmt.lower() != 'all':
            qs = qs.filter(format__iexact=fmt)
        if rep_type and rep_type.lower() != 'all':
            qs = qs.filter(report_type__iexact=rep_type)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(report_reference__icontains=search) |
                Q(generated_by_name__icontains=search)
            )
        return qs

    def perform_create(self, serializer):
        report = AnalyticsService.generate_report(self.request.data, self.request.user)
        serializer.instance = report


class DepartmentPerformanceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DepartmentPerformanceMetric.objects.all()
    serializer_class = DepartmentPerformanceMetricSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        AnalyticsService.get_department_metrics()
        return DepartmentPerformanceMetric.objects.all()


class OfficerPerformanceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OfficerPerformanceRecord.objects.all()
    serializer_class = OfficerPerformanceRecordSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        AnalyticsService.get_officer_performance()
        return OfficerPerformanceRecord.objects.all()


class RiskAssessmentViewSet(viewsets.ModelViewSet):
    queryset = RiskAssessmentAlert.objects.all().select_related('project')
    serializer_class = RiskAssessmentAlertSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        AnalyticsService.get_risk_assessments()
        qs = super().get_queryset()
        level = self.request.query_params.get('level')
        if level and level.lower() != 'all':
            qs = qs.filter(risk_level__iexact=level)
        return qs

    @action(detail=True, methods=['post'], url_path='mitigate')
    def mitigate(self, request, pk=None):
        alert = self.get_object()
        alert.status = 'Mitigated'
        alert.save()
        return Response(RiskAssessmentAlertSerializer(alert).data, status=status.HTTP_200_OK)


class ExecutiveAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='executive-kpis')
    def executive_kpis(self, request):
        kpis = AnalyticsService.get_executive_kpis()
        return Response(kpis, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='financial-summary')
    def financial_summary(self, request):
        return Response({
            "total_revenue": "₦428,500,000",
            "permit_fees": "₦394,300,000",
            "enforcement_penalties": "₦34,200,000",
            "outstanding_dues": "₦18,400,000",
            "collection_efficiency": "96.4%",
            "monthly_breakdown": [
                {"month": "May", "revenue": 68000000},
                {"month": "Jun", "revenue": 82000000},
                {"month": "Jul", "revenue": 95000000},
                {"month": "Aug", "revenue": 110000000},
                {"month": "Sep", "revenue": 73500000}
            ]
        }, status=status.HTTP_200_OK)
