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
from .services import (
    AnalyticsService, PerformanceAnalyticsService, StructuralRiskService,
    ProgressAnalyticsService, InspectionAnalyticsService, ComplianceAnalyticsService,
    IndustryAnalyticsService, FinancialAnalyticsService, AgencyAnalyticsService
)
from .permissions import CanViewIndustryAnalytics, CanViewFinancialAnalytics, CanExportReports

class PerformanceAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        filters = {
            'lga': request.query_params.get('lga'),
            'status': request.query_params.get('status')
        }
        data = PerformanceAnalyticsService.get_portfolio_performance(filters)
        return Response(data, status=status.HTTP_200_OK)


class StructuralRiskViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        data = StructuralRiskService.calculate_risk_index()
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='mitigate')
    def mitigate(self, request, pk=None):
        alert = RiskAssessmentAlert.objects.filter(pk=pk).first()
        if alert:
            alert.status = 'Mitigated'
            alert.save()
            return Response(RiskAssessmentAlertSerializer(alert).data, status=status.HTTP_200_OK)
        return Response({"message": "Risk alert marked as mitigated."}, status=status.HTTP_200_OK)


class ProgressAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        data = ProgressAnalyticsService.get_progress_data()
        return Response(data, status=status.HTTP_200_OK)


class InspectionAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        period = request.query_params.get('period', 'monthly')
        data = InspectionAnalyticsService.get_inspection_analytics(period)
        return Response(data, status=status.HTTP_200_OK)


class ComplianceAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        data = ComplianceAnalyticsService.get_compliance_analytics()
        return Response(data, status=status.HTTP_200_OK)


class IndustryAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated, CanViewIndustryAnalytics]

    def list(self, request):
        data = IndustryAnalyticsService.get_industry_analytics()
        return Response(data, status=status.HTTP_200_OK)


class FinancialAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated, CanViewFinancialAnalytics]

    def list(self, request):
        data = FinancialAnalyticsService.get_financial_analytics()
        return Response(data, status=status.HTTP_200_OK)


class AgencyPerformanceViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def list(self, request):
        data = AgencyAnalyticsService.get_agency_performance()
        return Response(data, status=status.HTTP_200_OK)


class GeneratedReportViewSet(viewsets.ModelViewSet):
    queryset = GeneratedReport.objects.all()
    serializer_class = GeneratedReportSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, CanExportReports]

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

    def create(self, request, *args, **kwargs):
        report = AnalyticsService.generate_report(request.data, request.user)
        return Response(GeneratedReportSerializer(report).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='download')
    def download(self, request, pk=None):
        report = self.get_object()
        AnalyticsService.log_audit(
            user=request.user,
            action="REPORT_DOWNLOADED",
            resource_id=report.id,
            new_state={"ref": report.report_reference, "format": report.format}
        )
        return Response({
            "report_reference": report.report_reference,
            "title": report.title,
            "format": report.format,
            "download_url": report.file_url or f"https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/reports/{report.report_reference}.pdf"
        }, status=status.HTTP_200_OK)


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


class ExecutiveAnalyticsViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['get'], url_path='executive-kpis')
    def executive_kpis(self, request):
        kpis = PerformanceAnalyticsService.get_portfolio_performance()
        return Response(kpis, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='financial-summary')
    def financial_summary(self, request):
        data = FinancialAnalyticsService.get_financial_analytics()
        return Response(data, status=status.HTTP_200_OK)
