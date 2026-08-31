from django.contrib import admin
from .models import EmergencyEvent, ResponderDispatch

@admin.register(EmergencyEvent)
class EmergencyEventAdmin(admin.ModelAdmin):
    pass

@admin.register(ResponderDispatch)
class ResponderDispatchAdmin(admin.ModelAdmin):
    pass

