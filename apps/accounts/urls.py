from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    CustomLoginView,
    UserRegistrationView,
    UserMeView,
    UserOnboardingView,
    LogoutView,
    SessionListView,
    RevokeSessionView,
    ChangePasswordView,
    ApiKeyViewSet,
    TwoFactorStatusView,
    TwoFactorSetupView,
    TwoFactorVerifyView,
    TwoFactorDisableView,
)

router = DefaultRouter()
router.register(r'api-keys', ApiKeyViewSet, basename='api-key')

urlpatterns = [
    path('', include(router.urls)),
    path('login/', CustomLoginView.as_view(), name='login'),
    path('token/', CustomLoginView.as_view(), name='token_obtain_pair'),
    path('register/', UserRegistrationView.as_view(), name='register'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', UserMeView.as_view(), name='me'),
    path('onboarding/', UserOnboardingView.as_view(), name='onboarding'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('sessions/', SessionListView.as_view(), name='sessions'),
    path('sessions/<uuid:session_id>/revoke/', RevokeSessionView.as_view(), name='revoke_session'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),

    # Two-factor authentication (TOTP) — plan §5 Week 6
    path('2fa/', TwoFactorStatusView.as_view(), name='two_factor_status'),
    path('2fa/setup/', TwoFactorSetupView.as_view(), name='two_factor_setup'),
    path('2fa/verify/', TwoFactorVerifyView.as_view(), name='two_factor_verify'),
    path('2fa/disable/', TwoFactorDisableView.as_view(), name='two_factor_disable'),
]
