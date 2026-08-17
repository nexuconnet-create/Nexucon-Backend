from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import CustomLoginView, UserRegistrationView, UserMeView, UserOnboardingView, LogoutView, SessionListView, RevokeSessionView, ChangePasswordView

urlpatterns = [
    path('login/', CustomLoginView.as_view(), name='login'),
    path('register/', UserRegistrationView.as_view(), name='register'),
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', UserMeView.as_view(), name='me'),
    path('onboarding/', UserOnboardingView.as_view(), name='onboarding'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('sessions/', SessionListView.as_view(), name='sessions'),
    path('sessions/<uuid:session_id>/revoke/', RevokeSessionView.as_view(), name='revoke_session'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),
]
