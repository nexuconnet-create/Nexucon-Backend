import logging
import urllib.parse
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.notifications.email_service import EmailService
from apps.notifications.models import Notification, EmailDelivery
from apps.audit.models import AuditEvent

logger = logging.getLogger(__name__)
User = get_user_model()


def resolve_project_inspectors(project):
    """
    Resolve all active field inspectors and test operators associated with a project.
    Inspectors can be:
    1. project.assigned_inspector_user (direct FK to User)
    2. project.assigned_inspector (free-text name matching an active inspector User)
    3. PUNDIT test operators & scan recorders (PUNDITTest.operator, PUNDITTest.created_by)
    4. Active inspectors assigned to inspection submissions on this project
    5. Stakeholder inspectors attached to the project/agency
    """
    inspectors = []
    seen_emails = set()

    def _add_user(user, role_desc=None):
        if not user or not getattr(user, 'is_active', False):
            return
        email = (getattr(user, 'email', None) or '').strip().lower()
        if not email or email in seen_emails:
            return
        seen_emails.add(email)
        full_name = user.get_full_name() or user.username or email.split('@')[0]
        inspectors.append({
            'user': user,
            'user_id': str(user.id),
            'email': email,
            'name': full_name,
            'role': role_desc or getattr(user, 'role', 'Field Inspector'),
        })

    # 1. Project assigned inspector user
    if getattr(project, 'assigned_inspector_user_id', None):
        _add_user(project.assigned_inspector_user, 'Assigned Project Inspector')

    # 2. Named inspector string lookup
    named = (getattr(project, 'assigned_inspector', '') or '').strip()
    if named:
        from apps.projects.models import resolve_inspector_user
        try:
            found_user, _ = resolve_inspector_user(named)
            if found_user:
                _add_user(found_user, 'Assigned Project Inspector')
        except Exception:
            pass

        # Also search by first/last name or email if not resolved
        if named.lower() not in seen_emails:
            name_parts = named.split()
            first = name_parts[0] if name_parts else named
            last = name_parts[-1] if len(name_parts) > 1 else ''
            candidates = User.objects.filter(is_active=True).filter(
                first_name__icontains=first
            )
            if last:
                candidates = candidates.filter(last_name__icontains=last)
            for c in candidates[:3]:
                _add_user(c, 'Assigned Inspector')

    # 3. PUNDIT test operators & creators
    try:
        from apps.digital_eye.models import PUNDITTest
        tests = PUNDITTest.objects.filter(project=project).select_related('operator', 'created_by')
        for t in tests:
            if t.operator:
                _add_user(t.operator, 'PUNDIT Device Operator')
            if t.created_by:
                _add_user(t.created_by, 'Field Scan Auditor')
    except Exception as e:
        logger.warning(f"Error querying PUNDIT operators for project {project.id}: {e}")

    # 4. Inspections assigned inspectors
    try:
        from apps.inspections.models import Inspection
        inspections = Inspection.objects.filter(
            project=project, inspector__isnull=False
        ).select_related('inspector')
        for insp in inspections:
            _add_user(insp.inspector, 'Statutory Site Inspector')
    except Exception as e:
        logger.warning(f"Error querying inspections for project {project.id}: {e}")

    # 5. Stakeholder inspectors
    try:
        from apps.stakeholders.models import Inspector as StakeholderInspector
        st_inspectors = StakeholderInspector.objects.filter(
            is_active=True, user__isnull=False, user__is_active=True
        ).select_related('user')
        # If there are active stakeholder inspectors in the system and we have fewer than 2 inspectors, add them
        if len(inspectors) == 0:
            for si in st_inspectors[:3]:
                _add_user(si.user, si.role_title or 'Accredited Inspector')
    except Exception as e:
        logger.warning(f"Error querying stakeholder inspectors: {e}")

    return inspectors


