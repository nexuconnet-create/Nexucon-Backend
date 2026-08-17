import json
import logging
from django.utils.deprecation import MiddlewareMixin
from apps.audit.models import AuditEvent

logger = logging.getLogger(__name__)

class AuditLogMiddleware(MiddlewareMixin):
    """
    Middleware to automatically log CRUD operations to the AuditEvent model.
    Intercepts non-safe methods (POST, PUT, PATCH, DELETE) and records them.
    """
    def process_request(self, request):
        # We capture the body here before the view processes it, if needed for previous_state
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            request._body = request.body
            
    def process_response(self, request, response):
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            # Only log successful or client error operations, avoid logging 500 errors unless needed
            if response.status_code >= 200 and response.status_code < 500:
                self._log_action(request, response)
        return response
        
    def _log_action(self, request, response):
        try:
            user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None
            
            # Map HTTP method to CRUD action
            action_map = {
                'POST': 'CREATE',
                'PUT': 'UPDATE',
                'PATCH': 'UPDATE',
                'DELETE': 'DELETE (Archive)'
            }
            action = action_map.get(request.method, request.method)
            
            # Extract resource info from path e.g. /api/permits/123/ -> resource_type: permits, id: 123
            path_parts = [p for p in request.path.split('/') if p]
            resource_type = path_parts[1] if len(path_parts) > 1 else 'unknown'
            resource_id = path_parts[2] if len(path_parts) > 2 else 'N/A'
            
            ip_address = self._get_client_ip(request)
            
            # We would optimally use signals for exact previous/new state, 
            # but middleware is good for high-level capture.
            
            AuditEvent.objects.create(
                user=user,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                previous_state=None, # To be fully populated by signals or view logic for accuracy
                new_state={"path": request.path, "status": response.status_code}
            )
        except Exception as e:
            logger.error(f"Failed to log audit event: {str(e)}")

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
