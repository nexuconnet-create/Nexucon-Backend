from rest_framework import permissions

# Roles defined in the system
ROLE_AGENCY_HEAD = "Agency Head"
ROLE_DIRECTOR = "Director"
ROLE_INSPECTOR = "Inspector"

def get_user_role(user):
    """Helper to get the government role of a user."""
    if not hasattr(user, 'government_profile') or not user.government_profile:
        return None
    
    if not user.government_profile.role:
        return None
        
    return user.government_profile.role.name

class IsAgencyHead(permissions.BasePermission):
    """
    Agency Head has full access (CREATE, READ, UPDATE, DELETE/Archive).
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return get_user_role(request.user) == ROLE_AGENCY_HEAD

class IsDirector(permissions.BasePermission):
    """
    Director can CREATE, READ, UPDATE all records in their department, but CANNOT DELETE without review.
    We handle DELETE restriction at the view level or simply deny it here.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        role = get_user_role(request.user)
        if role not in [ROLE_DIRECTOR, ROLE_AGENCY_HEAD]:
            return False
            
        # Deny standard DELETE requests for Director (unless specific view overrides)
        if request.method == 'DELETE' and role == ROLE_DIRECTOR:
            return False
            
        return True

class IsInspector(permissions.BasePermission):
    """
    Inspector can CREATE, READ all, UPDATE their own records, but CANNOT DELETE anything.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
            
        role = get_user_role(request.user)
        if role not in [ROLE_INSPECTOR, ROLE_DIRECTOR, ROLE_AGENCY_HEAD]:
            return False
            
        # Deny standard DELETE requests for Inspector
        if request.method == 'DELETE':
            return False
            
        return True

    def has_object_permission(self, request, view, obj):
        role = get_user_role(request.user)
        
        # Agency Head and Director can read/update all
        if role in [ROLE_AGENCY_HEAD, ROLE_DIRECTOR]:
            return True
            
        # Inspector can only update their own records
        if request.method in ['PUT', 'PATCH']:
            # Assuming models have a 'created_by' or 'user' or 'inspector' field
            # The view must properly define this, or we fallback to True if not present
            owner_fields = ['created_by', 'inspector', 'user', 'applicant']
            for field in owner_fields:
                if hasattr(obj, field):
                    return getattr(obj, field) == request.user
                    
        # Read-only operations are allowed for Inspector
        if request.method in permissions.SAFE_METHODS:
            return True
            
        return False
