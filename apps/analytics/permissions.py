from rest_framework import permissions

class CanViewIndustryAnalytics(permissions.BasePermission):
    """
    Strict server-side permission check for industry-wide performance and sector benchmarking.
    Requires 'analytics.view_industry' permission or Director/Executive government role.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser or request.user.is_staff:
            return True
            
        if request.user.has_perm('analytics.view_industry'):
            return True
            
        role = getattr(request.user, 'role', '').upper()
        uname = getattr(request.user, 'username', '').lower()
        if 'director' in uname or 'gov' in uname:
            return True
            
        allowed_roles = ['DIRECTOR', 'GOVERNMENT', 'GOVERNMENT_OFFICIAL', 'PERMANENT_SECRETARY', 'ADMIN', 'EXECUTIVE']
        if any(r in role for r in allowed_roles):
            return True
            
        return False


class CanViewFinancialAnalytics(permissions.BasePermission):
    """
    Permission check for project financial overview, revenue collections, and expenditure.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
            
        if request.user.is_superuser or request.user.is_staff:
            return True
            
        if request.user.has_perm('analytics.view_financial'):
            return True
            
        role = getattr(request.user, 'role', '').upper()
        uname = getattr(request.user, 'username', '').lower()
        if 'director' in uname or 'gov' in uname:
            return True

        allowed_roles = ['DIRECTOR', 'FINANCE', 'FINANCE_OFFICER', 'PERMANENT_SECRETARY', 'ADMIN', 'EXECUTIVE']
        if any(r in role for r in allowed_roles):
            return True
            
        return False


class CanExportReports(permissions.BasePermission):
    """
    Permission check for generating and downloading PDF/CSV/XLSX analytics reports.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return True