def get_or_create_authorized_download_token(project, report_reference, content_key, recipient_email, recipient_name):
    """
    Generate or retrieve an approved access token for the inspector so the direct download link
    in their notification email immediately works without getting 403 Forbidden.
    """
    from apps.documents.models import DocumentAccessRequest
    import uuid

    req = DocumentAccessRequest.objects.filter(
        report_digest=content_key,
        requester_email__iexact=recipient_email.strip(),
        status='APPROVED'
    ).first()

    if not req:
        token = str(uuid.uuid4())
        req = DocumentAccessRequest.objects.create(
            project=project,
            report_reference=report_reference,
            report_digest=content_key,
            document_title="BS 1881-203 Ultrasonic Pulse Velocity (UPV) NDT Report",
            requester_name=recipient_name,
            requester_email=recipient_email,
            requester_organization="Nexucon Field Surveillance & Materials Testing Authority",
            requester_role="Authorized Field Inspector",
            purpose="Statutory inspection verification and NDT compliance review.",
            status='APPROVED',
            access_token=token,
            reviewed_by_name="Nexucon Notification Engine",
            reviewed_at=timezone.now(),
            review_notes="Pre-authorized statutory inspector distribution on NDT report finalization."
        )
    return str(req.access_token)


def notify_inspectors_ndt_report_ready(
    archived_report_id,
    sender=None,
    recipient_emails=None,
    custom_message=None,
    force_resend=False
):
    """
    Alert designated inspectors via branded HTML email and In-App notification
    that an official Nondestructive Testing (NDT) report is ready for download.
    """
    from apps.reports.models import ArchivedReport
    try:
        if isinstance(archived_report_id, ArchivedReport):
            archived = archived_report_id
        else:
            archived = ArchivedReport.objects.select_related('project').get(id=archived_report_id)
    except ArchivedReport.DoesNotExist:
        logger.error(f"ArchivedReport {archived_report_id} not found.")
        return {'success': False, 'error': 'Archived report not found.'}

    project = archived.project
    all_inspectors = resolve_project_inspectors(project)

    # Filter by specific recipients if specified
    if recipient_emails:
        target_emails = {e.strip().lower() for e in recipient_emails if e and e.strip()}
        target_inspectors = [i for i in all_inspectors if i['email'] in target_emails]
        
        # If recipient_emails contained addresses not in resolved inspectors, still notify them
        existing_targets = {i['email'] for i in target_inspectors}
        for email in target_emails:
            if email not in existing_targets:
                user_match = User.objects.filter(email__iexact=email, is_active=True).first()
                target_inspectors.append({
                    'user': user_match,
                    'user_id': str(user_match.id) if user_match else None,
                    'email': email,
                    'name': user_match.get_full_name() if user_match else email.split('@')[0],
                    'role': 'Field Inspector'
                })
    else:
        target_inspectors = all_inspectors

    if not target_inspectors:
        logger.info(f"No inspectors found to notify for project {project.id} ({project.name}).")
        return {
            'success': True,
            'notified_count': 0,
            'recipients': [],
            'message': 'No inspectors assigned or identified for this project.'
        }

    frontend_url = EmailService.get_frontend_url()
    api_url = getattr(settings, 'API_BASE_URL', 'https://api.nexucon.net').rstrip('/')

    encoded_ref = urllib.parse.quote(archived.report_reference or '')
    encoded_digest = urllib.parse.quote(archived.content_key or '')
    verify_url = f"{frontend_url}/verify/report?ref={encoded_ref}&digest={encoded_digest}"
    dashboard_url = f"{frontend_url}/inspector/dashboard/digital-eye/pundit?project={project.id}"

    notified = []
    errors = []

    for inspector in target_inspectors:
        email = inspector['email']
        name = inspector['name']
        user = inspector['user']

        idempotency_key = f"NDT_READY:{archived.content_key}:{email}"
        if not force_resend:
            existing_delivery = EmailDelivery.objects.filter(
                idempotency_key=idempotency_key,
                status__in=['SENT', 'DELIVERED']
            ).first()
            if existing_delivery:
                logger.info(f"Inspector {email} already notified for report {archived.report_reference}.")
                notified.append({
                    'name': name,
                    'email': email,
                    'role': inspector['role'],
                    'status': 'already_notified'
                })
                continue

        # 1. Create token for direct seamless download link
        access_token = get_or_create_authorized_download_token(
            project=project,
            report_reference=archived.report_reference,
            content_key=archived.content_key,
            recipient_email=email,
            recipient_name=name
        )
        download_url = (
            f"{api_url}/api/v1/reports/verify/download/"
            f"?ref={encoded_ref}&digest={encoded_digest}&token={access_token}"
        )

        # 2. Dispatch in-app notification if user account exists
        in_app_notif = None
        if user:
            try:
                in_app_notif = Notification.objects.create(
                    recipient=user,
                    recipient_role='inspector',
                    category='INSPECTIONS',
                    event_type='NDT_REPORT_READY',
                    title=f"NDT Report Ready: {archived.report_reference}",
                    message=(
                        f"The certified BS 1881-203 Ultrasonic Pulse Velocity (UPV) report "
                        f"for '{project.name}' is finalized and ready for download."
                    ),
                    snippet=f"Report {archived.report_reference} ({archived.compliance_status}) is ready for download.",
                    priority='High',
                    entity_type='ArchivedReport',
                    entity_id=str(archived.id),
                    action_url=f"/inspector/dashboard/digital-eye/pundit?project={project.id}",
                    action_required="Download and review certified test dossier",
                    metadata={
                        'project_id': str(project.id),
                        'project_name': project.name,
                        'report_reference': archived.report_reference,
                        'content_key': archived.content_key,
                        'compliance_status': archived.compliance_status,
                        'download_url': download_url,
                        'verify_url': verify_url
                    }
                )
            except Exception as e:
                logger.warning(f"Could not create in-app notification for {email}: {e}")

        # 3. Dispatch branded HTML email via EmailService
        email_res = EmailService.send_ndt_report_ready_email(
            email=email,
            inspector_name=name,
            project_name=project.name,
            report_reference=archived.report_reference,
            download_url=download_url,
            compliance_status=archived.compliance_status,
            test_count=archived.test_count,
            assessed_count=archived.assessed_count,
            passed_count=archived.passed_count,
            sha256_checksum=archived.sha256_checksum,
            dashboard_url=dashboard_url,
            verify_url=verify_url,
            sealed_date=archived.created_at.strftime('%d %b %Y, %H:%M UTC') if archived.created_at else None,
            custom_message=custom_message
        )

        # 4. Record EmailDelivery audit ledger
        delivery_status = 'SENT' if email_res.get('success') else 'FAILED'
        try:
            EmailDelivery.objects.update_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    'notification': in_app_notif,
                    'recipient_email': email,
                    'recipient_user': user,
                    'template_key': 'ndt_report_ready',
                    'subject': f"📋 NDT Report Ready: {archived.report_reference} - {project.name}",
                    'provider': 'resend',
                    'provider_message_id': email_res.get('id'),
                    'status': delivery_status,
                    'attempt_count': 1,
                    'last_attempt_at': timezone.now(),
                    'sent_at': timezone.now() if delivery_status == 'SENT' else None,
                    'failed_at': timezone.now() if delivery_status == 'FAILED' else None,
                    'failure_reason': email_res.get('error') if not email_res.get('success') else None,
                    'metadata': {
                        'project_id': str(project.id),
                        'project_name': project.name,
                        'report_reference': archived.report_reference,
                        'download_url': download_url
                    }
                }
            )
        except Exception as e:
            logger.warning(f"Could not record EmailDelivery for {email}: {e}")

        if email_res.get('success'):
            notified.append({
                'name': name,
                'email': email,
                'role': inspector['role'],
                'status': 'sent',
                'delivery_id': email_res.get('id')
            })
        else:
            errors.append({
                'email': email,
                'error': email_res.get('error')
            })

    # Log audit event
    try:
        AuditEvent.objects.create(
            user=sender if getattr(sender, 'is_authenticated', False) else None,
            action="NDT_REPORT_INSPECTORS_NOTIFIED",
            resource_type="ArchivedReport",
            resource_id=str(archived.id),
            new_state={
                'report_reference': archived.report_reference,
                'notified_count': len([n for n in notified if n['status'] == 'sent']),
                'recipients': [n['email'] for n in notified]
            }
        )
    except Exception:
        pass

    return {
        'success': True,
        'notified_count': len([n for n in notified if n['status'] == 'sent']),
        'total_recipients': len(target_inspectors),
        'recipients': notified,
        'errors': errors
    }
