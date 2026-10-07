"""
Run the field gateway: watch a folder of instrument exports and push each one
to the platform.

The instrument exports as it always does, into a folder that is synced
(OneDrive, Dropbox, a network share). This command notices a new export once
it has stopped changing, sends it to ``telemetry/session/from-file/`` with the
site's device credential, and the capture appears on the platform as a
reviewable, un-promoted session. No one opens an upload form, and no radio is
involved.

It runs under ``config.settings.gateway`` — a settings module with no database
configured, so the process holds no credential to the registry. See
``apps/telemetry/gateway.py`` for the settle rule, the ledger and the retry
policy; this file is only the command line over it.

**Two modes, and which one you want is a statement you make, not a guess the
command draws from the filesystem.**

    python manage.py run_gateway --config gateway.json
    python manage.py run_gateway --config-dir /app/gateway-config --ledger-dir /state

``--config`` serves one instrument from one hand-written file. ``--config-dir``
serves every instrument the platform has provisioned on this deployment, one
JSON file per instrument, **written by the platform and never by hand** — see
``apps/telemetry/gateway_config.py``. That is the mode the deployment uses:

    python manage.py run_gateway --config-dir /app/gateway-config --ledger-dir /state
    python manage.py run_gateway --config-dir /app/gateway-config --ledger-dir /state --once

Two flags rather than one that sniffs its argument, because ``--config`` is
also the name of the thing an operator most naturally points at a *folder* —
the watch folder. A single flag would take ``--config /srv/inbox``, notice a
directory, enter directory mode, find no configs in it, and idle healthily
forever. That is the one failure this whole file is written against: a gateway
that looks like it is working and sends nothing.

``--once`` sweeps a single time and exits, which is what to run when
diagnosing a site: it reports exactly what a running gateway would do to the
folder as it stands. ``--dry-run`` says what would be sent and sends nothing —
it does not read the ledger's history as permission to skip, but it does not
write to it either, so a dry run never causes a real run to miss a file.

The ``nxdev_`` credential lives in the config file, which belongs outside
version control and readable only by the account the process runs as. In
directory mode nobody types it: the platform mints it and writes the file.

An absent ``watch_dir`` stops that instrument by default, because for a synced
folder that means the path is wrong. Where the folder genuinely comes and goes
— a USB stick, a share that is not always mounted — set
``"wait_for_watch_dir": true`` and it waits instead. That matters under any
supervisor with a finite restart budget: a process that exits every minute for
a folder that was never going to be there is a process that quietly dies.
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
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            '--config',
            help='Path to one instrument\'s gateway.json.')
        mode.add_argument(
            '--config-dir',
            help='Directory of configs, one per instrument, written by the '
                 'platform. Serves every instrument provisioned here.')
        parser.add_argument(
            '--ledger-dir',
            help='Where the per-instrument ledgers are written. Required with '
                 '--config-dir, whose directory is mounted read-only.')
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
        config_path, config_dir, ledger_dir = _check_flags(options)

        self._counts = {}
        self._by_device = {}
        self._dir_missing = {}
        self._config = None
        self._supervisor = None
        self._watch_dir_missing = False

        if config_dir:
            self._run_directory(config_dir, ledger_dir, options)
        else:
            self._run_single(config_path, options)

    # ------------------------------------------------------------------
    # One instrument, one hand-written config
    # ------------------------------------------------------------------

    def _run_single(self, config_path, options):
        try:
            config = gateway.GatewayConfig.from_file(config_path)
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
            # exiting non-zero is what makes a supervisor surface them.
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
    # Every instrument the platform has provisioned here
    # ------------------------------------------------------------------

    def _run_directory(self, config_dir, ledger_dir, options):
        try:
            supervisor = gateway.GatewaySupervisor(
                config_dir,
                ledger_dir,
                dry_run=options['dry_run'],
                retry_rejected=options['retry_rejected'],
                on_result=self._report,
                on_sweep=self._after_sweep_for,
            )
        except gateway.GatewayError as exc:
            raise CommandError(str(exc))
        self._supervisor = supervisor

        mode = 'dry run' if options['dry_run'] else 'watching'
        self.stdout.write(
            f'Gateway {mode} {config_dir}\n'
            f'  one instrument per config file the platform writes here\n'
            f'  ledgers in {ledger_dir}\n'
            f'  sweeping at the quickest provisioned interval; press Ctrl+C '
            f'to stop.')

        try:
            supervisor.run(once=options['once'])
        except gateway.GatewayError as exc:
            raise CommandError(str(exc))
        except KeyboardInterrupt:
            self.stdout.write('\nStopped.')
            return

        self.stdout.write(self._summary)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _report(self, result):
        """Tally one sweep result, and print it unless the log already did.

        A refusal and a failed attempt are logged by the gateway itself, at
        ERROR and WARNING, with the full reason — and those go to stderr,
        which is where something needing action belongs. Printing them here
        too would duplicate a long exception message twice in the log file for
        no gain. Everything else is this command's to report.
        """
        tally = self._counts if not result.device else \
            self._by_device.setdefault(result.device, {})
        tally[result.action] = tally.get(result.action, 0) + 1

        name = os.path.basename(result.path)
        # Directory mode serves several instruments, and a filename alone does
        # not say which: two sites' exports are called the same thing. The
        # first eight characters of the UUID are enough to tell them apart in
        # a column, and the full one is in the log line the gateway wrote.
        who = f'{result.device[:8]}  ' if result.device else ''

        if result.action == gateway.SENT:
            self.stdout.write(f'  {who}sent       {name} — {result.detail}')
        elif result.action == gateway.DUPLICATE:
            self.stdout.write(f'  {who}already    {name} — {result.detail}')
        elif result.action == gateway.WOULD_SEND:
            self.stdout.write(f'  {who}would send {name} — {result.detail}')
        elif result.action == gateway.DEFERRED:
            self.stdout.write(f'  {who}waiting    {name} — {result.detail}')
        elif result.action == gateway.SKIPPED:
            self.stdout.write(f'  {who}skipped    {name} — {result.detail}')

    @property
    def _summary(self):
        if self._supervisor is not None:
            return self._directory_summary
        return self._single_summary

    @property
    def _single_summary(self):
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

    @property
    def _directory_summary(self):
        """One line per instrument, and one per instrument that is not served.

        Read off the supervisor rather than off the results, because an
        instrument whose config would not load produces no results at all — and
        leaving it out of the summary would read as "not provisioned here",
        which is a different and more comfortable claim than "provisioned and
        broken".
        """
        served = self._supervisor.served
        failures = self._supervisor.failures

        if not served and not failures:
            return ('No instruments are provisioned for this gateway yet. The '
                    'platform writes a config file here when gateway sync is '
                    'turned on for an instrument.')

        lines = [f'Serving {len(served)} instrument(s).']
        for entry in sorted(served.values(), key=lambda s: s.device):
            counts = self._by_device.get(entry.device) or {}
            if counts:
                state = ', '.join(f'{count} {action}'
                                  for action, count in sorted(counts.items()))
            elif self._dir_missing.get(entry.device):
                state = 'the folder is not there at the moment'
            else:
                state = 'nothing in the folder to send'
            lines.append(f'  {entry.device}  {state}')

        for path in sorted(failures):
            lines.append(f'  NOT SERVED  {path} — {failures[path]}')
        return '\n'.join(lines)

    # ------------------------------------------------------------------

    def _after_sweep(self, runner):
        self._watch_dir_missing = runner.watch_dir_missing

    def _after_sweep_for(self, device, runner):
        if runner.watch_dir_missing:
            self._dir_missing[device] = True
        else:
            self._dir_missing.pop(device, None)


def _check_flags(options):
    """Validate the mode flags, and return the three of them.

    ``add_arguments`` already makes ``--config`` and ``--config-dir`` mutually
    exclusive and requires one of them, but ``call_command`` goes straight to
    ``handle`` and never touches argparse — so every check is made again here
    rather than trusted to a parser that some callers do not use.
    """
    config_path = options.get('config')
    config_dir = options.get('config_dir')
    ledger_dir = options.get('ledger_dir')

    if config_path and config_dir:
        raise CommandError(
            'Pass --config or --config-dir, not both: --config serves one '
            'hand-written file, --config-dir serves every config the platform '
            'has provisioned here.')
    if not config_path and not config_dir:
        raise CommandError(
            'Pass --config for one instrument, or --config-dir to serve every '
            'instrument the platform has provisioned on this deployment.')

    if config_dir and not ledger_dir:
        # The config directory is mounted read-only, so a ledger cannot live
        # beside its config. Without this, every instrument would resolve its
        # ledger to the same unwritable path and none of them would start —
        # with a message about permissions rather than about the missing flag.
        raise CommandError(
            '--config-dir needs --ledger-dir: the config directory is mounted '
            'read-only, so each instrument\'s ledger has to be written '
            'somewhere else — in this deployment, /state.')
    if ledger_dir and not config_dir:
        raise CommandError(
            '--ledger-dir belongs with --config-dir. A single --config names '
            'its own ledger_path, so --ledger-dir here would be ignored.')

    return config_path, config_dir, ledger_dir


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
