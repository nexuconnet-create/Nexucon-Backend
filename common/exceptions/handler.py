import traceback
import logging
from django.conf import settings
from rest_framework.views import exception_handler
from common.responses.standard import StandardResponse

logger = logging.getLogger(__name__)

def custom_exception_handler(exc, context):
    """
    Custom exception handler that wraps DRF errors into our StandardResponse format.
    """
    response = exception_handler(exc, context)

    if response is not None:
        errors = response.data
        message = "A validation or processing error occurred."
        if isinstance(errors, dict) and "detail" in errors:
            message = errors.pop("detail")
        elif isinstance(errors, list) and len(errors) > 0 and isinstance(errors[0], str):
            message = errors[0]
            
        return StandardResponse.error(
            message=str(message),
            errors=errors,
            status_code=response.status_code
        )

    # For unhandled exceptions, log the full traceback and return clean JSON response
    view_name = context.get('view', 'UnknownView') if context else 'UnknownView'
    logger.error(f"Unhandled 500 in {view_name}: {str(exc)}\n{traceback.format_exc()}")
    print(f"[ERROR 500] in {view_name}: {str(exc)}\n{traceback.format_exc()}")

    error_msg = f"Internal Server Error: {str(exc)}" if getattr(settings, 'DEBUG', False) else "An internal server error occurred."
    return StandardResponse.error(
        message=error_msg,
        errors={'exception': str(exc)} if getattr(settings, 'DEBUG', False) else None,
        status_code=500
    )
