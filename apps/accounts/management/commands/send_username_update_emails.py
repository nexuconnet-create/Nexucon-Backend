from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.accounts.views import send_username_update_email_to_user

User = get_user_model()


class Command(BaseCommand):
    help = "Send email and in-app notifications to existing users prompting them to update or auto-generate their username in Settings/Profile."

    def handle(self, *args, **options):
        users = User.objects.filter(is_active=True)
        notified_count = 0
        for u in users:
            uname = (u.username or '').strip()
            uemail = (u.email or '').strip()
            if not uname or '@' in uname or (uemail and uname.lower() == uemail.lower()):
                res = send_username_update_email_to_user(u)
                notified_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Notified {u.email} (suggested: @{res.get('suggested_username')}) -> sent={res.get('sent')}"
                    )
                )
        self.stdout.write(self.style.SUCCESS(f"Completed: {notified_count} existing user(s) notified."))

