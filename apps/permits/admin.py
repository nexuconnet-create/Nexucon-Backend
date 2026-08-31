from django.contrib import admin
from .models import Permit

@admin.register(Permit)
class PermitAdmin(admin.ModelAdmin):
    pass

