import logging
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

class StorageService:
    """
    Service to handle AWS S3 / MinIO pre-signed URLs and interactions.
    If credentials are missing or errors occur, falls back gracefully to mock URLs for local development.
    """
    def __init__(self):
        self.bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', 'sitesupervise-scans')
        self.endpoint_url = getattr(settings, 'AWS_S3_ENDPOINT_URL', None)
        self.region_name = getattr(settings, 'AWS_S3_REGION_NAME', 'us-east-1')
        self.access_key = getattr(settings, 'AWS_ACCESS_KEY_ID', None)
        self.secret_key = getattr(settings, 'AWS_SECRET_ACCESS_KEY', None)

        self.s3_client = None
        if self.access_key and self.secret_key:
            try:
                self.s3_client = boto3.client(
                    's3',
                    endpoint_url=self.endpoint_url,
                    region_name=self.region_name,
                    aws_access_key_id=self.access_key,
                    aws_secret_access_key=self.secret_key,
                    config=Config(signature_version='s3v4')
                )
            except Exception as e:
                logger.warning(f"Failed to initialize S3 client: {e}. Falling back to mock implementation.")

    def generate_presigned_upload_url(self, session_id: str, data_type: str, expires_in: int = 3600) -> str:
        """
        Generates a pre-signed URL for direct upload to S3.
        """
        object_name = f"scans/{session_id}/{data_type}.bin"
        if not self.s3_client:
            logger.debug("AWS S3 client is not configured.")
            raise ValueError("S3 Client not configured")
        
        try:
            url = self.s3_client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_name,
                },
                ExpiresIn=expires_in
            )
            return url
        except ClientError as e:
            logger.error(f"Error generating presigned upload URL: {e}")
            raise

    def generate_presigned_download_url(self, session_id: str, file_name: str, expires_in: int = 3600) -> str:
        """
        Generates a pre-signed URL to download files from S3.
        """
        object_name = f"reports/{session_id}/{file_name}"
        if not self.s3_client:
            logger.debug("AWS S3 client is not configured.")
            raise ValueError("S3 Client not configured")
        
        try:
            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_name
                },
                ExpiresIn=expires_in
            )
            return url
        except ClientError as e:
            logger.error(f"Error generating presigned download URL: {e}")
            raise
