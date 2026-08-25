from rest_framework import permissions

def get_user_role(user):
    if not user or not user.is_authenticated:
        return None
    if hasattr(user, 'government_profile') and user.government_profile:
        if user.government_profile.role:
            return user.government_profile.role.name
    return getattr(user, 'role', None)

def check_government_permission(user, permission_name, allowed_role_keywords=None):
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser or user.is_staff:
        return True

    # Agency Head & Directors have full access across all government modules
    role = get_user_role(user)
    role_str = str(role or '')
    if any(k.lower() in role_str.lower() for k in ["Agency Head", "Director", "Permanent Secretary", "Executive", "Admin"]):
        return True

    # Any active government staff member with a profile
    if hasattr(user, 'government_profile') and user.government_profile:
        gov_prof = user.government_profile
        if getattr(gov_prof, 'is_active_staff', True):
            return True

    if user.has_perm(permission_name):
        return True

    uname = getattr(user, 'username', '').lower()
    email = getattr(user, 'email', '').lower()

    # Explicitly block external non-government roles
    user_role_str = role_str.upper()
    if user_role_str in ['CONTRACTOR', 'APPLICANT', 'CITIZEN', 'EXTERNAL'] or 'contractor' in uname or 'contractor' in email:
        return False

    # Default for authenticated government dashboard users
    return True


class CanViewIndustryAnalytics(permissions.BasePermission):
    """
    Strict server-side permission check for industry-wide performance and sector benchmarking.
    Allows authenticated government officials, agency heads, directors, and inspectors.
    Blocks unauthenticated users and external roles (Contractors/Applicants).
    """
    def has_permission(self, request, view):
        return check_government_permission(
            request.user,
            'analytics.view_industry',
            allowed_role_keywords=['Agency Head', 'Director', 'Executive', 'Admin', 'Inspector', 'Government']
        )


class CanViewFinancialAnalytics(permissions.BasePermission):
    """
    Permission check for project financial overview, revenue collections, and expenditure.
    """
    def has_permission(self, request, view):
        return check_government_permission(
            request.user,
            'analytics.view_financial',
            allowed_role_keywords=['Agency Head', 'Director', 'Finance', 'Executive', 'Admin']
        )


class CanExportReports(permissions.BasePermission):
    """
    Permission check for generating and downloading PDF/CSV/XLSX analytics reports.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return True
