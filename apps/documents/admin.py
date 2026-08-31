from django.contrib import admin
from .models import DocumentFolder, Document, Version, Approval, DocumentTemplate

@admin.register(DocumentFolder)
class DocumentFolderAdmin(admin.ModelAdmin):
    pass

@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    pass

@admin.register(Version)
class VersionAdmin(admin.ModelAdmin):
    pass

@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    pass

@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    pass

