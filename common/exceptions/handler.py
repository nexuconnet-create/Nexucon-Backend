from rest_framework.views import exception_handler
from rest_framework.response import Response
from common.responses.standard import StandardResponse

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

    # For unhandled exceptions, let Django handle it (typically 500)
    # In production, we might want to catch these here too.
    return None
