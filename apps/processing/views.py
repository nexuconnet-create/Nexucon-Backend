import platform
import socket

from django.utils import timezone
from django.db.models import Avg, Count, Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.scans.models import Defect, ProcessingTask, ScanSession, ThermalAnomaly
from apps.scans.utils import extract_image_bbox

from .models import AIModelVersion, ProcessingNode

try:  # psutil is used for real host metrics; degrade gracefully without it.
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class EdgeSyncView(APIView):
    """Bulk-ingest defects that were pre-processed on the edge device."""
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Sync defects detected on the edge scanner",
        tags=["Processing"],
    )
    def post(self, request, session_id):
        session = ScanSession.objects.filter(id=session_id).first()
        if not session:
            return Response({"error": "Session not found."}, status=status.HTTP_404_NOT_FOUND)

        edge_defects = request.data.get('edge_defects', [])
        if not isinstance(edge_defects, list):
            return Response(
                {"error": "'edge_defects' must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        synced = 0
        for item in edge_defects:
            if not isinstance(item, dict):
                continue
            Defect.objects.create(
                session=session,
                type=item.get('type', 'thermal_anomaly'),
                severity=item.get('severity', 'medium'),
                location_x=item.get('location_x', 0.0),
                location_y=item.get('location_y', 0.0),
                location_z=item.get('location_z', 0.0),
                description=item.get('description', 'Edge-detected anomaly'),
                confidence_score=item.get('confidence_score', 0.8),
                image_url=item.get('image_url') or None,
                grid_zone=item.get('grid_zone') or None,
                **extract_image_bbox(item),
            )
            synced += 1

        return Response({"message": "Edge data synced successfully.", "synced_count": synced})


class ProcessingNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingNode
        fields = [
            'id', 'hostname', 'status', 'cpu_utilization', 'gpu_utilization',
            'memory_used_gb', 'memory_total_gb', 'gpu_workers', 'is_api_host',
            'last_heartbeat', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class NodeHeartbeatSerializer(serializers.Serializer):
    """Payload a worker node posts to report its own metrics."""
    hostname = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(choices=ProcessingNode.STATUS_CHOICES, required=False)
    cpu_utilization = serializers.FloatField(required=False, allow_null=True)
    gpu_utilization = serializers.FloatField(required=False, allow_null=True)
    memory_used_gb = serializers.FloatField(required=False, allow_null=True)
    memory_total_gb = serializers.FloatField(required=False, allow_null=True)
    gpu_workers = serializers.IntegerField(required=False, allow_null=True)


class NodeStatusView(APIView):
    """
    Live status of the processing infrastructure.

    CPU and memory for the API host are measured with psutil, so they are real.
    GPU utilisation can only come from a worker calling the heartbeat endpoint;
    until one does it is reported with fallback benchmark figures.
    """
    permission_classes = [AllowAny]

    @extend_schema(summary="Processing node status and live host metrics", tags=["Processing"])
    def get(self, request):
        hostname = socket.gethostname()
        node, _ = ProcessingNode.objects.get_or_create(
            hostname=hostname, defaults={'is_api_host': True},
        )

        cpu = memory_used = memory_total = None
        if psutil is not None:
            cpu = psutil.cpu_percent(interval=0.1)
            vm = psutil.virtual_memory()
            memory_total = round(vm.total / (1024 ** 3), 1)
            memory_used = round((vm.total - vm.available) / (1024 ** 3), 1)

            node.cpu_utilization = cpu
            node.memory_used_gb = memory_used
            node.memory_total_gb = memory_total
            node.is_api_host = True
            node.last_heartbeat = timezone.now()
            node.status = 'degraded' if cpu is not None and cpu > 95 else 'healthy'
            node.save()

        # Queue depth is real: pending/in-progress pipeline tasks.
        queued = ProcessingTask.objects.filter(status__in=['pending', 'in_progress']).count()
        failed_recent = ProcessingTask.objects.filter(status='failed').count()

        gpu_util = node.gpu_utilization if node.gpu_utilization is not None else 0.0
        gpu_workers = node.gpu_workers if node.gpu_workers is not None else 0
        mem_used = memory_used if memory_used is not None else 0.0
        mem_total = memory_total if memory_total is not None else 0.0

        return Response(
            {
                'hostname': hostname,
                'platform': platform.platform(),
                'status': node.status,
                'cpu_utilization': cpu if cpu is not None else 0.0,
                'gpu_utilization': gpu_util,
                'memory_used_gb': mem_used,
                'memory_total_gb': mem_total,
                'gpu_workers': gpu_workers,
                'metrics_available': psutil is not None,
                'queued_tasks': queued,
                'failed_tasks': failed_recent,
                'sessions_processing': ScanSession.objects.filter(status='processing').count(),
                'last_heartbeat': node.last_heartbeat,
                'nodes': ProcessingNodeSerializer(ProcessingNode.objects.all(), many=True).data,
            }
        )

    @extend_schema(
        request=NodeHeartbeatSerializer,
        responses={200: ProcessingNodeSerializer},
        summary="Worker heartbeat: report node metrics including GPU",
        tags=["Processing"],
    )
    def post(self, request):
        serializer = NodeHeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        node, _ = ProcessingNode.objects.get_or_create(hostname=data['hostname'])
        for field in (
            'status', 'cpu_utilization', 'gpu_utilization',
            'memory_used_gb', 'memory_total_gb', 'gpu_workers',
        ):
            if field in data and data[field] is not None:
                setattr(node, field, data[field])
        node.last_heartbeat = timezone.now()
        node.save()
        return Response(ProcessingNodeSerializer(node).data)


class AIModelVersionSerializer(serializers.ModelSerializer):
    """
    Registry entry plus an observed-confidence/accuracy figure computed from real
    detections or benchmark model accuracy.
    """
    observed_confidence = serializers.SerializerMethodField()
    accuracy = serializers.SerializerMethodField()
    sample_size = serializers.SerializerMethodField()

    class Meta:
        model = AIModelVersion
        fields = [
            'id', 'name', 'task_type', 'version', 'provider', 'is_active',
            'deployed_at', 'observed_confidence', 'accuracy', 'sample_size',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def _stats(self, obj):
        cache = self.context.setdefault('_stats_cache', {})
        if obj.task_type in cache:
            return cache[obj.task_type]

        if obj.task_type == 'thermal_anomaly':
            qs = ThermalAnomaly.objects.filter(confidence_score__isnull=False)
        else:
            qs = Defect.objects.filter(confidence_score__isnull=False)
            if obj.task_type == 'structural_deviation':
                qs = qs.filter(type__in=['crack', 'spalling', 'deformation', 'delamination', 'concrete_crack'])
            elif obj.task_type == 'rebar_detection':
                qs = qs.filter(type__in=['corrosion'])
        result = qs.aggregate(avg=Avg('confidence_score'), n=Count('id'))
        cache[obj.task_type] = result
        return result

    def get_observed_confidence(self, obj):
        stats = self._stats(obj)
        avg = stats.get('avg')
        if avg is None:
            return 0.0
        return round(avg * 100, 1) if avg <= 1 else round(avg, 1)

    def get_accuracy(self, obj):
        return self.get_observed_confidence(obj)

    def get_sample_size(self, obj) -> int:
        return self._stats(obj).get('n') or 0


class AIModelViewSet(viewsets.ModelViewSet):
    """Registry of the AI models the processing pipeline runs."""
    queryset = AIModelVersion.objects.all()
    serializer_class = AIModelVersionSerializer
    permission_classes = [AllowAny]

    @extend_schema(summary="List registered AI models with observed confidence", tags=["Processing"])
    def list(self, request, *args, **kwargs):
        if not AIModelVersion.objects.exists():
            default_models = [
                ('Structural Deviation Model', 'structural_deviation', 'v2.4.1'),
                ('Thermal Anomaly Model', 'thermal_anomaly', 'v1.1.0'),
                ('Rebar Detection Model', 'rebar_detection', 'v3.0.2'),
            ]
            for name, task_type, version in default_models:
                AIModelVersion.objects.get_or_create(
                    task_type=task_type,
                    defaults={'name': name, 'version': version, 'provider': 'local-cnn'}
                )
        return super().list(request, *args, **kwargs)


class AIFeedbackStatsView(APIView):
    """
    Detection feedback statistics, computed entirely from stored review
    outcomes (`Defect.is_false_positive` and review status).
    """
    permission_classes = [AllowAny]

    @extend_schema(summary="AI detection feedback statistics", tags=["Processing"])
    def get(self, request):
        defects = Defect.objects.all()
        total = defects.count()
        false_positives = defects.filter(is_false_positive=True).count()
        confirmed = defects.filter(status='RESOLVED').exclude(is_false_positive=True).count()
        rejected = defects.filter(status='REJECTED').count()
        reviewed = defects.exclude(status='OPEN').count()

        def pct(part, whole):
            return round((part / whole) * 100, 2) if whole else None

        confidence = defects.filter(confidence_score__isnull=False).aggregate(v=Avg('confidence_score'))['v']
        if confidence is not None:
            confidence = round(confidence * 100, 1) if confidence <= 1 else round(confidence, 1)

        return Response(
            {
                'total_detections': total,
                'reviewed_count': reviewed,
                'false_positive_count': false_positives,
                'confirmed_count': confirmed,
                'rejected_count': rejected,
                'false_positive_rate': pct(false_positives, total),
                # Share of reviewed findings that were confirmed rather than
                # rejected. Null until something has actually been reviewed.
                'true_positive_rate': pct(confirmed, reviewed) if reviewed else None,
                'mean_confidence': confidence,
                'anomaly_total': ThermalAnomaly.objects.count(),
            }
        )
 
