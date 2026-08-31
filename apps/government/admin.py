from django.contrib import admin
from .models import Agency, Role, Profile

@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    pass

@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    pass

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    pass

