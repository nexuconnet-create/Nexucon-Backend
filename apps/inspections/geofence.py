"""
Site geofencing for field check-in (Inspector PWA, Module 2).

The spec requires a check-in to be refused unless the inspector is within 50 m
of the site:

    ⚠️ WARNING: You must be within 50m of the site to check in

Before this module existed, `InspectionExecutionService.checkin()` validated
only that the coordinates were numeric and within global range, then set
``inspection.gps_verified = True`` unconditionally. The flag therefore carried
no information at all, while every consumer — the dashboard, the report, the
submission hash — read it as an attestation that the inspector was on site.

Three design decisions are worth stating plainly, because each of them is a
place where a plausible-looking implementation would lie:

1. **A project with no recorded coordinates is not "inside" anything.** Its
   check-ins are recorded as ``UNVERIFIABLE`` with ``gps_verified=False``. The
   alternative — treating an unknown site point as a pass — would mean the
   projects with the least recorded data get the weakest scrutiny.

2. **A fix whose own error exceeds the radius cannot certify the radius.** A
   50 m enclosure witnessed by a ±200 m fix is a false attestation: the device
   may be 200 m away and still report inside. Such a check-in is
   ``UNVERIFIABLE``, and a check-in that reports no accuracy at all is
   ``ACCURACY_NOT_REPORTED``. Both keep ``gps_verified=False``.

3. **Enforcement is a policy, not a constant.** `GEOFENCE_ENFORCEMENT` is
   ``off``/``warn``/``strict``. The default is ``warn``: the distance is
   measured, recorded and returned, but the check-in still succeeds. This is
   what lets the platform ship before every project's coordinates are
   backfilled, without the flag ever claiming more than it knows. Flipping to
   ``strict`` is a one-line settings change once the coordinate backfill is
   done — no code change.
"""
import math
from dataclasses import dataclass

from django.conf import settings

from common.geo import haversine_m

# --------------------------------------------------------------- states
STATE_VERIFIED = 'VERIFIED'
STATE_OUTSIDE = 'OUTSIDE'
STATE_UNVERIFIABLE = 'UNVERIFIABLE'

# --------------------------------------------------------------- reasons
REASON_WITHIN_RADIUS = 'WITHIN_RADIUS'
REASON_OUTSIDE_RADIUS = 'OUTSIDE_RADIUS'
REASON_PROJECT_COORDINATES_NOT_RECORDED = 'PROJECT_COORDINATES_NOT_RECORDED'
REASON_ACCURACY_NOT_REPORTED = 'ACCURACY_NOT_REPORTED'
REASON_DEVICE_ACCURACY_EXCEEDS_RADIUS = 'DEVICE_ACCURACY_EXCEEDS_RADIUS'

ENFORCEMENT_OFF = 'off'
ENFORCEMENT_WARN = 'warn'
ENFORCEMENT_STRICT = 'strict'
VALID_ENFORCEMENT_MODES = (ENFORCEMENT_OFF, ENFORCEMENT_WARN, ENFORCEMENT_STRICT)


def default_geofence_radius_m():
    """The platform-wide fallback radius, in metres."""
    return getattr(settings, 'DEFAULT_GEOFENCE_RADIUS_M', 50)


def enforcement_mode():
    """The active enforcement policy: ``off``, ``warn`` or ``strict``."""
    mode = str(getattr(settings, 'GEOFENCE_ENFORCEMENT', ENFORCEMENT_WARN)).lower()
    return mode if mode in VALID_ENFORCEMENT_MODES else ENFORCEMENT_WARN


def effective_geofence_radius(project):
    """Return ``(radius_m, source)`` for `project`.

    ``source`` is ``'project'`` when the project records its own radius and
    ``'platform_default'`` when the platform fallback applies. The caller is
    expected to surface it: presenting the platform default as though the
    project had recorded it would attribute a decision to the client that the
    client never made.
    """
    radius = getattr(project, 'geofence_radius_m', None)
    if radius:
        return int(radius), 'project'
    return default_geofence_radius_m(), 'platform_default'


