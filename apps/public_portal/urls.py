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

    # Public Transparency Gateway (Workstream D + Architecture §15) — approved data only
    path('transparency/overview/', views.PublicOverviewView.as_view(), name='public-transparency-overview'),
    path('transparency/projects/', views.PublicProjectsView.as_view(), name='public-projects'),
    path('transparency/projects/<str:slug_or_id>/',
         views.PublicProjectDetailView.as_view(), name='public-project-detail'),
    path('transparency/projects/<str:slug_or_id>/compliance/',
         views.PublicProjectComplianceView.as_view(), name='public-project-compliance'),
    path('transparency/projects/<str:slug_or_id>/inspections/',
         views.PublicProjectInspectionsView.as_view(), name='public-project-inspections'),
    path('transparency/projects/<str:slug_or_id>/documents/',
         views.PublicProjectDocumentsView.as_view(), name='public-project-documents'),
    path('transparency/notices/', views.PublicNoticesView.as_view(), name='public-notices'),
    path('transparency/verify/permit/<path:permit_number>/',
         views.PublicVerifyPermitView.as_view(), name='public-verify-permit'),
    path('transparency/verify/project/<path:permit_number>/',
         views.PublicVerifyPermitView.as_view(), name='public-verify-project'),
    path('transparency/map/projects/', views.PublicMapProjectsView.as_view(), name='public-map-projects'),
    path('transparency/violation-reports/',
         views.PublicViolationReportView.as_view(), name='public-violation-reports'),
    path('transparency/violation-report/',
         views.PublicViolationReportView.as_view(), name='public-violation-report'),

    # Direct aliases matching architecture specification (/api/v1/public/*)
    path('overview/', views.PublicOverviewView.as_view(), name='public-overview-direct'),
    path('search/', views.PublicProjectsView.as_view(), name='public-search-direct'),
    path('projects/', views.PublicProjectsView.as_view(), name='public-projects-direct'),
    path('projects/<str:slug_or_id>/', views.PublicProjectDetailView.as_view(), name='public-project-detail-direct'),
    path('projects/<str:slug_or_id>/compliance/', views.PublicProjectComplianceView.as_view(), name='public-compliance-direct'),
    path('projects/<str:slug_or_id>/inspections/', views.PublicProjectInspectionsView.as_view(), name='public-inspections-direct'),
    path('projects/<str:slug_or_id>/documents/', views.PublicProjectDocumentsView.as_view(), name='public-documents-direct'),
    path('notices/', views.PublicNoticesView.as_view(), name='public-notices-direct'),
    path('verify/permit/<path:permit_number>/', views.PublicVerifyPermitView.as_view(), name='public-verify-permit-direct'),
    path('verify/project/<path:permit_number>/', views.PublicVerifyPermitView.as_view(), name='public-verify-project-direct'),
    path('map/projects/', views.PublicMapProjectsView.as_view(), name='public-map-direct'),
    path('violations/report/', views.PublicViolationReportView.as_view(), name='public-violation-report-direct'),
] + router.urls

