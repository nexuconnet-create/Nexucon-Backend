"""
Geospatial helpers shared across apps.

These are pure math — no models, no ORM, no settings — so every app can use
them without importing another app's domain. `haversine_m` previously lived in
`apps.evidence.correlation`, which meant `apps.inspections` had to import the
evidence app to measure a distance between two points. That is the layering
inverted for a function that knows nothing about evidence.
"""
import math

# Mean Earth radius (IUGG). A spherical Earth is accurate to ~0.5% locally,
# which is far below the accuracy of any GNSS fix a field device reports, so
# the added precision of an ellipsoidal model would be false comfort here.
EARTH_RADIUS_M = 6371000.0


def haversine_m(point_a, point_b):
    """Great-circle distance in metres between two ``(lat, lon)`` pairs.

    Returns ``inf`` when either point is missing a coordinate. That is
    deliberate: an unknown position is not zero metres away, and a caller that
    compares the result against a radius must fail the comparison rather than
    pass it. Callers that need a distinct "could not measure" state should test
    for ``inf`` explicitly (see ``apps.inspections.geofence``).
    """
    if None in (point_a + point_b):
        return float('inf')
    lat1, lon1, lat2, lon2 = map(
        math.radians, (point_a[0], point_a[1], point_b[0], point_b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.asin(math.sqrt(a))
