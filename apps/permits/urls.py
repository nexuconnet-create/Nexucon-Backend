from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PermitViewSet

router = DefaultRouter()
router.register(r'', PermitViewSet, basename='permit')

urlpatterns = [
    path('', include(router.urls)),
]
