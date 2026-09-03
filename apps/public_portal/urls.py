from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()

urlpatterns = [
    # Client Portal (plan §5 Week 8) — client-scoped, strict data segregation
    path('client/projects/', views.ClientProjectsView.as_view(), name='client-projects'),
    path('client/projects/<uuid:project_id>/milestones/',
         views.ClientMilestonesView.as_view(), name='client-milestones'),
    path('client/inspections/', views.ClientInspectionsView.as_view(), name='client-inspections'),
    path('client/ncrs/', views.ClientNCRFeedView.as_view(), name='client-ncrs'),
    path('client/documents/', views.ClientDocumentsView.as_view(), name='client-documents'),
    path('client/documents/<uuid:document_id>/download/',
         views.ClientDocumentDownloadView.as_view(), name='client-document-download'),
    path('client/messages/', views.ClientMessagesView.as_view(), name='client-messages'),

    # Public Transparency Gateway (Workstream D) — approved data only
    path('transparency/projects/', views.PublicProjectsView.as_view(), name='public-projects'),
    path('transparency/projects/<uuid:project_id>/',
         views.PublicProjectDetailView.as_view(), name='public-project-detail'),
    path('transparency/violation-reports/',
         views.PublicViolationReportView.as_view(), name='public-violation-report'),
] + router.urls
