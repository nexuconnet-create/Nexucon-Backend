from django.contrib import admin
from .models import BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation

@admin.register(BIMModel)
class BIMModelAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMModelVersion)
class BIMModelVersionAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMClash)
class BIMClashAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMAnnotation)
class BIMAnnotationAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMProgressValidation)
class BIMProgressValidationAdmin(admin.ModelAdmin):
    pass

