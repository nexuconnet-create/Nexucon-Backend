import cloudinary
import cloudinary.uploader
import cloudinary.api
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class CloudinaryService:
    """Simple wrapper around Cloudinary SDK for uploading files.
    The Cloudinary configuration (CLOUDINARY_URL) should be set in the environment
    or via Django settings. The SDK reads ``CLOUDINARY_URL`` automatically.
    """

    @staticmethod
    def _configure():
        if not cloudinary.config().api_key:
            import os
            cloudinary.config(
                cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
                api_key=os.environ.get("CLOUDINARY_API_KEY"),
                api_secret=os.environ.get("CLOUDINARY_API_SECRET")
            )

    @staticmethod
    def upload_file(file_obj, folder: str = None) -> str:
        """Upload ``file_obj`` to Cloudinary and return the secure URL.
        If ``folder`` is provided, the asset will be placed under that folder.
        """
        CloudinaryService._configure()
        try:
            options = {"resource_type": "auto"}
            if folder:
                options["folder"] = folder

            # Cloudinary blocks public delivery of any URL ending in ".pdf"
            # (account security default -> "deny or ACL failure"), and raw
            # uploads take their extension from the source filename. Upload
            # PDFs from a nameless stream under an extension-less public_id
            # so the raw URL stays publicly downloadable.
            source_name = str(getattr(file_obj, 'name', '') or file_obj or '')
            if source_name.lower().endswith('.pdf'):
                import io
                import uuid
                if isinstance(file_obj, str):
                    with open(file_obj, 'rb') as fh:
                        payload = io.BytesIO(fh.read())
                else:
                    payload = io.BytesIO(file_obj.read())
                options["resource_type"] = "raw"
                options["public_id"] = uuid.uuid4().hex
                result = cloudinary.uploader.upload(payload, **options)
                url = result.get("secure_url")
                logger.info(f"Uploaded file to Cloudinary: {url}")
                return url

            file_size = getattr(file_obj, 'size', 0)
            if file_size > 10 * 1024 * 1024:
                result = cloudinary.uploader.upload_large(file_obj, **options)
            else:
                result = cloudinary.uploader.upload(file_obj, **options)
            
            url = result.get("secure_url")
            logger.info(f"Uploaded file to Cloudinary: {url}")
            return url
        except Exception as e:
            logger.error(f"Cloudinary upload failed: {e}")
            raise

