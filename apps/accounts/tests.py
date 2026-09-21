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


from apps.accounts.models import ApiKey, TwoFactorSecret, UserSession, EmailVerificationToken


class RegisterLoginEndpointsTestCase(TestCase):
    """Register/login endpoints stay AllowAny (intentional) and issue JWTs."""

    def setUp(self):
        self.client = APIClient()
        self.existing_user = User.objects.create_user(
            username='existing@agency.gov.ng',
            email='existing@agency.gov.ng',
            password='Password123!',
            is_verified=True,
        )

    def test_register_creates_unverified_user_and_dispatches_otp(self):
        res = self.client.post('/api/v1/auth/register/', {
            'email': 'newuser@client.dev',
            'first_name': 'New',
            'last_name': 'Developer',
            'phone_number': '+2348012345678',
            'password': 'Password123!',
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['data']['email'], 'newuser@client.dev')
        self.assertTrue(res.data['data']['requires_verification'])
        self.assertFalse(res.data['data']['is_verified'])

        # Check user in database
        user = User.objects.get(email='newuser@client.dev')
        self.assertFalse(user.is_verified)

        # Check OTP token was generated
        token = EmailVerificationToken.objects.filter(email='newuser@client.dev', is_used=False).first()
        self.assertIsNotNone(token)
        self.assertEqual(len(token.code), 6)
        self.assertTrue(token.is_valid)

    def test_verify_email_with_valid_otp(self):
        # Register user
        self.client.post('/api/v1/auth/register/', {
            'email': 'verify_test@nexucon.net',
            'first_name': 'Test',
            'last_name': 'User',
            'password': 'Password123!',
        })
        token = EmailVerificationToken.objects.filter(email='verify_test@nexucon.net', is_used=False).first()

        # Verify with correct code
        res = self.client.post('/api/v1/auth/verify-email/', {
            'email': 'verify_test@nexucon.net',
            'code': token.code,
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        self.assertIn('access', res.data['data'])
        self.assertIn('refresh', res.data['data'])
        self.assertTrue(res.data['data']['user']['is_verified'])

        # Check user is verified in DB
        user = User.objects.get(email='verify_test@nexucon.net')
        self.assertTrue(user.is_verified)

        # Token is marked as used
        token.refresh_from_db()
        self.assertTrue(token.is_used)

    def test_verify_email_with_invalid_otp_fails(self):
        self.client.post('/api/v1/auth/register/', {
            'email': 'invalid_otp@nexucon.net',
            'first_name': 'Invalid',
            'last_name': 'OTP',
            'password': 'Password123!',
        })
        res = self.client.post('/api/v1/auth/verify-email/', {
            'email': 'invalid_otp@nexucon.net',
            'code': '000000',
        })
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.data['success'])

        user = User.objects.get(email='invalid_otp@nexucon.net')
        self.assertFalse(user.is_verified)

    def test_resend_verification_generates_new_otp(self):
        self.client.post('/api/v1/auth/register/', {
            'email': 'resend_test@nexucon.net',
            'first_name': 'Resend',
            'last_name': 'Tester',
            'password': 'Password123!',
        })
        # Simulate time passing to avoid 30s rate limit
        from django.utils import timezone
        token1 = EmailVerificationToken.objects.filter(email='resend_test@nexucon.net').first()
        token1.created_at = timezone.now() - timezone.timedelta(seconds=40)
        token1.save(update_fields=['created_at'])

        res = self.client.post('/api/v1/auth/resend-verification/', {
            'email': 'resend_test@nexucon.net',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])

        tokens = EmailVerificationToken.objects.filter(email='resend_test@nexucon.net').order_by('-created_at')
        self.assertEqual(tokens.count(), 2)
        self.assertTrue(tokens[0].is_valid)
        self.assertTrue(tokens[1].is_used)

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
        self.assertIn('access_token', res.cookies)
        self.assertIn('refresh_token', res.cookies)

    def test_login_with_bad_credentials_rejected(self):
        res = self.client.post('/api/v1/auth/login/', {
            'email': 'existing@agency.gov.ng',
            'password': 'WrongPassword!',
        })
        self.assertEqual(res.status_code, 401)


class StakeholderAuthTestCase(TestCase):
    """
    Validates stakeholder registration, login, and onboarding flows
    for Developers, Contractors, Licensed Professionals, and Consultants.
    """

    def setUp(self):
        self.client = APIClient()

    def test_stakeholder_registration_creates_user_and_developer_record(self):
        payload = {
            'name': 'Femi Adebayo',
            'email': 'femi@primedev.ng',
            'password': 'Password123!',
            'phone_number': '+2348011112222',
            'stakeholder_type': 'developer',
            'company_name': 'Prime Developments Ltd',
            'registration_number': 'RC-102938',
            'country': 'NG',
            'state_region': 'Lagos',
            'office_address': 'Plot 4, Victoria Island, Lagos',
        }
        res = self.client.post('/api/v1/auth/register/', payload)
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data['success'])

        # Verify user created with split names
        user = User.objects.get(email='femi@primedev.ng')
        self.assertEqual(user.first_name, 'Femi')
        self.assertEqual(user.last_name, 'Adebayo')

        # Verify Developer entity created
        from apps.stakeholders.models import Developer
        dev = Developer.objects.filter(user=user).first()
        self.assertIsNotNone(dev)
        self.assertEqual(dev.name, 'Prime Developments Ltd')
        self.assertEqual(dev.primary_contact_name, 'Femi Adebayo')

    def test_stakeholder_contractor_registration(self):
        payload = {
            'name': 'Chidi Okeke',
            'email': 'chidi@buildtech.ng',
            'password': 'Password123!',
            'phone_number': '+2348033334444',
            'stakeholder_type': 'contractor',
            'company_name': 'BuildTech Construction',
            'registration_number': 'RC-555666',
            'license_number': 'CON-LIC-998',
        }
        res = self.client.post('/api/v1/auth/register/', payload)
        self.assertEqual(res.status_code, 201)

        from apps.stakeholders.models import Contractor
        user = User.objects.get(email='chidi@buildtech.ng')
        con = Contractor.objects.filter(user=user).first()
        self.assertIsNotNone(con)
        self.assertEqual(con.company_name, 'BuildTech Construction')
        self.assertEqual(con.license_number, 'CON-LIC-998')

    def test_stakeholder_login_and_me_profile(self):
        user = User.objects.create_user(
            username='dev@skyline.ng',
            email='dev@skyline.ng',
            password='Password123!',
            first_name='Amina',
            last_name='Danjuma',
            is_verified=True,
        )
        from apps.stakeholders.models import Developer
        Developer.objects.create(
            user=user,
            name='Skyline Properties',
            status='Active',
            hq_location='Abuja FCT',
            primary_contact_name='Amina Danjuma',
        )

        res = self.client.post('/api/v1/auth/login/', {
            'email': 'dev@skyline.ng',
            'password': 'Password123!',
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        user_data = res.data['data']['user']
        self.assertEqual(user_data['role_name'], 'Stakeholder: Developer')
        self.assertIsNotNone(user_data['stakeholder_profile'])
        self.assertEqual(user_data['stakeholder_profile']['name'], 'Skyline Properties')

    def test_stakeholder_onboarding_updates_profile_and_completes(self):
        user = User.objects.create_user(
            username='engr.tunde@consult.ng',
            email='engr.tunde@consult.ng',
            password='Password123!',
            first_name='Tunde',
            last_name='Bakare',
            is_verified=True,
            is_onboarded=False,
        )
        self.client.force_authenticate(user=user)

        onboarding_payload = {
            'portal': 'stakeholder',
            'stakeholder_type': 'professional',
            'company_name': 'Bakare & Associates Structural Engineering',
            'registration_number': 'RC-998877',
            'license_authority': 'COREN',
            'license_number': 'R.29481',
            'country': 'NG',
            'state_region': 'Lagos',
            'city': 'Ikeja',
            'office_address': '12 Allen Avenue, Ikeja',
            'project_scale_focus': 'infrastructure',
        }
        res = self.client.post('/api/v1/auth/onboarding/', onboarding_payload)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])

        user.refresh_from_db()
        self.assertTrue(user.is_onboarded)

        from apps.stakeholders.models import LicensedProfessional
        prof = LicensedProfessional.objects.filter(user=user).first()
        self.assertIsNotNone(prof)
        self.assertEqual(prof.license_authority, 'COREN')
        self.assertEqual(prof.firm_name, 'Bakare & Associates Structural Engineering')
        self.assertEqual(prof.license_status, 'Active')


