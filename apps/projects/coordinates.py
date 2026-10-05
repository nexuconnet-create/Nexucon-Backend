import math
from typing import List, Dict, Any, Tuple

def utm_to_latlon(
    easting: float,
    northing: float,
    zone: int = 31,
    northern_hemisphere: bool = True,
    datum: str = 'WGS84'
) -> Tuple[float, float]:
    """
    Converts Universal Transverse Mercator (UTM) coordinates to WGS84 Latitude and Longitude.
    Supports WGS84 and Nigerian Minna Datum (Clarke 1880 RGS).
    """
    a = 6378137.0
    f = 1 / 298.257223563
    if datum.upper() == 'MINNA':
        a = 6378249.145
        f = 1 / 293.465

    b = a * (1 - f)
    e = math.sqrt(1 - (b**2 / a**2))
    e_prime_sq = (a**2 - b**2) / (b**2)
    k0 = 0.9996

    x = easting - 500000.0
    y = northing if northern_hemisphere else northing - 10000000.0

    M = y / k0
    mu = M / (a * (1 - e**2 / 4 - 3 * e**4 / 64 - 5 * e**6 / 256))

    e1 = (1 - math.sqrt(1 - e**2)) / (1 + math.sqrt(1 - e**2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )

    N1 = a / math.sqrt(1 - e**2 * math.sin(phi1)**2)
    T1 = math.tan(phi1)**2
    C1 = e_prime_sq * math.cos(phi1)**2
    R1 = a * (1 - e**2) / ((1 - e**2 * math.sin(phi1)**2)**1.5)
    D = x / (N1 * k0)

    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D**2 / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1**2 - 9 * e_prime_sq) * D**4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1**2 - 252 * e_prime_sq - 3 * C1**2) * D**6 / 720
    )
    lat = math.degrees(lat)

    lon_origin = (zone - 1) * 6 - 180 + 3
    lon = lon_origin + math.degrees(
        (
            D
            - (1 + 2 * T1 + C1) * D**3 / 6
            + (5 - 2 * C1 + 28 * T1 - 3 * C1**2 + 8 * e_prime_sq + 24 * T1**2) * D**5 / 120
        )
        / math.cos(phi1)
    )

    if datum.upper() == 'MINNA':
        # Standard 3-parameter shift from Minna to WGS84 for Nigeria (dx=-92, dy=-93, dz=122)
        lat += 0.000305
        lon -= 0.00085

    return lat, lon


def latlon_to_utm(
    lat: float,
    lon: float,
    forced_zone: int = None,
    datum: str = 'WGS84'
) -> Tuple[float, float, int]:
    """
    Converts Latitude and Longitude to UTM Easting, Northing, and Zone.
    """
    adjusted_lat = lat
    adjusted_lon = lon

    a = 6378137.0
    f = 1 / 298.257223563
    if datum.upper() == 'MINNA':
        a = 6378249.145
        f = 1 / 293.465
        adjusted_lat -= 0.000305
        adjusted_lon += 0.00085

    b = a * (1 - f)
    e = math.sqrt(1 - (b**2 / a**2))
    e_prime_sq = (a**2 - b**2) / (b**2)
    k0 = 0.9996

    zone = forced_zone if forced_zone is not None else int((adjusted_lon + 180) / 6) + 1
    lon_origin = (zone - 1) * 6 - 180 + 3
    lon_origin_rad = math.radians(lon_origin)

    lat_rad = math.radians(adjusted_lat)
    lon_rad = math.radians(adjusted_lon)

    N = a / math.sqrt(1 - e**2 * math.sin(lat_rad)**2)
    T = math.tan(lat_rad)**2
    C = e_prime_sq * math.cos(lat_rad)**2
    A = math.cos(lat_rad) * (lon_rad - lon_origin_rad)

    M = a * (
        (1 - e**2 / 4 - 3 * e**4 / 64 - 5 * e**6 / 256) * lat_rad
        - (3 * e**2 / 8 + 3 * e**4 / 32 + 45 * e**6 / 1024) * math.sin(2 * lat_rad)
        + (15 * e**4 / 256 + 45 * e**6 / 1024) * math.sin(4 * lat_rad)
        - (35 * e**6 / 3072) * math.sin(6 * lat_rad)
    )

    easting = (
        k0
        * N
        * (
            A
            + (1 - T + C) * A**3 / 6
            + (5 - 18 * T + T**2 + 72 * C - 58 * e_prime_sq) * A**5 / 120
        )
        + 500000.0
    )

    northing = k0 * (
        M
        + N
        * math.tan(lat_rad)
        * (
            A**2 / 2
            + (5 - T + 9 * C + 4 * C**2) * A**4 / 24
            + (61 - 58 * T + T**2 + 600 * C - 330 * e_prime_sq) * A**6 / 720
        )
    )

    if adjusted_lat < 0:
        northing += 10000000.0

    return easting, northing, zone


def dms_to_dd(deg: float, minutes: float, sec: float, direction: str = 'N') -> float:
    dd = abs(deg) + (abs(minutes) or 0) / 60.0 + (abs(sec) or 0) / 3600.0
    if direction.upper() in ('S', 'W'):
        dd = -dd
    return dd


