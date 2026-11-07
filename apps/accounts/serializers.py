import re
import random
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


def validate_username_value(value: str, exclude_user_id=None) -> str:
    """
    Validate a username for registration or profile update.
    Enforces presence, length (3-35), allowed characters, no '@' symbol, and uniqueness.
    """
    clean = (value or '').strip()
    if not clean:
        raise serializers.ValidationError("Username is required. No user can register without a username.")
    if len(clean) < 3:
        raise serializers.ValidationError("Username must be at least 3 characters long.")
    if len(clean) > 35:
        raise serializers.ValidationError("Username cannot exceed 35 characters.")
    if '@' in clean:
        raise serializers.ValidationError("Username cannot be an email address or contain '@'. Please choose a unique handle or auto-generate one.")
    if not re.match(r'^[a-zA-Z0-9_.-]+$', clean):
        raise serializers.ValidationError("Username can only contain letters, numbers, underscores (_), dots (.), and hyphens (-).")

    qs = User.objects.filter(username__iexact=clean)
    if exclude_user_id:
        qs = qs.exclude(id=exclude_user_id)
    if qs.exists():
        raise serializers.ValidationError("This username is already taken. Please choose another or click Auto-Generate.")
    return clean


def generate_unique_username(
    base_name: str = "",
    email: str = "",
    role: str = "",
    company_name: str = "",
    exclude_user_id=None
) -> str:
    """
    Generate a clean, available username based on user's name, company, role, or email prefix.
    """
    suggestions = generate_username_suggestions(
        base_name=base_name,
        email=email,
        role=role,
        company_name=company_name,
        exclude_user_id=exclude_user_id,
        count=1
    )
    return suggestions[0]


def generate_username_suggestions(
    base_name: str = "",
    email: str = "",
    role: str = "",
    company_name: str = "",
    exclude_user_id=None,
    count: int = 4
) -> list[str]:
    def _slug(text: str) -> str:
        s = re.sub(r'[^a-z0-9]+', '_', (text or '').strip().lower()).strip('_')
        return s[:22]

    name_parts = [re.sub(r'[^a-z0-9]', '', p.lower()) for p in (base_name or '').split() if re.sub(r'[^a-z0-9]', '', p.lower())]
    email_prefix = _slug((email or '').split('@')[0])
    comp_slug = _slug(company_name)
    role_slug = _slug(role)

    candidates = []
    if len(name_parts) >= 2:
        first, last = name_parts[0], name_parts[-1]
        candidates.extend([
            f"{first}_{last}",
            f"{first}.{last}",
            f"{first}{last}",
            f"{first[0]}_{last}",
        ])
    elif len(name_parts) == 1:
        first = name_parts[0]
        candidates.extend([
            first,
            f"{first}_nxc",
        ])

    if comp_slug:
        if name_parts:
            candidates.append(f"{name_parts[0]}_{comp_slug[:10]}")
        candidates.append(comp_slug)

    if email_prefix and len(email_prefix) >= 3:
        candidates.append(email_prefix)

    if role_slug and name_parts:
        short_role = 'insp' if 'inspector' in role_slug else ('gov' if 'agency' in role_slug or 'director' in role_slug else role_slug[:6])
        candidates.append(f"{short_role}_{name_parts[0]}")

    if not candidates:
        candidates = ["nexucon_user", "nxc_member", "nxc_official"]

    available = []
    seen = set()

    def _is_taken(u: str) -> bool:
        qs = User.objects.filter(username__iexact=u)
        if exclude_user_id:
            qs = qs.exclude(id=exclude_user_id)
        return qs.exists()

    # First check clean candidates
    for cand in candidates:
        c = cand[:30].strip('._-')
        if len(c) < 3:
            c = f"{c}_nxc"
        if c not in seen and not _is_taken(c):
            seen.add(c)
            available.append(c)
            if len(available) >= count:
                return available

    # Append numeric suffixes until we have enough suggestions
    base_seed = (candidates[0] if candidates else "nexucon_user")[:24].strip('._-')
    if len(base_seed) < 3:
        base_seed = "nxc_user"

    attempts = 0
    while len(available) < count and attempts < 40:
        attempts += 1
        suffix = random.randint(10, 999) if attempts > 3 else attempts + 1
        cand = f"{base_seed}{suffix}" if attempts % 2 == 0 else f"{base_seed}_{suffix}"
        if cand not in seen and not _is_taken(cand):
            seen.add(cand)
            available.append(cand)

    return available or [f"nxc_user_{random.randint(1000, 9999)}"]


