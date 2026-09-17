"""
drf-spectacular extensions for this platform's custom authenticators.

Without these, every operation on the platform emits two warnings —
"could not resolve authenticator ... There was no OpenApiAuthenticationExtension
registered for that class" — and the generated schema advertises no security
scheme at all. That is worse than noise: a client generated from the schema
would have no way to learn how to authenticate, and the browser-based Swagger UI
would offer no Authorize button.

Registering the two classes here removes both problems at the source. Nothing
about runtime authentication changes; this only describes what already happens.

`CookieJWTAuthentication` extends SimpleJWT's `JWTAuthentication`, which
drf-spectacular already understands as HTTP bearer. Its addition here is the
cookie fallback, which a schema cannot express — so the scheme is declared as
bearer and the cookie is documented in the description rather than invented as a
second scheme.

Drf-spectacular discovers these by import — a subclass registers itself the
moment the class body runs — so `AccountsConfig.ready()` imports this module.
It is never imported on a request path, so it costs a request nothing.
"""
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = 'apps.accounts.authentication.ApiKeyAuthentication'
    name = 'ApiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': 'X-API-Key',
            'description': (
                'Machine-to-machine key for scanners and integrations that '
                'cannot run the interactive login flow. Must be active and '
                'bound to a user. `Authorization: ApiKey <key>` is also '
                'accepted.'
            ),
        }


class CookieJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = 'apps.accounts.authentication.CookieJWTAuthentication'
    name = 'JWTCookieAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
            'description': (
                'Access token in `Authorization: Bearer <token>`, or the '
                'HttpOnly `access_token` cookie the web client uses. A cookie '
                'session is additionally checked against its UserSession '
                'record and refused once that session is no longer active.'
            ),
        }
