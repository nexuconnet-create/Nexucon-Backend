import time

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from apps.accounts import two_factor as tf
from apps.accounts.authentication import ApiKeyAuthentication
from apps.accounts.models import ApiKey, TwoFactorSecret, UserSession
from apps.accounts.serializers import CustomTokenObtainPairSerializer
from apps.government.models import Agency, Profile, Role

User = get_user_model()


class ApiKeyAuthenticationTestCase(TestCase):
    """
    Machine-to-machine X-API-Key authentication (apps/accounts/authentication.py).
    Keys must be active and bound to an active user; anything else is a 401.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='scanner_operator',
            email='operator@agency.gov.ng',
            password='Password123!',
            first_name='Ada',
            last_name='Obi',
        )
        self.client = APIClient()
        self.factory = RequestFactory()

    def _make_key(self):
        return ApiKey.objects.create(
            user=self.user,
            name='Scanner key',
            key=ApiKey.generate_key(),
        )

    def test_valid_api_key_authenticates_active_user(self):
        key = self._make_key()
        self.client.credentials(HTTP_X_API_KEY=key.key)
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['data']['email'], self.user.email)
        # First use stamps last_used_at (throttled to once per minute).
        key.refresh_from_db()
        self.assertIsNotNone(key.last_used_at)

    def test_api_key_accepted_via_authorization_header(self):
        key = self._make_key()
        request = self.factory.get(
            '/api/v1/auth/me/', HTTP_AUTHORIZATION=f'ApiKey {key.key}')
        user_auth = ApiKeyAuthentication().authenticate(request)
        self.assertIsNotNone(user_auth)
        self.assertEqual(user_auth[0], self.user)
        self.assertEqual(user_auth[1].pk, key.pk)

    def test_revoked_api_key_rejected(self):
        key = self._make_key()
        key.revoke()
        self.client.credentials(HTTP_X_API_KEY=key.key)
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 401)

    def test_unbound_api_key_rejected(self):
        key = ApiKey.objects.create(
            user=None, name='Unbound key', key=ApiKey.generate_key())
        self.client.credentials(HTTP_X_API_KEY=key.key)
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 401)

    def test_inactive_user_api_key_rejected(self):
        self.user.is_active = False
        self.user.save()
        key = self._make_key()
        self.client.credentials(HTTP_X_API_KEY=key.key)
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 401)

    def test_garbage_api_key_rejected(self):
        self.client.credentials(HTTP_X_API_KEY='nex_live_totally_fake_key')
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 401)

    def test_missing_api_key_falls_through_to_jwt(self):
        # With no key present at all the authenticator declines (returns
        # None) so the JWT authenticator can handle the request instead.
        request = self.factory.get('/api/v1/auth/me/')
        self.assertIsNone(ApiKeyAuthentication().authenticate(request))

        # End-to-end: a Bearer JWT authenticates with no API key involved.
        login = self.client.post('/api/v1/auth/login/', {
            'email': 'operator@agency.gov.ng',
            'password': 'Password123!',
        })
        self.assertEqual(login.status_code, 200)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access']}")
        res = self.client.get('/api/v1/auth/me/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['data']['email'], self.user.email)


class JwtClaimsTestCase(TestCase):
    """
    Custom JWT claims (CustomTokenObtainPairSerializer.get_token). Government
    users carry their real role; non-government users must get exactly
    'Client' with empty permissions (privilege-escalation regression guard).
    """

    def setUp(self):
        self.plain_user = User.objects.create_user(
            username='developer_clara',
            email='clara@client.dev',
            password='Password123!',
            first_name='Clara',
            last_name='Nwosu',
        )

    def _token_for(self, user):
        return CustomTokenObtainPairSerializer.get_token(user)

    def test_government_user_token_carries_real_role_and_permissions(self):
        user = User.objects.create_user(
            username='gov_inspector',
            email='inspector@agency.gov.ng',
            password='Password123!',
        )
        role = Role.objects.create(
            name='Chief Structural Inspector',
            permissions=['permits.approve', 'inspections.view'],
        )
        agency = Agency.objects.create(
            name='Lagos State Building Control (Test)',
            code='LASBCA-T',
        )
        Profile.objects.create(user=user, agency=agency, role=role)

        token = self._token_for(user)
        self.assertEqual(token['role'], 'Chief Structural Inspector')
        self.assertEqual(token['permissions'], ['permits.approve', 'inspections.view'])
        self.assertEqual(token['email'], user.email)

    def test_plain_user_token_gets_client_role_and_no_permissions(self):
        token = self._token_for(self.plain_user)
        self.assertEqual(token['role'], 'Client')
        self.assertEqual(token['permissions'], [])
        # Privilege-escalation regression: no admin claims may leak into a
        # non-government token.
        self.assertNotIn(token['role'], ('Director', 'admin', 'Agency Head'))
        self.assertNotIn('admin', token['permissions'])

    def test_superuser_token_gets_director_role(self):
        director = User.objects.create_superuser(
            username='super_director',
            email='director@agency.gov.ng',
            password='Password123!',
        )
        token = self._token_for(director)
        self.assertEqual(token['role'], 'Director')


class TwoFactorAuthTestCase(TestCase):
    """
    TOTP 2FA (apps/accounts/two_factor.py + /api/v1/auth/2fa/ endpoints).
    The app implements RFC 6238 itself, so its own helper (totp_at) is used
    to generate live codes.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='totp_user',
            email='totp@agency.gov.ng',
            password='Password123!',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _enroll(self):
        res = self.client.post('/api/v1/auth/2fa/setup/')
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['provisioning_uri'].startswith('otpauth://totp/'))
        self.assertIn(f"secret={res.data['secret']}", res.data['provisioning_uri'])
        return res.data['secret']

    @staticmethod
    def _wrong_code_for(secret):
        real = tf.totp_at(secret)
        return f'{(int(real) + 1) % 10**6:06d}'

    # ---------------- TOTP helper module ----------------

    def test_totp_helper_generates_and_verifies_real_codes(self):
        secret = tf.generate_secret()
        self.assertTrue(secret)
        self.assertTrue(tf.verify_code(secret, tf.totp_at(secret)))

    def test_totp_helper_rejects_wrong_code(self):
        secret = tf.generate_secret()
        self.assertFalse(tf.verify_code(secret, self._wrong_code_for(secret)))
        # Malformed codes are rejected outright.
        self.assertFalse(tf.verify_code(secret, '12ab89'))
        self.assertFalse(tf.verify_code(secret, ''))

    def test_totp_helper_accepts_one_step_clock_drift(self):
        secret = tf.generate_secret()
        code = tf.totp_at(secret, timestamp=1_700_000_000)
        self.assertTrue(tf.verify_code(secret, code, at_timestamp=1_700_000_030))

    def test_totp_helper_rejects_replayed_code(self):
        secret = tf.generate_secret()
        now = int(time.time())
        code = tf.totp_at(secret, timestamp=now)
        self.assertTrue(tf.verify_code(secret, code, at_timestamp=now))
        # The same code must not be accepted twice (replay protection).
        counter = now // tf.STEP_SECONDS
        self.assertFalse(
            tf.verify_code(secret, code, at_timestamp=now, last_used_counter=counter))

    # ---------------- 2FA endpoints ----------------

    def test_2fa_setup_generates_real_secret(self):
        secret = self._enroll()
        # The secret is real: it produces a code the app itself verifies.
        self.assertTrue(tf.verify_code(secret, tf.totp_at(secret)))
        config = TwoFactorSecret.objects.get(user=self.user)
        self.assertEqual(config.secret, secret)
        self.assertFalse(config.is_enabled)
        self.assertIsNone(config.confirmed_at)

    def test_2fa_verify_with_correct_code_enables_2fa(self):
        secret = self._enroll()
        res = self.client.post('/api/v1/auth/2fa/verify/',
                               {'code': tf.totp_at(secret)})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['enabled'])
        config = TwoFactorSecret.objects.get(user=self.user)
        self.assertTrue(config.is_enabled)
        self.assertIsNotNone(config.confirmed_at)

    def test_2fa_verify_with_wrong_code_fails(self):
        secret = self._enroll()
        res = self.client.post('/api/v1/auth/2fa/verify/',
                               {'code': self._wrong_code_for(secret)})
        self.assertEqual(res.status_code, 400)
        config = TwoFactorSecret.objects.get(user=self.user)
        self.assertFalse(config.is_enabled)
        self.assertIsNone(config.confirmed_at)

    def test_2fa_status_reports_pending_and_enabled(self):
        res = self.client.get('/api/v1/auth/2fa/')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['enabled'])
        self.assertFalse(res.data['pending_setup'])

        secret = self._enroll()
        res = self.client.get('/api/v1/auth/2fa/')
        self.assertTrue(res.data['pending_setup'])
        self.assertFalse(res.data['enabled'])

        self.client.post('/api/v1/auth/2fa/verify/', {'code': tf.totp_at(secret)})
        res = self.client.get('/api/v1/auth/2fa/')
        self.assertTrue(res.data['enabled'])
        self.assertFalse(res.data['pending_setup'])

    def test_login_with_2fa_enabled_requires_and_accepts_totp_code(self):
        secret = self._enroll()
        self.client.post('/api/v1/auth/2fa/verify/', {'code': tf.totp_at(secret)})

        unauth = APIClient()
        # Missing code: rejected before credentials are issued.
        res = unauth.post('/api/v1/auth/login/', {
            'email': 'totp@agency.gov.ng',
            'password': 'Password123!',
        })
        self.assertEqual(res.status_code, 400)
        self.assertTrue(res.data['data']['mfa_required'])
        self.assertEqual(res.data['data']['mfa_method'], 'totp')

        # Correct code: login succeeds.
        res_ok = unauth.post('/api/v1/auth/login/', {
            'email': 'totp@agency.gov.ng',
            'password': 'Password123!',
            'totp_code': tf.totp_at(secret),
        })
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.data['success'])
        self.assertIn('access', res_ok.data['data'])


