from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()

class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    
    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'phone_number', 'password')

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            phone_number=validated_data.get('phone_number', ''),
        )
        return user


class UserMeSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    agency_code = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'phone_number', 'is_verified', 'role_name', 'agency_code', 'permissions')

    def get_permissions(self, obj):
        default_agency_perms = [
            'admin',
            'projects.view', 'projects.create', 'projects.edit', 'projects.delete',
            'applications.view', 'applications.create', 'applications.approve', 'applications.reject',
            'inspections.view', 'inspections.create', 'inspections.update', 'inspections.delete',
            'analytics.view_industry', 'all.delete', 'permits.create', 'permits.read', 'permits.update', 'permits.delete'
        ]
        if hasattr(obj, 'government_profile') and obj.government_profile and obj.government_profile.role:
            perms = list(obj.government_profile.role.permissions or [])
            if obj.government_profile.role.name in ['Agency Head', 'agency_head', 'Director', 'admin']:
                for p in default_agency_perms:
                    if p not in perms:
                        perms.append(p)
            return perms
        if hasattr(obj, 'government_profile'):
            return default_agency_perms
        # Non-government users (client developers, stakeholders) get no
        # elevated permissions — their access is scoped per-request by the
        # helpers in common.permissions.
        return []

    def get_role_name(self, obj):
        if hasattr(obj, 'government_profile') and obj.government_profile and obj.government_profile.role:
            return obj.government_profile.role.name
        if hasattr(obj, 'government_profile') and obj.government_profile:
            return 'Agency Head'
        if obj.is_superuser:
            return 'Director'
        return 'Client'
        
    def get_agency_code(self, obj):
        if hasattr(obj, 'government_profile') and obj.government_profile and obj.government_profile.agency:
            return obj.government_profile.agency.code
        return None


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        'no_active_account': 'Incorrect email or password.'
    }

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Add custom claims
        token['email'] = user.email
        if hasattr(user, 'government_profile') and user.government_profile and user.government_profile.role:
            token['role'] = user.government_profile.role.name
            token['permissions'] = user.government_profile.role.permissions
        else:
            # Non-government users (client developers, stakeholders) get NO
            # elevated claims — authorization is decided per-request from the
            # role-scoped helpers in common.permissions, never from this token.
            token['role'] = 'Client'
            token['permissions'] = []
        if user.is_superuser:
            token['role'] = 'Director'
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        # Add extra responses here
        data.update({'user': UserMeSerializer(self.user).data})
        return data


class ApiKeySerializer(serializers.ModelSerializer):
    """
    Read serializer. Never exposes the full secret — only the masked preview.
    The plaintext key is returned once, by the create endpoint.
    """
    masked_key = serializers.CharField(read_only=True)

    class Meta:
        from .models import ApiKey
        model = ApiKey
        fields = [
            'id', 'name', 'masked_key', 'is_active',
            'last_used_at', 'revoked_at', 'created_at',
        ]
        read_only_fields = ['id', 'masked_key', 'last_used_at', 'revoked_at', 'created_at']
