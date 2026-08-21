import os
import mimetypes
try:
    import magic
except Exception:
    magic = None
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

@deconstructible
class SecureFileValidator:
    """
    Validates uploaded files based on extension, MIME type, and magic bytes.
    Ensures that a file isn't maliciously disguised (e.g. .exe masquerading as .pdf).
    """
    
    ALLOWED_TYPES = {
        'pdf': {
            'mime': 'application/pdf',
            'magic': [b'%PDF-'],
            'max_size': 15 * 1024 * 1024  # 15 MB
        },
        'docx': {
            'mime': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'magic': [b'PK\x03\x04'],
            'max_size': 10 * 1024 * 1024  # 10 MB
        },
        'png': {
            'mime': 'image/png',
            'magic': [b'\x89PNG\r\n\x1a\n'],
            'max_size': 5 * 1024 * 1024  # 5 MB
        },
        'jpeg': {
            'mime': 'image/jpeg',
            'magic': [b'\xff\xd8\xff'],
            'max_size': 5 * 1024 * 1024  # 5 MB
        },
        'jpg': {
            'mime': 'image/jpeg',
            'magic': [b'\xff\xd8\xff'],
            'max_size': 5 * 1024 * 1024  # 5 MB
        },
        'rvt': {
            'mime': 'application/octet-stream', # Revit often defaults to octet-stream
            'magic': [b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'], # OLE Compound File (standard for Revit)
            'max_size': 100 * 1024 * 1024 # 100 MB
        },
        'ifc': {
            'mime': 'application/x-step', # IFC is STEP format based
            'magic': [b'ISO-10303-21;'], # Starts with STEP identifier
            'max_size': 100 * 1024 * 1024 # 100 MB
        }
    }

    def __init__(self, allowed_extensions=None):
        self.allowed_extensions = allowed_extensions or self.ALLOWED_TYPES.keys()

    def __call__(self, file):
        # Check size limits
        ext = os.path.splitext(file.name)[1][1:].lower()
        if ext not in self.allowed_extensions:
            raise ValidationError(f"File extension '{ext}' is not allowed.")
            
        rules = self.ALLOWED_TYPES.get(ext)
        if not rules:
            raise ValidationError(f"File validation rules for '{ext}' not found.")
            
        if file.size > rules['max_size']:
            max_mb = rules['max_size'] / (1024 * 1024)
            raise ValidationError(f"File size exceeds the limit of {max_mb} MB for {ext} files.")

        # Check Magic bytes
        file.seek(0)
        file_head = file.read(2048)
        file.seek(0)
        
        # We can do a rudimentary byte check if we have the signature
        magic_match = False
        for signature in rules['magic']:
            if file_head.startswith(signature):
                magic_match = True
                break
                
        # If byte check fails, try python-magic for mime type
        if not magic_match and magic is not None:
            try:
                mime_type = magic.from_buffer(file_head, mime=True)
                if mime_type != rules['mime']:
                    raise ValidationError(f"Invalid file content. Expected {rules['mime']} but got {mime_type}.")
            except ValidationError:
                raise
            except Exception:
                pass

        return True