class RegisterLoginEndpointsTestCase(TestCase):
    """Register/login endpoints stay AllowAny (intentional) and issue JWTs."""

    def setUp(self):
        self.client = APIClient()
        self.existing_user = User.objects.create_user(
            username='existing_user',
            email='existing@agency.gov.ng',
            password='Password123!',
        )

    def test_register_creates_user_and_returns_tokens(self):
        res = self.client.post('/api/v1/auth/register/', {
            'email': 'newuser@client.dev',
            'first_name': 'New',
            'last_name': 'Developer',
            'phone_number': '+2348012345678',
            'password': 'Password123!',
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['data']['user']['email'], 'newuser@client.dev')
        self.assertEqual(res.data['data']['user']['role_name'], 'Client')
        self.assertIn('access', res.data['data'])
        self.assertIn('refresh', res.data['data'])
        # A session row is created for the new user.
        self.assertEqual(
            UserSession.objects.filter(user__email='newuser@client.dev').count(), 1)

    def test_login_returns_tokens_and_sets_auth_cookies(self):
        res = self.client.post('/api/v1/auth/login/', {
            'email': 'existing@agency.gov.ng',
            'password': 'Password123!',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        self.assertIn('access', res.data['data'])
        self.assertIn('refresh', res.data['data'])
        self.assertEqual(res.data['data']['user']['email'], 'existing@agency.gov.ng')
        self.assertEqual(res.data['data']['user']['role_name'], 'Client')
        self.assertIn('access_token', res.cookies)
        self.assertIn('refresh_token', res.cookies)

    def test_login_with_bad_credentials_rejected(self):
        res = self.client.post('/api/v1/auth/login/', {
            'email': 'existing@agency.gov.ng',
            'password': 'WrongPassword!',
        })
        self.assertEqual(res.status_code, 401)
