from django.core.management.base import BaseCommand
from apps.projects.models import Project
from apps.stakeholders.services import StakeholderService
from apps.stakeholders.models import ProjectStakeholderTeam, Developer

class Command(BaseCommand):
    help = 'Synchronize all Projects and their assigned professionals into the ProjectStakeholderTeam matrix and Developer directory'

    def handle(self, *args, **options):
        projects = Project.objects.all()
        total = projects.count()
        self.stdout.write(f"Starting synchronization of {total} projects into stakeholder teams and developers matrix...")

        synced = 0
        for p in projects:
            try:
                team = StakeholderService.sync_project_team(p)
                StakeholderService.sync_developer_from_project(p)
                member_count = len(team.team_data) if team and team.team_data else 0
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [SYNCED] {p.name} ({p.reference_number}) -> {member_count} members | Developer: {p.developer_name or p.developer_organization}"
                    )
                )
                synced += 1
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [ERROR] {p.name} ({p.reference_number}): {e}"
                    )
                )

        final_teams = ProjectStakeholderTeam.objects.count()
        final_devs = Developer.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuccessfully synchronized {synced}/{total} projects. Total ProjectStakeholderTeam records: {final_teams}. Total Developer records: {final_devs}."
            )
        )

