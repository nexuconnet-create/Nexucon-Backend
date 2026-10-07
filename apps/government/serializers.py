from rest_framework import serializers
from .models import Agency, District, Inspector, Profile, Role


class DistrictSerializer(serializers.ModelSerializer):
    """An operational zone — the state's zonal jurisdiction (Government UI).

    The two counts are the deciding information before a zone is retired: a
    district with projects or staff still attached is not an unused row, and
    retiring it changes what those officers can see. They are annotated by the
    list view and counted directly otherwise, so the value is the real count in
    either case rather than an estimate the client has to trust.

    ``boundary_polygon`` stays a pass-through field. Boundaries are supplied by
    the Survey/GIS client (plan §7) and are never invented by the platform, so
    the API accepts and returns real geometry without this serializer implying
    the UI can produce one.
    """

    project_count = serializers.SerializerMethodField()
    staff_count = serializers.SerializerMethodField()

    class Meta:
        model = District
        fields = [
            'id', 'name', 'code', 'state_region', 'description',
            'boundary_polygon', 'office_address',
            'lead_officer_name', 'lead_officer_email',
            'is_active', 'project_count', 'staff_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_project_count(self, obj):
        count = getattr(obj, 'project_count', None)
        return count if count is not None else obj.projects.count()

    def get_staff_count(self, obj):
        count = getattr(obj, 'staff_count', None)
        return count if count is not None else obj.staff.count()

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A zone name is required.')
        return value

    def validate_code(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A zone code is required.')
        return value


class AgencyProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agency
        fields = (
            'id', 'name', 'code', 'description',
            'country', 'state_region', 'city', 'department_name',
            'primary_role', 'jurisdiction_level', 'project_scale_focus', 'collaboration_preference',
            'short_name', 'official_email', 'main_phone', 'physical_address', 'timezone', 'measurement_system'
        )


class InspectorSerializer(serializers.ModelSerializer):
    """Inspector accreditation (Inspector PWA Module 1).

    ``effective_status`` is read-only and derived: it applies the expiry date
    to the stored status without writing anything back, so the column and the
    date can never disagree.
    """

    effective_status = serializers.CharField(read_only=True)
    is_valid = serializers.BooleanField(read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_full_name = serializers.SerializerMethodField()

    class Meta:
        model = Inspector
        fields = [
            'id', 'user', 'user_email', 'user_full_name',
            'badge_number', 'full_name', 'directorate',
            'accreditation_status', 'effective_status', 'is_valid',
            'accreditation_expiry', 'issued_by', 'issued_at',
            'suspended_at', 'suspension_reason', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_user_full_name(self, obj):
        """The account's current name, shown *alongside* the accredited name.

        Both are returned so a reviewer can see when a badge was issued to a
        name the account no longer uses. Collapsing them into one field would
        hide exactly the discrepancy this snapshot exists to expose.
        """
        name = obj.user.get_full_name()
        return name or None

    def validate_badge_number(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A badge number is required.')
        return value

    def validate(self, attrs):
        expiry = attrs.get('accreditation_expiry',
                           getattr(self.instance, 'accreditation_expiry', None))
        status_value = attrs.get(
            'accreditation_status',
            getattr(self.instance, 'accreditation_status', Inspector.STATUS_ACTIVE))
        if expiry and status_value == Inspector.STATUS_ACTIVE:
            from django.utils import timezone
            if expiry < timezone.localdate():
                raise serializers.ValidationError({
                    'accreditation_expiry':
                        'This expiry date is already in the past. Record the '
                        'accreditation as EXPIRED or REVOKED, or correct the date '
                        '— an accreditation cannot be created already lapsed and '
                        'still marked ACTIVE.',
                })
        return attrs
