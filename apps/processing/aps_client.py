import os
import time
import base64
import logging
import re
from urllib.parse import quote

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class AutodeskAPSClient:
    """
    Client for Autodesk Platform Services (APS), formerly Forge.
    Uploads proprietary .rvt files to OSS via S3 signed URLs (the legacy
    direct PUT object endpoint is deprecated for new Server-to-Server apps),
    translates them to IFC with the Model Derivative API, and downloads the
    translated derivative via the signed-cookies CDN endpoint.
    """

    BASE_URL = "https://developer.api.autodesk.com"

    def __init__(self):
        self.client_id = getattr(settings, 'AUTODESK_CLIENT_ID', None) or os.environ.get('AUTODESK_CLIENT_ID')
        self.client_secret = getattr(settings, 'AUTODESK_CLIENT_SECRET', None) or os.environ.get('AUTODESK_CLIENT_SECRET')
        self.bucket_key = "nexucon_bim_processing_temp"
        self._token = None
        self._token_expires_at = 0

    def _get_token(self):
        if not self.client_id or not self.client_secret:
            raise ValueError("Autodesk APS credentials missing. Cannot parse proprietary .rvt file.")

        if self._token and time.time() < self._token_expires_at:
            return self._token

        # Two-legged client-credentials flow — client id/secret go in the
        # form body, NOT a Basic auth header.
        response = requests.post(
            f"{self.BASE_URL}/authentication/v2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "bucket:create bucket:read data:read data:write data:create",
            },
            timeout=30,
        )

        if response.status_code != 200:
            logger.error("Failed to authenticate with Autodesk APS: %s", response.text)
            raise Exception("Autodesk APS Authentication Failed")

        res_data = response.json()
        self._token = res_data["access_token"]
        # expire 60s early for safety
        self._token_expires_at = time.time() + res_data.get("expires_in", 3599) - 60
        return self._token

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _ensure_bucket(self):
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        res = requests.post(
            f"{self.BASE_URL}/oss/v2/buckets",
            headers=headers,
            json={"bucketKey": self.bucket_key, "policyKey": "transient"},  # deleted after 24h
            timeout=30,
        )
        # 409 means bucket already exists, which is fine
        if res.status_code not in (200, 409):
            logger.error("Failed to create APS bucket: %s", res.text)
            raise Exception("Failed to ensure APS bucket")

    def _upload_to_oss(self, file_path, object_name):
        """
        Upload via the S3 signed-URL workflow:
          1. GET  /oss/v2/buckets/{bucket}/objects/{name}/signeds3upload?Content-Length={size}
             -> {"urls": [...], "uploadKey": "..."} (single part for files < 100MB)
          2. PUT  the raw bytes to each signed S3 URL (no auth headers)
          3. POST /oss/v2/buckets/{bucket}/objects/{name}/signeds3upload
             with body {"uploadKey": "..."} -> object details incl. objectId
        """
        size = os.path.getsize(file_path)
        headers = self._auth_headers()

        res = requests.get(
            f"{self.BASE_URL}/oss/v2/buckets/{self.bucket_key}/objects/{object_name}/signeds3upload",
            params={"Content-Length": str(size)},
            headers=headers,
            timeout=60,
        )
        if res.status_code != 200:
            logger.error("Failed to generate signed S3 upload URL: %s", res.text)
            raise Exception("APS Signed Upload URL Generation Failed")

        signed = res.json()
        urls = signed.get("urls") or []
        upload_key = signed.get("uploadKey")
        if not urls or not upload_key:
            logger.error("Unexpected signeds3upload response: %s", signed)
            raise Exception("APS Signed Upload Response Malformed")

        with open(file_path, "rb") as f:
            data = f.read()

        # Single-part upload (files under 100MB — our BIM files qualify).
        if len(urls) == 1:
            put = requests.put(urls[0], data=data, timeout=300)
            if put.status_code != 200:
                logger.error("Failed to upload part 1 to S3: %s %s", put.status_code, put.text[:500])
                raise Exception("APS S3 Upload Failed")
        else:
            # Multipart: split evenly and PUT each chunk to its signed URL.
            part_size, rem = divmod(size, len(urls))
            offsets = []
            pos = 0
            for i in range(len(urls)):
                length = part_size + (rem if i == 0 else 0)
                offsets.append((pos, pos + length))
                pos += length
            for i, url in enumerate(urls):
                start, end = offsets[i]
                put = requests.put(url, data=data[start:end], timeout=300)
                if put.status_code != 200:
                    logger.error("Failed to upload part %s to S3: %s", i + 1, put.text[:500])
                    raise Exception("APS S3 Multipart Upload Failed")

        res = requests.post(
            f"{self.BASE_URL}/oss/v2/buckets/{self.bucket_key}/objects/{object_name}/signeds3upload",
            headers={**headers, "Content-Type": "application/json"},
            json={"uploadKey": upload_key},
            timeout=60,
        )
        if res.status_code != 200:
            logger.error("Failed to complete APS upload: %s", res.text)
            raise Exception("APS Upload Completion Failed")

        return res.json()["objectId"]

    def _download_derivative(self, urn, derivative_urn, out_path):
        """
        Download a derivative via the signed-cookies flow. The endpoint sets
        CloudFront cookies scoped to the CDN host that must be forwarded
        manually (requests does not jar cross-domain cookies from this API).
        """
        headers = self._auth_headers()
        res = requests.get(
            f"{self.BASE_URL}/modelderivative/v2/designdata/{urn}/manifest/{quote(derivative_urn, safe='')}/signedcookies",
            headers=headers,
            timeout=60,
        )
        if res.status_code != 200:
            logger.error("Failed to fetch derivative download URL: %s", res.text)
            raise Exception("APS Derivative Download URL Failed")

        download = res.json()
        # Multiple Set-Cookie headers (CloudFront-Policy / -Signature /
        # -Key-Pair-Id). urllib3's HTTPHeaderDict keeps them separate.
        raw_cookies = res.raw.headers.getlist("Set-Cookie") if hasattr(res.raw.headers, "getlist") else []
        cookie_pairs = [sc.split(";")[0].strip() for sc in raw_cookies if "=" in sc]
        if not cookie_pairs:
            # fall back to the merged header — base64 values contain no commas,
            # so splitting on "," then filtering fragments without "=" is safe
            cookie_pairs = [
                pair.split(";")[0].strip()
                for pair in res.headers.get("Set-Cookie", "").split(",")
                if "=" in pair and not pair.strip().startswith(("Path", "Domain", "Expires", "SameSite", "Secure"))
            ]

        dl = requests.get(
            download["url"],
            headers={"Cookie": "; ".join(cookie_pairs)} if cookie_pairs else {},
            stream=True,
            timeout=600,
        )
        if dl.status_code != 200:
            logger.error("Failed to download derivative from CDN: %s %s", dl.status_code, dl.text[:300])
            raise Exception("APS Derivative CDN Download Failed")

        with open(out_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        return out_path

    def convert_rvt_to_ifc(self, file_path):
        """
        Uploads RVT, translates to IFC, downloads IFC, and returns path to the new IFC file.
        """
        self._ensure_bucket()
        file_name = os.path.basename(file_path)
        object_name = f"{int(time.time())}_{file_name}"

        # 1. Upload to OSS via S3 signed URLs
        logger.info("Uploading %s to Autodesk APS for translation...", file_name)
        object_id = self._upload_to_oss(file_path, object_name)
        urn = base64.b64encode(object_id.encode("utf-8")).decode("utf-8").rstrip("=")

        # 2. Trigger translation to IFC
        logger.info("Triggering RVT -> IFC translation job for URN: %s", urn)
        headers = {**self._auth_headers(), "Content-Type": "application/json"}
        job_res = requests.post(
            f"{self.BASE_URL}/modelderivative/v2/designdata/job",
            headers=headers,
            json={
                "input": {"urn": urn},
                "output": {"formats": [{"type": "ifc"}]},
            },
            timeout=60,
        )
        if job_res.status_code not in (200, 201):
            logger.error("Failed to trigger APS translation: %s", job_res.text)
            raise Exception("APS Translation Trigger Failed")

        # 3. Poll for completion
        manifest_url = f"{self.BASE_URL}/modelderivative/v2/designdata/{urn}/manifest"
        translation_success = False
        derivative_urn = None
        deadline = time.time() + 25 * 60  # large models can take a while

        while time.time() < deadline:
            time.sleep(10)
            man_res = requests.get(manifest_url, headers=headers, timeout=60)
            if man_res.status_code != 200:
                continue
            man_data = man_res.json()
            status = man_data.get("status", "")
            logger.info("APS translation status: %s %s%%", status, man_data.get("progress", ""))
            if status == "success":
                translation_success = True
                for derivative in man_data.get("derivatives", []):
                    for child in derivative.get("children", []):
                        if child.get("role") == "ifc" and child.get("status") == "success":
                            derivative_urn = child.get("urn")
                            break
                break
            elif status in ("failed", "timeout"):
                logger.error("APS Translation failed: %s", man_data)
                raise Exception("APS Translation Failed")

        if not translation_success or not derivative_urn:
            raise Exception("APS Translation timed out or failed to produce IFC")

        # 4. Download translated IFC
        logger.info("Translation successful. Downloading translated IFC from APS...")
        out_dir = os.path.dirname(file_path)
        out_name = re.sub(r"\.rvt$", "", file_name, flags=re.IGNORECASE) + "_translated.ifc"
        out_path = os.path.join(out_dir, out_name)
        self._download_derivative(urn, derivative_urn, out_path)

        logger.info("Successfully converted RVT to IFC at %s", out_path)
        return out_path