@dataclass(frozen=True)
class GeofenceResult:
    """The outcome of evaluating one position against one project's geofence."""

    checked: bool           # False when the site point is unknown
    state: str              # VERIFIED | OUTSIDE | UNVERIFIABLE
    reason: str
    distance_m: float | None    # None when it could not be measured
    radius_m: int
    radius_source: str          # 'project' | 'platform_default'
    accuracy_m: float | None    # as reported by the device; None if not sent

    @property
    def verified(self) -> bool:
        """True only when the position is inside the radius and properly evidenced.

        This is the single source of truth for ``Inspection.gps_verified``.
        """
        return self.state == STATE_VERIFIED

    def as_dict(self) -> dict:
        """The wire shape returned by the check-in endpoint."""
        return {
            'state': self.state,
            'reason': self.reason,
            'distance_m': (None if self.distance_m is None
                           else round(self.distance_m, 1)),
            'radius_m': self.radius_m,
            'radius_source': self.radius_source,
            'accuracy_m': self.accuracy_m,
            'checked': self.checked,
        }


def _coerce_accuracy(accuracy_m):
    """Return a usable non-negative accuracy in metres, or None."""
    if accuracy_m is None or accuracy_m == '':
        return None
    try:
        value = float(accuracy_m)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value) or value < 0:
        return None
    return value


def evaluate_geofence(project, latitude, longitude, accuracy_m=None):
    """Evaluate a device position against `project`'s geofence.

    `latitude`/`longitude` are the device-reported position (already validated
    as numeric and in global range by the caller). `accuracy_m` is the device's
    own reported horizontal accuracy in metres, or None when it did not report
    one. It is never assumed to be good.
    """
    radius_m, radius_source = effective_geofence_radius(project)
    accuracy = _coerce_accuracy(accuracy_m)

    site_lat = getattr(project, 'latitude', None)
    site_lon = getattr(project, 'longitude', None)

    if site_lat is None or site_lon is None:
        return GeofenceResult(
            checked=False,
            state=STATE_UNVERIFIABLE,
            reason=REASON_PROJECT_COORDINATES_NOT_RECORDED,
            distance_m=None,
            radius_m=radius_m,
            radius_source=radius_source,
            accuracy_m=accuracy,
        )

    distance = haversine_m((site_lat, site_lon), (latitude, longitude))

    # A device that did not report its accuracy cannot certify a radius: the
    # fix may be far less precise than the enclosure.
    if accuracy is None:
        return GeofenceResult(
            checked=True,
            state=STATE_UNVERIFIABLE,
            reason=REASON_ACCURACY_NOT_REPORTED,
            distance_m=distance,
            radius_m=radius_m,
            radius_source=radius_source,
            accuracy_m=None,
        )

    # An accuracy larger than the radius means the device could be outside the
    # enclosure while reporting a position inside it.
    if accuracy > radius_m:
        return GeofenceResult(
            checked=True,
            state=STATE_UNVERIFIABLE,
            reason=REASON_DEVICE_ACCURACY_EXCEEDS_RADIUS,
            distance_m=distance,
            radius_m=radius_m,
            radius_source=radius_source,
            accuracy_m=accuracy,
        )

    if distance <= radius_m:
        return GeofenceResult(
            checked=True,
            state=STATE_VERIFIED,
            reason=REASON_WITHIN_RADIUS,
            distance_m=distance,
            radius_m=radius_m,
            radius_source=radius_source,
            accuracy_m=accuracy,
        )

    return GeofenceResult(
        checked=True,
        state=STATE_OUTSIDE,
        reason=REASON_OUTSIDE_RADIUS,
        distance_m=distance,
        radius_m=radius_m,
        radius_source=radius_source,
        accuracy_m=accuracy,
    )


def refusal_message(result: GeofenceResult) -> str:
    """A refusal message that states the measured distance, not just "no"."""
    if result.reason == REASON_OUTSIDE_RADIUS:
        return (
            f'You must be within {result.radius_m} m of the site to check in. '
            f'Your reported position is {result.distance_m:.0f} m from the '
            f'recorded site location.'
        )
    if result.reason == REASON_PROJECT_COORDINATES_NOT_RECORDED:
        return (
            'This project has no recorded site coordinates, so a geofenced '
            'check-in cannot be verified. Ask a Director to record the site '
            'location.'
        )
    if result.reason == REASON_ACCURACY_NOT_REPORTED:
        return (
            'The device did not report its GPS accuracy, so a '
            f'{result.radius_m} m geofence cannot be verified. Send '
            'gps_accuracy_m with the check-in.'
        )
    if result.reason == REASON_DEVICE_ACCURACY_EXCEEDS_RADIUS:
        return (
            f'The device reports a GPS accuracy of ±{result.accuracy_m:.0f} m, '
            f'which is wider than the {result.radius_m} m geofence. Move to a '
            'position with a better fix and check in again.'
        )
    return f'Check-in could not be verified ({result.reason}).'
