"""
Run the field gateway: watch a folder of instrument exports and push each one
to the platform.

The instrument exports as it always does, into a folder that is synced
(OneDrive, Dropbox, a network share). This command notices a new export once
it has stopped changing, sends it to ``telemetry/session/from-file/`` with the
site's device credential, and the capture appears on the platform as a
reviewable, un-promoted session. No one opens an upload form, and no radio is
involved.

It runs on the site machine, not on the server, and under
``config.settings.gateway`` — a settings module with no database configured,
so the laptop holds no credential to the registry. See
``apps/telemetry/gateway.py`` for the settle rule, the ledger and the retry
policy; this file is only the command line over it.

Usage:
    python manage.py run_gateway --config gateway.json
    python manage.py run_gateway --config gateway.json --once
    python manage.py run_gateway --config gateway.json --once --dry-run
    python manage.py run_gateway --config gateway.json --retry-rejected

``--once`` sweeps a single time and exits, which is what to run when
diagnosing a site: it reports exactly what a running gateway would do to the
folder as it stands. ``--dry-run`` says what would be sent and sends nothing —
it does not read the ledger's history as permission to skip, but it does not
write to it either, so a dry run never causes a real run to miss a file.

Run it continuously under Windows Task Scheduler ("At startup", restart on
failure); ``run_gateway.bat`` beside the repository root is the wrapper to
point the task at. The ``nxdev_`` credential lives in ``gateway.json``, which
belongs outside version control and readable only by the account the task runs
as.

An absent ``watch_dir`` stops the gateway by default, because for a synced
folder that means the path is wrong. Where the folder genuinely comes and goes
— a USB stick, a share that is not always mounted — set
``"wait_for_watch_dir": true`` and it waits instead. That matters under Task
Scheduler, whose restart budget is finite: a task that stops every minute for
a folder that was never going to be there is a task that quietly dies.
"""
import logging
import os
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.telemetry import gateway

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = ('Watch a folder of instrument exports and push each settled file '
            'to the platform as a reviewable telemetry session.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--config', required=True,
            help='Path to the site\'s gateway.json.')
        parser.add_argument(
            '--once', action='store_true',
            help='Sweep once and exit, instead of watching continuously.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be sent, sending nothing and writing '
                 'nothing to the ledger.')
        parser.add_argument(
            '--retry-rejected', action='store_true',
            help='Forget the platform\'s earlier refusals so those files are '
                 'offered again — use after fixing a device\'s column mapping.')

    def handle(self, *args, **options):
        _configure_logging()
        self._counts = {}
        self._config = None
        self._watch_dir_missing = False

        try:
            config = gateway.GatewayConfig.from_file(options['config'])
        except gateway.GatewayError as exc:
            raise CommandError(str(exc))
        self._config = config

        mode = 'dry run' if options['dry_run'] else 'watching'
        self.stdout.write(
            f'Gateway {mode}: {config.watch_dir}\n'
            f'  pushing to {config.send_url}\n'
            f'  as device {config.device}\n'
            f'  ledger {config.ledger_path}')

        if config.wait_for_watch_dir:
            self.stdout.write(
                '  the folder may not exist yet; the gateway waits for it')

        if not options['once']:
            self.stdout.write(
                f'  every {config.interval_seconds:.0f}s; press Ctrl+C to '
                f'stop.')

        try:
            gateway.run(
                config,
                once=options['once'],
                dry_run=options['dry_run'],
                retry_rejected=options['retry_rejected'],
                on_result=self._report,
                on_sweep=self._after_sweep,
            )
        except gateway.GatewayError as exc:
            # Raised mid-run when the watched folder disappears or the ledger
            # becomes unwritable. Both are conditions a person has to fix, and
            # exiting non-zero is what makes Task Scheduler surface them.
            raise CommandError(str(exc))
        except KeyboardInterrupt:
            # A normal way to stop a watcher, so it is not an error. Reported
            # on stdout rather than stderr to keep a clean exit clean.
            self.stdout.write('\nStopped.')
            return

        summary = self._summary
        if summary:
            self.stdout.write(summary)

    # ------------------------------------------------------------------

    def _report(self, result):
        """Tally one sweep result, and print it unless the log already did.

        A refusal and a failed attempt are logged by the gateway itself, at
        ERROR and WARNING, with the full reason — and those go to stderr,
        which is where something needing action belongs. Printing them here
        too would duplicate a long exception message twice in the log file for
        no gain. Everything else is this command's to report.
        """
        name = os.path.basename(result.path)
        self._counts[result.action] = self._counts.get(result.action, 0) + 1

        if result.action == gateway.SENT:
            self.stdout.write(f'  sent       {name} — {result.detail}')
        elif result.action == gateway.DUPLICATE:
            self.stdout.write(f'  already    {name} — {result.detail}')
        elif result.action == gateway.WOULD_SEND:
            self.stdout.write(f'  would send {name} — {result.detail}')
        elif result.action == gateway.DEFERRED:
            self.stdout.write(f'  waiting    {name} — {result.detail}')
        elif result.action == gateway.SKIPPED:
            self.stdout.write(f'  skipped    {name} — {result.detail}')

    @property
    def _summary(self):
        if self._watch_dir_missing:
            # "Nothing in the folder to send" must not appear here. It reads
            # as the instrument having produced nothing, which is reassuring
            # and is a different claim from the folder not being there at all.
            # What was sent before it went is still worth reporting.
            note = (f'{self._config.watch_dir} is not there at the moment; '
                    f'waiting for it to appear.')
            return f'{self._counted}\n  {note}' if self._counts else note

        if not self._counts:
            return 'Nothing in the folder to send.'
        return self._counted

    @property
    def _counted(self):
        parts = [f'{count} {action}'
                 for action, count in sorted(self._counts.items())]
        return 'Sweep complete: ' + ', '.join(parts) + '.'

    def _after_sweep(self, runner):
        self._watch_dir_missing = runner.watch_dir_missing


def _configure_logging():
    """Send the gateway's own log lines to stderr in a plain format.

    Without this the module logger propagates to Django's default config,
    which the gateway settings deliberately do not carry — so a transient
    network failure would be silent, and the operator would see a file not
    arrive with no reason given.

    These lines are the report for the two outcomes this command does not
    print itself: a refused file (ERROR) and a failed attempt (WARNING). Both
    belong on stderr, and `_report` deliberately leaves them to here.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
