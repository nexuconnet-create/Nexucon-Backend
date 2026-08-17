import threading

_local = threading.local()

class AuditMiddleware:
    """
    Middleware to capture the current request's user and IP address.
    Stores it in thread-local storage so that signals can access it when models are saved.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None
        
        # Get client IP
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR')
            
        _local.ip_address = ip

        response = self.get_response(request)

        # Clean up
        _local.user = None
        _local.ip_address = None

        return response

def get_current_user():
    return getattr(_local, 'user', None)

def get_current_ip():
    return getattr(_local, 'ip_address', None)
