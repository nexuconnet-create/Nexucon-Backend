import logging
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, serializers
from django.shortcuts import get_object_or_404
from .models import ArchivedReport, ReportVersion
from .serializers import ReportVersionSerializer
from drf_spectacular.utils import extend_schema

logger = logging.getLogger(__name__)

class ReportVersionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(summary="List versions for an archived report", tags=["Reports"], responses={200: ReportVersionSerializer(many=True)})
    def get(self, request, report_id):
        report = get_object_or_404(ArchivedReport, id=report_id)
        versions = report.versions.all()
        return Response(ReportVersionSerializer(versions, many=True).data)

    @extend_schema(summary="Create a new report version with AI feedback", tags=["Reports"], responses={201: ReportVersionSerializer})
    def post(self, request, report_id):
        report = get_object_or_404(ArchivedReport, id=report_id)
        version_string = request.data.get('version_string', '1.0')
        ai_feedback_context = request.data.get('ai_feedback_context', '')
        
        version = ReportVersion.objects.create(
            archived_report=report,
            version_string=version_string,
            ai_feedback_context=ai_feedback_context,
            created_by=request.user
        )
        return Response(ReportVersionSerializer(version).data, status=status.HTTP_201_CREATED)
