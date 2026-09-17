"""
Populate `Project.assigned_inspector_user` from the free-text
`Project.assigned_inspector`, then prove that doing so took no access away.

`common.permissions.scoped_projects` used to grant an Inspector every project
whose `assigned_inspector` text equalled their own name. That match is being
replaced by a real foreign key (`assigned_inspector_user`), and a foreign key
can only hold one person — so this command has to answer the question the switch
raises: **who loses a project?**

It resolves through `apps.projects.models.resolve_inspector_user`, the same
function the live save path uses, so the backfill cannot assign a user the
runtime would have refused, or refuse one it would have assigned. A name that
matches nobody, or matches more than one active user, resolves to nothing and is
reported as such — never guessed.

The parity report is the gate. Before writing, it simulates the whole backfill
in memory and prints, per user, the projects reachable by name versus the
projects reachable by the simulated foreign key. If anybody would lose access,
the command says so and does not write unless you insist.

Read-only by default. `--execute` writes.

Usage:
    python manage.py backfill_project_assigned_inspector_user
    python manage.py backfill_project_assigned_inspector_user --execute
    python manage.py backfill_project_assigned_inspector_user --execute --force
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.projects.models import Project, resolve_inspector_user

User = get_user_model()


def inspector_name(user):
    """The string `assigned_inspector` carried for this user before the switch.

    This is `str(user.get_full_name() or user.email)`, which is what
    `scoped_projects` built. Kept as a module function so the parity comparison
    below and the tests can both name the old rule instead of restating it.
    """
    return str(user.get_full_name() or user.email)


class Command(BaseCommand):
    help = (
        'Populate Project.assigned_inspector_user from the free-text '
        'assigned_inspector, reporting the projects reachable only through the '
        'old name match. Read-only without --execute.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute', action='store_true',
            help='Write the resolved assignments. Without it the command only '
                 'simulates and reports.')
        parser.add_argument(
            '--force', action='store_true',
            help='With --execute, write even though the parity report shows '
                 'users losing projects. Only for the case where you have '
                 'reviewed the losses and they are correct.')

    def handle(self, *args, **options):
        execute = options['execute']
        projects = list(Project.objects.all().order_by('reference_number'))

        # --- the plan, built with the rule the runtime uses -------------
        plan = []          # (project, resolved_user, candidate_count)
        ambiguous = []     # text matched more than one active user
        unmatched = []     # text matched no active user
        already = []       # FK already populated

        for project in projects:
            if project.assigned_inspector_user_id:
                already.append(project)
                continue
            wanted = (project.assigned_inspector or '').strip()
            if not wanted:
                continue
            user, candidates = resolve_inspector_user(wanted)
            plan.append((project, user, candidates))
            if user is None:
                (ambiguous if candidates > 1 else unmatched).append(
                    (project, wanted, candidates))

        resolved = [(p, u) for p, u, _ in plan if u is not None]

        self.stdout.write('Backfill: Project.assigned_inspector_user')
        self.stdout.write('=' * 68)
        self.stdout.write(
            f'Projects: {len(projects)}   '
            f'already assigned (skipped): {len(already)}   '
            f'non-empty text with no assignment: {len(plan)}')
        self.stdout.write(
            f'Resolvable: {len(resolved)}   '
            f'ambiguous: {len(ambiguous)}   '
            f'unmatched: {len(unmatched)}')

        if plan:
            self.stdout.write('')
            self.stdout.write('Resolution:')
            for project, user, _ in plan:
                target = user.email if user is not None else '—'
                self.stdout.write(
                    f'  {project.reference_number:<22} '
                    f'{(project.assigned_inspector or "")!r:<36} -> {target}')

        if ambiguous:
            self.stdout.write('')
            self.stdout.write('Names matching more than one active user '
                              '(left unassigned):')
            for project, wanted, count in ambiguous:
                self.stdout.write(
                    f'  {project.reference_number} says {wanted!r} '
                    f'({count} matches)')

        if unmatched:
            self.stdout.write('')
            self.stdout.write('Names matching no active user '
                              '(left unassigned):')
            for project, wanted, count in unmatched[:50]:
                self.stdout.write(
                    f'  {project.reference_number} says {wanted!r}')
            if len(unmatched) > 50:
                self.stdout.write(f'  … and {len(unmatched) - 50} more')

        # --- parity: the old name match against the simulated FK -------
        simulated = {p.id: u.id for p, u in resolved}
        for project in already:
            simulated[project.id] = project.assigned_inspector_user_id

        by_name = {}
        for user in User.objects.filter(is_active=True).only(
                'id', 'email', 'first_name', 'last_name'):
            by_name.setdefault(inspector_name(user), []).append(user)

        losses = []
        self.stdout.write('')
        self.stdout.write('Parity — of the projects the name match gave each '
                          'user, how many the backfilled key also gives them:')
        header = (f'  {"user":<36} {"by name":>8} {"kept":>6} '
                  f'{"lost":>5}  shared name with')
        self.stdout.write(header)
        self.stdout.write('  ' + '-' * (len(header) - 2))
        for name, holders in sorted(by_name.items()):
            name_ids = {p.id for p in projects
                        if (p.assigned_inspector or '').strip() == name}
            if not name_ids:
                continue
            for user in holders:
                fk_ids = {pid for pid, uid in simulated.items()
                          if uid == user.id}
                kept = name_ids & fk_ids
                lost = name_ids - fk_ids
                others = [h.email for h in holders if h.id != user.id]
                if lost:
                    losses.append((user, lost))
                self.stdout.write(
                    f'  {user.email:<36} {len(name_ids):>8} {len(kept):>6} '
                    f'{len(lost):>5}  {", ".join(others) if others else "—"}')

        orphaned = [p for p in projects
                    if (p.assigned_inspector or '').strip()
                    and simulated.get(p.id) is None]

        self.stdout.write('')
        self.stdout.write('-' * 68)
        self.stdout.write(f'Users losing projects: {len(losses)}')
        self.stdout.write(f'Projects left with no assignment: {len(orphaned)}')

        if losses:
            self.stdout.write('')
            self.stdout.write('Access that the foreign key does not reproduce:')
            for user, lost in losses:
                for project in projects:
                    if project.id in lost:
                        self.stdout.write(
                            f'  {user.email} would lose '
                            f'{project.reference_number} '
                            f'({project.assigned_inspector!r})')
            for project in orphaned:
                self.stdout.write(
                    f'  {project.reference_number} '
                    f'({project.assigned_inspector!r}) resolves to nobody and '
                    f'would read as unassigned')

        self.stdout.write('')
        if not losses and not orphaned:
            self.stdout.write(
                'CLEAR: every project reachable through the name match is '
                'reachable through the foreign key. The switch takes no access '
                'away.')
        else:
            self.stdout.write(
                'REVIEW: the lines above are access the name match granted and '
                'the foreign key will not. Resolve them — correct the text, or '
                'assign the project to the right person — before switching '
                '`scoped_projects` to the foreign key.')

        # --- write ------------------------------------------------------
        if not execute:
            self.stdout.write('')
            self.stdout.write('Nothing was written. Re-run with --execute to '
                              'apply.')
            return

        if (losses or orphaned) and not options['force']:
            self.stderr.write('')
            self.stderr.write(
                'Refusing to write: the parity report above shows access the '
                'foreign key does not reproduce. Review it, then either fix the '
                'data or re-run with --execute --force.')
            return

        with transaction.atomic():
            for project, user in resolved:
                # Through the model, not `queryset.update()`: `save()` also
                # rewrites the display name from the user it resolved, which is
                # what stops the mirror and the key from disagreeing the moment
                # the backfill finishes.
                project.assigned_inspector_user = user
                project.save(update_fields=['assigned_inspector_user'])

        self.stdout.write('')
        self.stdout.write(f'Assigned {len(resolved)} project(s).')