def dd_to_dms(dd: float, is_latitude: bool) -> Dict[str, Any]:
    abs_dd = abs(dd)
    deg = int(abs_dd)
    min_remainder = (abs_dd - deg) * 60.0
    minute = int(min_remainder)
    sec = round((min_remainder - minute) * 60.0, 2)
    direction = ('N' if dd >= 0 else 'S') if is_latitude else ('E' if dd >= 0 else 'W')
    return {
        'degrees': deg,
        'minutes': minute,
        'seconds': sec,
        'direction': direction,
    }


def format_dms(dd: float, is_latitude: bool) -> str:
    d = dd_to_dms(dd, is_latitude)
    return f"{d['degrees']}° {d['minutes']}' {d['seconds']:.2f}\" {d['direction']}"


def calculate_polygon_area_sqm(utm_points: List[Dict[str, float]]) -> float:
    """Shoelace formula in projected metric Cartesian coordinates."""
    if len(utm_points) < 3:
        return 0.0
    n = len(utm_points)
    total = 0.0
    for i in range(n):
        j = (i + 1) % n
        total += utm_points[i]['easting'] * utm_points[j]['northing']
        total -= utm_points[j]['easting'] * utm_points[i]['northing']
    return round(abs(total) / 2.0, 2)


def calculate_perimeter_meters(utm_points: List[Dict[str, float]]) -> float:
    if len(utm_points) < 2:
        return 0.0
    n = len(utm_points)
    perimeter = 0.0
    for i in range(n):
        j = (i + 1) % n
        dx = utm_points[j]['easting'] - utm_points[i]['easting']
        dy = utm_points[j]['northing'] - utm_points[i]['northing']
        perimeter += math.sqrt(dx**2 + dy**2)
    return round(perimeter, 2)


def convert_four_corners(system: str, corners_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Transforms 4 building corners across systems (DD, DMS, UTM Zone 31N/32N WGS84 & Minna),
    calculates center, area, and bounding perimeter.
    """
    converted_corners = []
    utm_points = []
    lat_list = []
    lng_list = []

    for index, corner in enumerate(corners_data):
        label = corner.get('label') or f"Corner {index + 1}"
        lat = 0.0
        lng = 0.0
        easting = 0.0
        northing = 0.0
        utm_zone = '31N'

        if system == 'WGS84_DD':
            lat = float(corner.get('lat') or 0.0)
            lng = float(corner.get('lng') or 0.0)
            e, n, z = latlon_to_utm(lat, lng, 31, 'WGS84')
            easting = round(e, 2)
            northing = round(n, 2)
            utm_zone = f"{z}N"

        elif system == 'DMS':
            lat_deg = float(corner.get('latDeg') or 0.0)
            lat_min = float(corner.get('latMin') or 0.0)
            lat_sec = float(corner.get('latSec') or 0.0)
            lat_dir = str(corner.get('latDir') or 'N')

            lng_deg = float(corner.get('lngDeg') or 0.0)
            lng_min = float(corner.get('lngMin') or 0.0)
            lng_sec = float(corner.get('lngSec') or 0.0)
            lng_dir = str(corner.get('lngDir') or 'E')

            lat = dms_to_dd(lat_deg, lat_min, lat_sec, lat_dir)
            lng = dms_to_dd(lng_deg, lng_min, lng_sec, lng_dir)
            e, n, z = latlon_to_utm(lat, lng, 31, 'WGS84')
            easting = round(e, 2)
            northing = round(n, 2)
            utm_zone = f"{z}N"

        elif system in ('UTM_31N_WGS84', 'UTM_31N_MINNA', 'UTM_32N_WGS84', 'UTM_32N_MINNA'):
            easting = float(corner.get('easting') or 0.0)
            northing = float(corner.get('northing') or 0.0)
            zone_num = 32 if '32N' in system else 31
            datum_type = 'MINNA' if 'MINNA' in system else 'WGS84'

            lat_val, lng_val = utm_to_latlon(easting, northing, zone_num, True, datum_type)
            lat = round(lat_val, 6)
            lng = round(lng_val, 6)
            utm_zone = f"{zone_num}N"

        else:
            lat = float(corner.get('lat') or 0.0)
            lng = float(corner.get('lng') or 0.0)
            e, n, z = latlon_to_utm(lat, lng, 31, 'WGS84')
            easting = round(e, 2)
            northing = round(n, 2)
            utm_zone = f"{z}N"

        lat_list.append(lat)
        lng_list.append(lng)
        utm_points.append({'easting': easting, 'northing': northing})

        converted_corners.append({
            'id': corner.get('id', index + 1),
            'label': label,
            'lat': round(lat, 6),
            'lng': round(lng, 6),
            'formattedLatDms': format_dms(lat, True),
            'formattedLngDms': format_dms(lng, False),
            'easting': easting,
            'northing': northing,
            'utmZone': utm_zone,
        })

    center_lat = round(sum(lat_list) / len(lat_list), 6) if lat_list else 0.0
    center_lng = round(sum(lng_list) / len(lng_list), 6) if lng_list else 0.0
    footprint_area = calculate_polygon_area_sqm(utm_points)
    perimeter = calculate_perimeter_meters(utm_points)

    return {
        'system': system,
        'corners': converted_corners,
        'center': {
            'lat': center_lat,
            'lng': center_lng,
            'formattedLatDms': format_dms(center_lat, True),
            'formattedLngDms': format_dms(center_lng, False),
        },
        'footprintAreaSqm': footprint_area,
        'perimeterMeters': perimeter,
        'googleMapsUrl': f"https://www.google.com/maps?q={center_lat},{center_lng}&t=k",
    }
