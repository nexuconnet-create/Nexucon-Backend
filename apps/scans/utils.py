"""
Helpers for working with stored object-storage URLs.

Uploads store a presigned URL on the model. Those signatures expire about an
hour after upload, so anything that reads the object later — a server-side
download, a browser fetch, a serializer — must re-sign the URL on demand
instead of trusting the stored one.
"""
from urllib.parse import urlsplit, unquote

from django.conf import settings
from django.core.files.storage import default_storage

# Hosts whose URLs are presigned S3/R2 object links.
_REMOTE_HOSTS = ('r2.cloudflarestorage.com', 's3.amazonaws.com')


def extract_object_key(url):
    """Return the durable S3/R2 object key behind a presigned URL, or None."""
    if not url or not url.startswith('http'):
        return None
    path = urlsplit(url).path.lstrip('/')
    bucket = getattr(settings, 'CLOUDFLARE_R2_BUCKET_NAME', '')
    if bucket and path.startswith(bucket + '/'):
        path = path[len(bucket) + 1:]
    return unquote(path) or None


def refresh_storage_url(url):
    """
    Re-sign a stored URL against the active storage backend. Falls back to
    the original URL whenever re-signing is impossible or unnecessary
    (local /media/ files, unknown hosts, storage errors).
    """
    if not url or not any(host in url for host in _REMOTE_HOSTS):
        return url
    key = extract_object_key(url)
    if not key:
        return url
    try:
        fresh = default_storage.url(key)
        return fresh if fresh else url
    except Exception:
        return url


def extract_image_bbox(item):
    """
    Pull the AI-reported image_bbox {'xmin','ymin','xmax','ymax'} (normalised
    0-1 image coords) out of an AI finding dict and return it as model-ready
    bbox_* field values. Returns an empty dict when the AI supplied no usable
    bbox — the caller then leaves the columns null.
    """
    bbox = item.get('image_bbox') if isinstance(item, dict) else None
    if not isinstance(bbox, dict):
        return {}

    values = {}
    for key in ('xmin', 'ymin', 'xmax', 'ymax'):
        try:
            values[key] = float(bbox.get(key))
        except (TypeError, ValueError):
            return {}

    xmin, ymin, xmax, ymax = values['xmin'], values['ymin'], values['xmax'], values['ymax']
    if not all(0.0 <= v <= 1.0 for v in values.values()):
        return {}
    if xmin >= xmax or ymin >= ymax:
        return {}

    return {
        'bbox_xmin': xmin,
        'bbox_ymin': ymin,
        'bbox_xmax': xmax,
        'bbox_ymax': ymax,
    }


def resolve_session_bim_file(session):
    """
    Locate the session's BIM model on local disk, downloading it from remote
    storage when needed. Returns (path, extension, is_temp) or
    (None, None, False). The extension is preserved — .rvt routing to
    Autodesk APS depends on it.
    """
    import os
    import tempfile
    from apps.scans.models import ScanFile

    bim_file = ScanFile.objects.filter(session=session, file_type='bim').last()
    if not bim_file or not bim_file.file_url:
        return None, None, False

    url = bim_file.file_url
    name = url.split('?')[0].rstrip('/').rsplit('/', 1)[-1]
    ext = os.path.splitext(name)[1].lower() or '.ifc'

    if url.startswith('http'):
        fresh = refresh_storage_url(url)
        import requests
        try:
            res = requests.get(fresh, stream=True, timeout=300)
            res.raise_for_status()
            fd, path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, 'wb') as f:
                for chunk in res.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            return path, ext, True
        except Exception:
            return None, None, False

    rel = url.replace('/media/', '', 1)
    fp = os.path.join(settings.MEDIA_ROOT or '', rel)
    if os.path.exists(fp):
        return fp, ext, False
    return None, None, False