class UserRegistrationSerializer(serializers.ModelSerializer):
    username = serializers.CharField(required=True, min_length=3, max_length=35)
    password = serializers.CharField(write_only=True, min_length=8)
    name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    stakeholder_type = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    company_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    registration_number = serializers.CharField(write_only=True, required=False, allow_blank=True)
    license_authority = serializers.CharField(write_only=True, required=False, allow_blank=True)
    license_number = serializers.CharField(write_only=True, required=False, allow_blank=True)
    country = serializers.CharField(write_only=True, required=False, allow_blank=True)
    state_region = serializers.CharField(write_only=True, required=False, allow_blank=True)
    office_address = serializers.CharField(write_only=True, required=False, allow_blank=True)
    
    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'first_name', 'last_name', 'phone_number', 'password',
            'name', 'stakeholder_type', 'role_name', 'company_name',
            'registration_number', 'license_authority', 'license_number',
            'country', 'state_region', 'office_address'
        )

    def validate_username(self, value):
        return validate_username_value(value)

    def validate_email(self, value):
        clean_email = value.strip().lower()
        # Rule 1: No replica email across Agency, Inspector, and Stakeholder
        if User.objects.filter(email__iexact=clean_email).exists():
            raise serializers.ValidationError("An account with this email address already exists. Please sign in instead.")

        from apps.government.models import Profile
        if Profile.objects.filter(user__email__iexact=clean_email).exists():
            raise serializers.ValidationError("This email belongs to an existing Government Agency or Inspector account. It cannot be used to create a Stakeholder account.")

        from apps.settings.models import UserInvitation
        if UserInvitation.objects.filter(email__iexact=clean_email).exists():
            raise serializers.ValidationError("This email is assigned to an Agency or Inspectorate credential and cannot be registered as a Stakeholder.")

        return clean_email

    def create(self, validated_data):
        # Extract extra stakeholder registration metadata
        name = validated_data.pop('name', '').strip()
        first_name = validated_data.get('first_name', '').strip()
        last_name = validated_data.get('last_name', '').strip()

        if name and not (first_name or last_name):
            parts = name.split(None, 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''

        stakeholder_type = (validated_data.pop('stakeholder_type', '') or '').lower().strip()
        role_name = validated_data.pop('role_name', '')
        company_name = validated_data.pop('company_name', '').strip()
        registration_number = validated_data.pop('registration_number', '').strip()
        license_authority = validated_data.pop('license_authority', '').strip() or 'COREN'
        license_number = validated_data.pop('license_number', '').strip()
        country = validated_data.pop('country', '').strip()
        state_region = validated_data.pop('state_region', '').strip()
        office_address = validated_data.pop('office_address', '').strip()

        raw_username = (validated_data.get('username') or '').strip()
        if not raw_username:
            raise serializers.ValidationError({'username': 'Username is required. No user can register without a username.'})

        user = User.objects.create_user(
            username=raw_username,
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=first_name,
            last_name=last_name,
            phone_number=validated_data.get('phone_number', ''),
        )

        full_name = f"{first_name} {last_name}".strip() or user.email
        hq_loc = office_address or state_region or country or 'Nigeria'

        # Auto-create or link corresponding stakeholder entity
        if stakeholder_type:
            from apps.stakeholders.models import (
                Developer, Contractor, Consultant, LicensedProfessional, generate_lic_id
            )
            if stakeholder_type in ['client', 'developer']:
                Developer.objects.create(
                    user=user,
                    name=company_name or full_name,
                    status='Active',
                    hq_location=hq_loc,
                    primary_contact_name=full_name,
                    primary_contact_email=user.email,
                    primary_contact_phone=user.phone_number or '',
                    is_active=True,
                )
            elif stakeholder_type == 'contractor':
                Contractor.objects.create(
                    user=user,
                    name=company_name or full_name,
                    company_name=company_name,
                    registration_number=registration_number,
                    license_number=license_number,
                    contractor_type='General Contractor',
                    status='Prequalified',
                    license_status='Active' if license_number else 'Pending',
                    is_active=True,
                )
            elif stakeholder_type == 'professional':
                LicensedProfessional.objects.create(
                    user=user,
                    license_id=license_number or generate_lic_id(),
                    name=full_name,
                    role_title='Licensed Professional',
                    firm_name=company_name or 'Independent Practice',
                    license_authority=license_authority,
                    license_status='Active',
                    is_verified=True,
                )
            elif stakeholder_type == 'consultant':
                Consultant.objects.create(
                    user=user,
                    name=company_name or full_name,
                    company_name=company_name,
                    registration_number=registration_number,
                    specialty='Advisory Consultant',
                    status='Active',
                    hq_location=hq_loc,
                    is_active=True,
                )

        return user


class UserMeSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    role_name = serializers.SerializerMethodField()
    agency_code = serializers.SerializerMethodField()
    stakeholder_profile = serializers.SerializerMethodField()
    is_onboarded = serializers.SerializerMethodField()
    needs_username_update = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'first_name', 'last_name', 'phone_number',
            'is_verified', 'is_onboarded', 'needs_username_update', 'role_name', 'agency_code',
            'stakeholder_profile', 'permissions'
        )

    def get_needs_username_update(self, obj):
        uname = (getattr(obj, 'username', '') or '').strip()
        uemail = (getattr(obj, 'email', '') or '').strip()
        if not uname or '@' in uname or (uemail and uname.lower() == uemail.lower()):
            return True
        return False

    def get_is_onboarded(self, obj):
        if getattr(obj, 'is_onboarded', False):
            return True
        if hasattr(obj, 'government_profile') and obj.government_profile:
            return True
        from apps.stakeholders.models import Developer, Contractor, Consultant, LicensedProfessional
        if Developer.objects.filter(user=obj).exclude(status='Pending').exists():
            return True
        if Contractor.objects.filter(user=obj).exclude(status='Pending').exists():
            return True
        if Consultant.objects.filter(user=obj).exclude(status='Pending').exists():
            return True
        if LicensedProfessional.objects.filter(user=obj).exists():
            return True
        return False

    def get_stakeholder_profile(self, obj):
        from apps.stakeholders.models import Developer, Contractor, Consultant, LicensedProfessional
        dev = Developer.objects.filter(user=obj).first()
        if dev:
            return {
                'type': 'developer',
                'id': str(dev.id),
                'identifier': dev.developer_id,
                'name': dev.name,
                'status': dev.status,
                'hq_location': dev.hq_location,
                'portfolio_value': dev.portfolio_value,
                'active_projects_count': dev.active_projects_count,
            }
        con = Contractor.objects.filter(user=obj).first()
        if con:
            return {
                'type': 'contractor',
                'id': str(con.id),
                'identifier': con.contractor_id,
                'name': con.name,
                'status': con.status,
                'license_number': con.license_number,
                'compliance_score': con.compliance_score,
                'active_permits': con.active_permits,
            }
        cns = Consultant.objects.filter(user=obj).first()
        if cns:
            return {
                'type': 'consultant',
                'id': str(cns.id),
                'identifier': cns.consultant_id,
                'name': cns.name,
                'specialty': cns.specialty,
                'status': cns.status,
                'hq_location': cns.hq_location,
                'active_roles_count': cns.active_roles_count,
            }
        lic = LicensedProfessional.objects.filter(user=obj).first()
        if lic:
            return {
                'type': 'professional',
                'id': str(lic.id),
                'identifier': lic.license_id,
                'name': lic.name,
                'firm_name': lic.firm_name,
                'role_title': lic.role_title,
                'license_authority': lic.license_authority,
                'license_status': lic.license_status,
                'is_verified': lic.is_verified,
            }
        return None

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

        # Stakeholders get dedicated action permissions
        from apps.stakeholders.models import Developer, Contractor, Consultant, LicensedProfessional
        if (
            Developer.objects.filter(user=obj).exists() or
            Contractor.objects.filter(user=obj).exists() or
            Consultant.objects.filter(user=obj).exists() or
            LicensedProfessional.objects.filter(user=obj).exists()
        ):
            return [
                'stakeholder.view',
                'inspections.request',
                'inspections.view',
                'milestones.view',
                'milestones.signoff',
                'financials.view',
                'financials.pay',
                'messages.send',
                'meetings.join'
            ]
        return []

    def get_role_name(self, obj):
        if hasattr(obj, 'government_profile') and obj.government_profile and obj.government_profile.role:
            return obj.government_profile.role.name
        if hasattr(obj, 'government_profile') and obj.government_profile:
            return 'Agency Head'
        if obj.is_superuser:
            return 'Director'

        from apps.settings.models import UserInvitation
        inv = UserInvitation.objects.filter(email__iexact=obj.email).first()
        if inv and inv.role:
            return inv.role

        from apps.stakeholders.models import Developer, Contractor, Consultant, LicensedProfessional
        if Developer.objects.filter(user=obj).exists():
            return 'Stakeholder: Developer'
        if Contractor.objects.filter(user=obj).exists():
            return 'Stakeholder: Contractor'
        if Consultant.objects.filter(user=obj).exists():
            return 'Stakeholder: Consultant'
        if LicensedProfessional.objects.filter(user=obj).exists():
            return 'Stakeholder: Professional'
        return 'Client'
        
    def get_agency_code(self, obj):
        if hasattr(obj, 'government_profile') and obj.government_profile and obj.government_profile.agency:
            return obj.government_profile.agency.code
        return None


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    username = serializers.CharField(required=False, allow_blank=True, write_only=True)

    default_error_messages = {
        'no_active_account': 'Incorrect username/email or password.'
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.username_field in self.fields:
            self.fields[self.username_field].required = False
            self.fields[self.username_field].allow_blank = True

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Add custom claims
        token['email'] = user.email
        token['username'] = user.username
        if hasattr(user, 'government_profile') and user.government_profile and user.government_profile.role:
            token['role'] = user.government_profile.role.name
            token['permissions'] = user.government_profile.role.permissions
        elif user.is_superuser:
            token['role'] = 'Director'
            token['permissions'] = ['admin']
        else:
            from apps.stakeholders.models import Developer, Contractor, Consultant, LicensedProfessional
            if Developer.objects.filter(user=user).exists():
                token['role'] = 'Stakeholder: Developer'
            elif Contractor.objects.filter(user=user).exists():
                token['role'] = 'Stakeholder: Contractor'
            elif Consultant.objects.filter(user=user).exists():
                token['role'] = 'Stakeholder: Consultant'
            elif LicensedProfessional.objects.filter(user=user).exists():
                token['role'] = 'Stakeholder: Professional'
            else:
                token['role'] = 'Client'
            token['permissions'] = []
        return token

    def validate(self, attrs):
        # Accept either 'username' or 'email' as login identifier
        raw_identifier = (
            attrs.get('username')
            or attrs.get(self.username_field)
            or (self.initial_data.get('username') if hasattr(self, 'initial_data') and isinstance(self.initial_data, dict) else '')
            or (self.initial_data.get('email') if hasattr(self, 'initial_data') and isinstance(self.initial_data, dict) else '')
            or ''
        ).strip()

        if not raw_identifier:
            raise serializers.ValidationError({'username': 'Username or email is required to sign in.'})

        user = User.objects.filter(
            Q(username__iexact=raw_identifier) | Q(email__iexact=raw_identifier)
        ).first()
        if user:
            attrs[self.username_field] = getattr(user, self.username_field)
        else:
            attrs[self.username_field] = raw_identifier

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
