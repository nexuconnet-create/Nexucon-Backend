from django.contrib import admin
from .models import Agency, Inspector, Role, Profile

@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    pass

@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    pass

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    pass


@admin.register(Inspector)
class InspectorAdmin(admin.ModelAdmin):
    """Accreditations are issued here or through the API — never seeded.

    If this list is empty on a fresh deployment that is correct: nobody has
    issued a badge in the platform yet, and the API says so rather than
    inventing one.
    """

    list_display = ['badge_number', 'full_name', 'directorate',
                    'accreditation_status', 'effective_status',
                    'accreditation_expiry', 'issued_at']
    list_filter = ['accreditation_status', 'directorate']
    search_fields = ['badge_number', 'full_name', 'user__email', 'issued_by']
    readonly_fields = ['id', 'created_at', 'updated_at', 'effective_status']

    @admin.display(description='Effective status')
    def effective_status(self, obj):
        return obj.effective_status
