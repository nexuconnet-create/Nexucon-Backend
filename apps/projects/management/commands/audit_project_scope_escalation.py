"""
Read-only diagnostic: what the free-text `assigned_inspector` name match would
have granted, and which rows it cannot account for.

`common.permissions.scoped_projects` **used to** give an Inspector the projects
where

    Q(inspections__inspector=user) | Q(assigned_inspector=<their name as text>)

The second half was a privilege escalation. `assigned_inspector` is a free-text
`CharField` on `Project`, so it was matched against a *string*, not a user:

  * anyone who could type a name into that field granted that person's project
    access to whoever happened to share the name;
  * two users with the same full name — or one user with no full name, whose
    email was used instead — resolved to the same projects;
  * editing your own profile name changed which projects you could see.

That match has since been removed: `scoped_projects` now tests the
`assigned_inspector_user` foreign key and nothing else, and
`backfill_project_assigned_inspector_user` populated it.

This command is kept because its report still has a job after the switch. The
text column remains a display mirror, and these are the rows an administrator
has to look at:

  * text that resolves to no active user — nobody was ever granted anything by
    it, and the project now reads as unassigned;
  * text that resolves to several — the ambiguity the escalation rested on;
  * projects the name match reached that no real assignment reaches.

It writes nothing. `--dry-run` is accepted so the command line says so
explicitly; there is no other mode, and `--execute` is refused rather than
silently ignored.

Usage:
    python manage.py audit_project_scope_escalation
    python manage.py audit_project_scope_escalation --dry-run
    python manage.py audit_project_scope_escalation --all-roles
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.projects.models import Project
from common.permissions import ROLE_INSPECTOR

User = get_user_model()


class Command(BaseCommand):
    help = (
        'Read-only. Report what the removed free-text `Project.assigned_inspector` '
        'name match would have granted, the names that resolve to more than one '
        'user, and the text that resolves to nobody — the rows an administrator '
        'has to clean up now that access follows the foreign key.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Accepted and implied: this command never writes anything. '
                 'Stated on the command line so the read-only nature is '
                 'visible in the invocation.')
        parser.add_argument(
            '--execute', action='store_true',
            help='Refused. This command is a diagnostic; the command that '
                 'writes is `backfill_project_assigned_inspector_user`.')
        parser.add_argument(
            '--all-roles', action='store_true',
            help='Audit every user, not only those holding the Inspector role. '
                 'The escalation is reachable by any authenticated user whose '
                 'name matches, so this is the wider view.')

    def handle(self, *args, **options):
        if options['execute']:
            self.stderr.write(
                'This command is read-only and has no --execute mode. The '
                'command that writes the new foreign key is '
                '`backfill_project_assigned_inspector_user`.')
            return

        projects = list(Project.objects.all().values('id', 'name', 'assigned_inspector'))
        user_rows = list(User.objects.filter(is_active=True).values(
            'id', 'email', 'first_name', 'last_name'))

        if options['all_roles']:
            users = user_rows
        else:
            # Resolved through Profile rather than by calling `user_role_name`
            # on each row: that helper takes a model instance and looks the
            # profile up again, which would be one query per user here.
            from apps.government.models import Profile
            inspector_ids = set(Profile.objects.filter(
                role__name=ROLE_INSPECTOR).values_list('user_id', flat=True))
            users = [u for u in user_rows if u['id'] in inspector_ids]

        # --- the name match, gathered per user -------------------------
        # name -> the users that resolve to it. More than one is the collision
        # that makes the match unsafe: both users see the same projects.
        by_name = {}
        for user in users:
            by_name.setdefault(self._name_of(user), []).append(user)

        rows = []
        reachable_by_name = set()
        for name, holders in sorted(by_name.items()):
            matched = [p for p in projects
                       if (p['assigned_inspector'] or '').strip() == name]
            if not matched:
                continue
            reachable_by_name.update(p['id'] for p in matched)
            for user in holders:
                # What the platform grants this user today: a real inspection,
                # or the foreign key. Anything in `matched` outside this set was
                # reachable only through the text, and no longer is.
                granted = set(Project.objects.filter(
                    Q(inspections__inspector_id=user['id'])
                    | Q(assigned_inspector_user_id=user['id'])
                ).values_list('id', flat=True))
                via_name = {p['id'] for p in matched}
                rows.append({
                    'user': user,
                    'name': name,
                    'shared_with': [h for h in holders if h['id'] != user['id']],
                    'total': len(matched),
                    'only_by_name': len(via_name - granted),
                })

        # --- assignments that resolve to nobody ------------------------
        unmatched = [p for p in projects
                     if (p['assigned_inspector'] or '').strip()
                     and (p['assigned_inspector'] or '').strip() not in by_name]

        self.stdout.write('Project scope: `assigned_inspector` legacy audit')
        self.stdout.write('=' * 68)
        self.stdout.write(
            f'Projects: {len(projects)}   '
            f'Users in scope: {len(users)}   '
            f'Non-empty assigned_inspector: '
            f'{sum(1 for p in projects if (p["assigned_inspector"] or "").strip())}')

        if not rows:
            self.stdout.write('')
            self.stdout.write(
                'No active user\'s name matches any project\'s '
                'assigned_inspector text, so the removed name match would have '
                'granted nobody anything.')
        else:
            self.stdout.write('')
            self.stdout.write('Per user (projects carrying their name):')
            header = (f'  {"user":<34} {"name":<24} {"matched":>7} '
                      f'{"not-granted":>12}  collisions')
            self.stdout.write(header)
            self.stdout.write('  ' + '-' * (len(header) - 2))
            for row in sorted(rows, key=lambda r: -r['only_by_name']):
                collisions = (
                    ', '.join(h['email'] for h in row['shared_with'])
                    if row['shared_with'] else '—')
                label = f"{row['user']['email']}"
                self.stdout.write(
                    f'  {label:<34} {row["name"]:<24} {row["total"]:>7} '
                    f'{row["only_by_name"]:>12}  {collisions}')

        self.stdout.write('')
        self.stdout.write('Names resolving to more than one active user:')
        collisions = {n: h for n, h in by_name.items() if len(h) > 1}
        if not collisions:
            self.stdout.write('  none')
        else:
            for name, holders in sorted(collisions.items()):
                self.stdout.write(
                    f'  {name!r} -> ' + ', '.join(h['email'] for h in holders))

        self.stdout.write('')
        self.stdout.write('Assignments resolving to no active user:')
        if not unmatched:
            self.stdout.write('  none')
        else:
            for project in unmatched[:50]:
                self.stdout.write(
                    f'  {project["name"]!r} says '
                    f'{project["assigned_inspector"]!r}')
            if len(unmatched) > 50:
                self.stdout.write(f'  … and {len(unmatched) - 50} more')

        # --- the verdict -----------------------------------------------
        only_by_name = sum(r['only_by_name'] for r in rows)
        self.stdout.write('')
        self.stdout.write('-' * 68)
        self.stdout.write(
            f'Projects carrying a user\'s name that no assignment or foreign '
            f'key grants them: {only_by_name}')
        self.stdout.write(
            f'Assignments that resolve to no active user: {len(unmatched)}')
        self.stdout.write(
            f'Names shared by more than one active user: {len(collisions)}')
        self.stdout.write('')
        if collisions:
            self.stdout.write(
                'STOP: at least one name resolves to more than one user. Those '
                'projects\' text cannot say who was meant, and the foreign key '
                'holds at most one of them. Decide who each assignment belongs '
                'to and correct `assigned_inspector`.')
        elif unmatched:
            self.stdout.write(
                'CAUTION: some assignments resolve to no active user. Nobody '
                'holds those projects as inspector; they read as unassigned. '
                'Confirm that is correct, or assign them.')
        elif only_by_name:
            self.stdout.write(
                f'REVIEW: {only_by_name} project(s) carry a user\'s name '
                'without granting them anything. If the text is stale, correct '
                'it; if somebody was meant to hold the project, assign the '
                'foreign key.')
        else:
            self.stdout.write(
                'CLEAR: every project carrying a user\'s name is also held by '
                'that user through a real inspection or the foreign key. No '
                'text is doing work the key cannot account for.')
        self.stdout.write('')
        self.stdout.write('Nothing was written. This command is read-only.')

    # ------------------------------------------------------------------

    @staticmethod
    def _name_of(user_values):
        """The exact string `scoped_projects` builds for this user.

        It is `str(user.get_full_name() or user.email)` on the model instance, so
        it has to be reproduced from the values dict the same way — including
        the `or email` fallback, which is how a user with no name ends up
        matched against an email address typed into a project field.
        """
        full_name = f"{user_values['first_name']} {user_values['last_name']}".strip()
        return str(full_name or user_values['email'])
