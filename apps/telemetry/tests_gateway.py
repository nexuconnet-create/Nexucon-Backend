"""
Tests for the field gateway.

The gateway has no server of its own, so the transport is injected and these
tests drive ``GatewayRunner.sweep()`` directly. That is where the behaviour
worth testing actually lives — the settle rule, the ledger, and the difference
between a refusal and a failure — and none of it needs a network to exercise.

What each group is guarding:

  * **Settle.** A file must not be sent while something is still writing it.
    Every folder watcher that skipped this rule has uploaded half a CSV.
  * **Ledger.** The same bytes must never be filed as two captures, and the
    ledger must only record what the platform actually confirmed.
  * **Refusal vs failure.** A 4xx is a verdict — retrying it forever buries
    the message that says how to fix it. A 5xx is not a verdict — dropping the
    file would lose a capture. The two must not be confused.
  * **Config.** Every mistake must surface at startup, because a gateway that
    starts and then silently does nothing looks identical to a working one.
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import LiveServerTestCase, SimpleTestCase, override_settings

from apps.digital_eye.models import FieldDevice, PUNDITReading
from apps.projects.models import Project
from apps.telemetry import gateway
from apps.telemetry.models import (
    DEVICE_TOKEN_PREFIX, DeviceToken, TelemetryPacket, TelemetrySession,
)
from apps.telemetry.services import DeviceTokenService

User = get_user_model()

DEVICE_UUID = '11111111-1111-1111-1111-111111111111'
PROJECT_UUID = '22222222-2222-2222-2222-222222222222'

CSV = (
    'STRUCTURAL ELEMENT,TEST TYPE,POINT,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
    'Column C1,Pulse Velocity,A,300,65.2\n'
).encode('utf-8')


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeTransport:
    """Stands in for the HTTP call, and remembers what it was asked to send."""

    def __init__(self, *responses):
        self.calls = []
        self._responses = list(responses)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            return FakeResponse(201, {'session_reference': 'TMS-TEST-0001'})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def sent_filenames(self):
        return [call['filename'] for call in self.calls]


class GatewayTestBase(SimpleTestCase):
    """A watch folder and a ledger in a temporary directory.

    Nothing here touches the ORM or storage, so a plain ``SimpleTestCase`` is
    enough — and it keeps the suite honest about the gateway importing no
    models, which is the property that lets it run under
    ``config.settings.gateway``.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='nexucon_gateway_')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.watch_dir = os.path.join(self.dir, 'watch')
        os.makedirs(self.watch_dir)
        self.ledger_path = os.path.join(self.dir, 'gateway-ledger.json')
        # Several tests deliberately provoke refusals and failures, and the
        # runner logs each one. Silenced so the suite's output stays readable;
        # the rare test that asserts on a log line re-enables it itself.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def config(self, **overrides):
        values = dict(
            api_url='http://localhost:8000',
            device_token='nxdev_' + 'x' * 32,
            device=DEVICE_UUID,
            watch_dir=self.watch_dir,
            ledger_path=self.ledger_path,
            settle_seconds=20.0,
        )
        values.update(overrides)
        return gateway.GatewayConfig(**values)

    def runner(self, transport=None, config=None, dry_run=False):
        return gateway.GatewayRunner(
            config or self.config(),
            transport=transport if transport is not None else FakeTransport(),
            dry_run=dry_run)

    def drop(self, name='export.csv', content=CSV, age=0.0):
        """Put a file in the watch folder, ``age`` seconds old."""
        path = os.path.join(self.watch_dir, name)
        with open(path, 'wb') as handle:
            handle.write(content)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def actions(self, results):
        return {os.path.basename(r.path): r.action for r in results}


# ----------------------------------------------------------------------
# Settling
# ----------------------------------------------------------------------

class GatewaySettleTests(GatewayTestBase):
    """A file is only eligible once it has stopped changing."""

    def test_a_file_already_past_the_settle_window_is_sent_at_once(self):
        # The restart case: the gateway was down, files accumulated, and they
        # are plainly finished. Waiting another window for each would stall a
        # backlog for no benefit.
        transport = FakeTransport()
        self.drop(age=60)

        results = self.runner(transport).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SENT})
        self.assertEqual(transport.sent_filenames, ['export.csv'])

    def test_a_freshly_written_file_is_waited_for(self):
        transport = FakeTransport()
        self.drop(age=0)

        results = self.runner(transport).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.DEFERRED})
        self.assertEqual(transport.calls, [])

    def test_a_file_that_has_stopped_changing_is_sent_on_a_later_sweep(self):
        transport = FakeTransport()
        self.drop(age=0)
        runner = self.runner(transport)

        first = runner.sweep(now=1000.0)
        self.assertEqual(self.actions(first), {'export.csv': gateway.DEFERRED})

        # Same size, same mtime, a full window later.
        second = runner.sweep(now=1025.0)
        self.assertEqual(self.actions(second), {'export.csv': gateway.SENT})
        self.assertEqual(len(transport.calls), 1)

    def test_a_file_that_grows_after_being_seen_restarts_the_clock(self):
        """The half-written export, which is the failure this rule exists for."""
        transport = FakeTransport()
        path = self.drop(age=0)
        runner = self.runner(transport)

        runner.sweep(now=1000.0)
        with open(path, 'ab') as handle:
            handle.write(b'Column C1,Pulse Velocity,B,300,68.1\n')

        results = runner.sweep(now=1025.0)

        self.assertEqual(self.actions(results), {'export.csv': gateway.DEFERRED})
        self.assertEqual(transport.calls, [],
                         'a file still being written must never be sent')

    def test_an_empty_file_is_skipped_rather_than_sent(self):
        # A zero-byte file is a sync placeholder or a failed export. Sending
        # it produces an "empty file" rejection that hides the real problem.
        transport = FakeTransport()
        self.drop(content=b'', age=600)

        results = self.runner(transport).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SKIPPED})
        self.assertEqual(transport.calls, [])

    def test_a_partial_download_is_never_a_candidate(self):
        transport = FakeTransport()
        self.drop(name='export.csv.crdownload', age=600)
        self.drop(name='export.csv.part', age=600)

        results = self.runner(transport).sweep()

        self.assertEqual(results, [])
        self.assertEqual(transport.calls, [])

    def test_a_file_the_patterns_do_not_cover_is_ignored(self):
        transport = FakeTransport()
        self.drop(name='site-notes.txt', age=600)

        self.assertEqual(self.runner(transport).sweep(), [])

    def test_the_ledger_inside_the_watch_folder_is_never_uploaded(self):
        transport = FakeTransport()
        shared = os.path.join(self.watch_dir, 'gateway-ledger.json')
        with open(shared, 'w', encoding='utf-8') as handle:
            json.dump({'sent': {}}, handle)
        os.utime(shared, (time.time() - 600, time.time() - 600))

        results = self.runner(transport, config=self.config(ledger_path=shared)).sweep()

        self.assertEqual(results, [])
        self.assertEqual(transport.calls, [])

    def test_a_missing_watch_folder_is_refused_by_name(self):
        runner = self.runner(config=self.config(
            watch_dir=os.path.join(self.dir, 'not-here')))

        with self.assertRaises(gateway.GatewayError) as ctx:
            runner.sweep()
        self.assertIn('does not exist', str(ctx.exception))


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------

class GatewayLedgerTests(GatewayTestBase):
    """At most once, keyed by content, recorded only on a confirmation."""

    def test_the_ledger_is_written_only_after_the_platform_accepts(self):
        transport = FakeTransport(FakeResponse(500, {'detail': 'boom'}))
        self.drop(age=600)
        runner = self.runner(transport)

        runner.sweep()

        self.assertFalse(os.path.exists(self.ledger_path),
                         'a 5xx is not an acceptance and must record nothing')

    def test_a_server_error_leaves_the_file_queued(self):
        transport = FakeTransport(FakeResponse(503, {'detail': 'unavailable'}),
                                  FakeResponse(201, {'session_reference': 'TMS-2'}))
        self.drop(age=600)
        runner = self.runner(transport)

        first = runner.sweep()
        self.assertEqual(self.actions(first), {'export.csv': gateway.FAILED})

        # Swept again with no change to the file, it is offered once more.
        second = runner.sweep()
        self.assertEqual(self.actions(second), {'export.csv': gateway.SENT})
        self.assertEqual(len(transport.calls), 2)

    def test_an_unreachable_platform_leaves_the_file_queued(self):
        transport = FakeTransport(OSError('network is unreachable'),
                                  FakeResponse(201, {'session_reference': 'TMS-3'}))
        self.drop(age=600)
        runner = self.runner(transport)

        first = runner.sweep()
        self.assertEqual(self.actions(first), {'export.csv': gateway.FAILED})
        self.assertIn('could not reach the platform', first[0].detail)

        second = runner.sweep()
        self.assertEqual(self.actions(second), {'export.csv': gateway.SENT})

    def test_a_successful_send_is_recorded_and_never_repeated(self):
        transport = FakeTransport()
        self.drop(age=600)
        runner = self.runner(transport)

        runner.sweep()
        again = runner.sweep()

        self.assertEqual(self.actions(again), {'export.csv': gateway.SKIPPED})
        self.assertEqual(len(transport.calls), 1)
        self.assertIn('already sent', again[0].detail)

    def test_a_200_duplicate_is_recorded_so_it_is_not_offered_again(self):
        """The lost-response case: the platform already had the bytes.

        Answering 200 with the existing session is the platform saying "the
        file is in". Recording it is what stops a gateway retrying forever.
        """
        transport = FakeTransport(FakeResponse(200, {
            'session_reference': 'TMS-FIRST',
            'import_stats': {'duplicate': True},
        }))
        self.drop(age=600)
        runner = self.runner(transport)

        first = runner.sweep()
        self.assertEqual(self.actions(first), {'export.csv': gateway.DUPLICATE})
        self.assertIn('TMS-FIRST', first[0].detail)

        self.assertEqual(self.actions(runner.sweep()),
                         {'export.csv': gateway.SKIPPED})
        self.assertEqual(len(transport.calls), 1)

    def test_the_ledger_survives_a_restart(self):
        transport = FakeTransport()
        self.drop(age=600)
        self.runner(transport).sweep()

        # A new process, reading the same ledger file.
        fresh = FakeTransport()
        results = self.runner(fresh).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SKIPPED})
        self.assertEqual(fresh.calls, [])

    def test_the_ledger_is_keyed_by_content_not_by_name(self):
        """A sync client re-downloading a file rewrites its name and mtime."""
        transport = FakeTransport()
        self.drop(name='export.csv', age=600)
        self.runner(transport).sweep()

        os.remove(os.path.join(self.watch_dir, 'export.csv'))
        fresh = FakeTransport()
        self.drop(name='export (1).csv', age=600)
        results = self.runner(fresh).sweep()

        self.assertEqual(self.actions(results), {'export (1).csv': gateway.SKIPPED})
        self.assertEqual(fresh.calls, [])

    def test_different_bytes_are_sent_even_under_the_same_name(self):
        transport = FakeTransport()
        self.drop(name='export.csv', age=600)
        self.runner(transport).sweep()

        os.remove(os.path.join(self.watch_dir, 'export.csv'))
        changed = CSV.replace(b'65.2', b'70.9')
        self.drop(name='export.csv', content=changed, age=600)
        results = self.runner(transport).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SENT})
        self.assertEqual(len(transport.calls), 2)

    def test_a_refusal_is_recorded_with_its_reason_and_not_retried(self):
        transport = FakeTransport(FakeResponse(400, {
            'detail': 'export.csv has column(s) this platform does not '
                      'recognise: distance_mm.'}))
        self.drop(age=600)
        runner = self.runner(transport)

        first = runner.sweep()
        self.assertEqual(self.actions(first), {'export.csv': gateway.REJECTED})
        self.assertIn('distance_mm', first[0].detail)

        # Still offered to the operator, but never sent again.
        second = runner.sweep()
        self.assertEqual(self.actions(second), {'export.csv': gateway.REJECTED})
        self.assertEqual(len(transport.calls), 1)

    def test_a_refusal_is_reported_with_the_fix_that_would_clear_it(self):
        transport = FakeTransport(FakeResponse(400, {'detail': 'no test type'}))
        self.drop(age=600)
        runner = self.runner(transport)

        runner.sweep()
        results = runner.sweep()

        self.assertIn('--retry-rejected', results[0].detail)

    def test_retrying_rejected_files_offers_the_same_bytes_again(self):
        """The column-mapping workflow: fix the device, then re-offer."""
        transport = FakeTransport(
            FakeResponse(400, {'detail': 'unknown column'}),
            FakeResponse(201, {'session_reference': 'TMS-AFTER-FIX'}))
        self.drop(age=600)
        runner = self.runner(transport)
        runner.sweep()

        runner.ledger.clear_rejected()
        runner.ledger.save()
        results = runner.sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SENT})
        self.assertEqual(len(transport.calls), 2)

    def test_a_field_error_is_flattened_into_the_reason(self):
        transport = FakeTransport(FakeResponse(400, {
            'device': ['This credential may only push as PUNDIT-002.']}))
        self.drop(age=600)

        results = self.runner(transport).sweep()

        self.assertIn('PUNDIT-002', results[0].detail)

    def test_a_corrupt_ledger_refuses_to_run_rather_than_starting_empty(self):
        """Starting empty would silently re-send every file in the folder."""
        with open(self.ledger_path, 'w', encoding='utf-8') as handle:
            handle.write('{"sent": {"a": ')

        with self.assertRaises(gateway.GatewayError) as ctx:
            self.runner()

        self.assertIn('could not be read', str(ctx.exception))

    def test_a_non_json_error_body_still_yields_a_readable_reason(self):
        transport = FakeTransport(FakeResponse(502, ValueError('not json')))
        self.drop(age=600)

        results = self.runner(transport).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.FAILED})
        self.assertIn('502', results[0].detail)

    # -- proving the ledger is writable before relying on it -------------

    def blocked_ledger_path(self):
        """A ledger path whose parent is a file, so no directory can be made.

        Used instead of chmod because the suite runs on Windows, where the
        read-only bit on a directory does not stop a file being created in it.
        A path *through* a file fails the same way everywhere: ``makedirs``
        raises rather than suppressing, because ``exist_ok`` only forgives an
        existing directory.
        """
        blocker = os.path.join(self.dir, 'not-a-directory')
        with open(blocker, 'w', encoding='utf-8') as handle:
            handle.write('this is a file, not a folder\n')
        return os.path.join(blocker, 'gateway-ledger.json')

    def test_an_unwritable_ledger_refuses_to_start(self):
        """Refused at startup, not discovered after the first successful send.

        This is the ordering that matters. `save()` runs only once the
        platform has accepted a file, so without this check the gateway would
        send the first export — the capture really is on the platform — and
        only then find it cannot record that it did. Under `restart: always`,
        or Task Scheduler's restart-on-failure, that is a gateway re-sending
        one file forever with the log's last line about the ledger rather than
        about the send that worked.
        """
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.runner(config=self.config(
                ledger_path=self.blocked_ledger_path()))

        self.assertIn('could not be written', str(ctx.exception))

    def test_an_unwritable_ledger_stops_the_gateway_before_it_sends(self):
        transport = FakeTransport()
        self.drop(age=600)

        with self.assertRaises(gateway.GatewayError):
            self.runner(transport, config=self.config(
                ledger_path=self.blocked_ledger_path()))

        self.assertEqual(transport.calls, [],
                         'nothing may be sent once a send cannot be recorded')

    def test_a_dry_run_refuses_an_unwritable_ledger_too(self):
        """A dry run's whole job is to predict the real one.

        Reporting files it would send, on a machine where the real run would
        refuse to start, is a prediction that is simply wrong.
        """
        with self.assertRaises(gateway.GatewayError):
            self.runner(dry_run=True,
                        config=self.config(ledger_path=self.blocked_ledger_path()))

    def test_the_writability_check_leaves_no_temporary_file_behind(self):
        """A leftover in the watched folder would be treated as an export."""
        self.runner()

        self.assertEqual(
            [name for name in os.listdir(self.dir)
             if name.startswith('.ledger-')], [])
        self.assertFalse(os.path.exists(self.ledger_path),
                         'checking is not the same as writing one')


# ----------------------------------------------------------------------
# Dry run
# ----------------------------------------------------------------------

class GatewayDryRunTests(GatewayTestBase):
    """A dry run answers what would happen, and does none of it."""

    def test_a_dry_run_sends_nothing(self):
        transport = FakeTransport()
        self.drop(age=600)

        results = self.runner(transport, dry_run=True).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.WOULD_SEND})
        self.assertEqual(transport.calls, [])

    def test_a_dry_run_writes_no_ledger(self):
        """Otherwise a dry run would make the real run skip work it never did."""
        transport = FakeTransport()
        self.drop(age=600)

        self.runner(transport, dry_run=True).sweep()

        self.assertFalse(os.path.exists(self.ledger_path))

    def test_a_dry_run_still_reports_what_the_ledger_already_holds(self):
        transport = FakeTransport()
        self.drop(age=600)
        self.runner(transport).sweep()

        results = self.runner(FakeTransport(), dry_run=True).sweep()

        self.assertEqual(self.actions(results), {'export.csv': gateway.SKIPPED})

    def test_a_dry_run_reports_the_digest_it_would_have_sent(self):
        import hashlib
        self.drop(age=600)

        results = self.runner(dry_run=True).sweep()

        self.assertIn(hashlib.sha256(CSV).hexdigest()[:12], results[0].detail)


# ----------------------------------------------------------------------
# The request itself
# ----------------------------------------------------------------------

class GatewayRequestTests(GatewayTestBase):
    """What actually goes on the wire."""

    def test_the_request_carries_the_device_credential_and_the_file(self):
        transport = FakeTransport()
        self.drop(age=600)

        self.runner(transport).sweep()

        call = transport.calls[0]
        self.assertEqual(call['url'],
                         'http://localhost:8000/api/v1/telemetry/session/from-file/')
        self.assertEqual(call['token'], 'nxdev_' + 'x' * 32)
        self.assertEqual(call['filename'], 'export.csv')
        self.assertEqual(call['content'], CSV)
        self.assertEqual(call['fields']['device'], DEVICE_UUID)
        self.assertEqual(call['fields']['data_type'], 'pundit')

    def test_a_trailing_slash_on_the_api_url_does_not_double_up(self):
        transport = FakeTransport()
        self.drop(age=600)
        runner = self.runner(transport, config=self.config(
            api_url='http://localhost:8000/'))

        runner.sweep()

        self.assertEqual(transport.calls[0]['url'],
                         'http://localhost:8000/api/v1/telemetry/session/from-file/')

    def test_the_project_and_context_are_sent_when_configured(self):
        transport = FakeTransport()
        self.drop(age=600)
        runner = self.runner(transport, config=self.config(
            project=PROJECT_UUID,
            context={'test_type': 'Pulse Velocity', 'floor': 'Ground Floor',
                     'transducer_frequency_khz': 54, 'weather_condition': ''}))

        runner.sweep()

        fields = transport.calls[0]['fields']
        self.assertEqual(fields['project'], PROJECT_UUID)
        self.assertEqual(fields['test_type'], 'Pulse Velocity')
        self.assertEqual(fields['floor'], 'Ground Floor')
        self.assertEqual(fields['transducer_frequency_khz'], '54')
        # A blank is silence, not a value, so it is not sent at all.
        self.assertNotIn('weather_condition', fields)


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

class GatewayConfigTests(SimpleTestCase):
    """Every mistake surfaces at startup, naming the key."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='nexucon_gateway_cfg_')
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, payload):
        path = os.path.join(self.dir, 'gateway.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)
        return path

    def base(self, **overrides):
        values = {
            'api_url': 'http://localhost:8000',
            'device_token': 'nxdev_' + 'x' * 32,
            'device': DEVICE_UUID,
            'watch_dir': self.dir,
        }
        values.update(overrides)
        return values

    def load(self, **overrides):
        return gateway.GatewayConfig.from_file(self.write(self.base(**overrides)))

    def test_a_valid_config_loads(self):
        config = self.load()

        self.assertEqual(config.device, DEVICE_UUID)
        self.assertEqual(config.data_type, 'pundit')
        self.assertEqual(config.settle_seconds, 20.0)
        self.assertEqual(config.interval_seconds, 30.0)

    def test_the_ledger_defaults_beside_the_config_not_in_the_watch_folder(self):
        config = self.load()

        self.assertEqual(os.path.dirname(config.ledger_path), self.dir)
        self.assertTrue(config.ledger_path.endswith('gateway-ledger.json'))

    def test_a_token_that_is_not_a_device_credential_is_refused(self):
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.load(device_token='PUNDIT-SERIAL-12345')

        self.assertIn('device credential', str(ctx.exception))

    def test_a_serial_number_where_the_device_uuid_belongs_is_refused(self):
        # The likeliest configuration mistake at a site, and one that would
        # otherwise surface as a 400 that reads like a broken gateway.
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.load(device='PUNDIT-SERIAL-12345')

        self.assertIn('UUID', str(ctx.exception))

    def test_a_missing_key_is_named(self):
        payload = self.base()
        payload.pop('watch_dir')

        with self.assertRaises(gateway.GatewayError) as ctx:
            gateway.GatewayConfig.from_file(self.write(payload))

        self.assertIn('watch_dir', str(ctx.exception))

    def test_an_api_url_carrying_the_endpoint_is_refused(self):
        """It doubles the path, and a 404 reads as a transient failure.

        So it is retried forever rather than reported as something to fix,
        and the log names the symptom instead of the cause.
        """
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.load(api_url='https://api.nexucon.net/api/v1/telemetry/'
                              'session/from-file/')

        message = str(ctx.exception)
        self.assertIn('api_url', message)
        self.assertIn('scheme and host', message)

    def test_the_platform_service_name_is_a_valid_base_address(self):
        """The shape the compose service uses — base only, no endpoint."""
        config = self.load(api_url='http://web:8000')

        self.assertEqual(config.send_url,
                         'http://web:8000/api/v1/telemetry/session/from-file/')

    def test_a_trailing_slash_on_the_base_is_harmless(self):
        config = self.load(api_url='https://api.nexucon.net/')

        self.assertEqual(config.send_url,
                         'https://api.nexucon.net/api/v1/telemetry/'
                         'session/from-file/')

    def test_an_unknown_context_field_is_refused_rather_than_ignored(self):
        """A field that silently never arrives is a lie in the config file."""
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.load(context={'element': 'Column C1'})

        self.assertIn('element', str(ctx.exception))

    def test_a_non_numeric_settle_window_is_refused(self):
        with self.assertRaises(gateway.GatewayError) as ctx:
            self.load(settle_seconds='soon')

        self.assertIn('settle_seconds', str(ctx.exception))

    def test_a_zero_settle_window_is_refused(self):
        with self.assertRaises(gateway.GatewayError):
            self.load(settle_seconds=0)

    def test_malformed_json_names_the_file(self):
        path = os.path.join(self.dir, 'broken.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{"api_url": "http://x",}')

        with self.assertRaises(gateway.GatewayError) as ctx:
            gateway.GatewayConfig.from_file(path)

        self.assertIn('broken.json', str(ctx.exception))

    def test_a_missing_config_file_is_refused(self):
        with self.assertRaises(gateway.GatewayError) as ctx:
            gateway.GatewayConfig.from_file(os.path.join(self.dir, 'nope.json'))

        self.assertIn('No gateway config', str(ctx.exception))

    def test_the_token_prefix_matches_the_one_the_platform_issues(self):
        """The copy is deliberate — the gateway may not import the model.

        This assertion is what makes the copy safe: change the platform's
        prefix and this test fails, rather than a site's gateway rejecting a
        token the platform had just issued.
        """
        self.assertEqual(gateway.DEVICE_TOKEN_PREFIX, DEVICE_TOKEN_PREFIX)

    def test_the_send_path_matches_the_route_the_platform_exposes(self):
        """The literal path is duplicated because the gateway may not import
        the URLconf's view module under its stripped settings. If the route is
        ever renamed, this fails rather than every site's gateway 404ing."""
        from django.urls import reverse

        self.assertEqual(gateway.SEND_PATH,
                         reverse('telemetry-session-from-file'))


# ----------------------------------------------------------------------
# A watched folder that is not there
# ----------------------------------------------------------------------

class GatewayWatchDirTests(GatewayTestBase):
    """What happens when the watched folder is absent.

    A synced folder is always present, so an absent one means the path is
    wrong: the gateway should say so and stop, because a gateway that runs and
    silently sends nothing is indistinguishable from a working one. A USB
    stick is absent most of the time, and stopping is then wrong twice over —
    nothing is missed by waiting, and under Task Scheduler the restart budget
    is finite. Which of the two applies is declared in the config, never
    inferred from the path.
    """

    def missing_dir(self):
        return os.path.join(self.dir, 'not-mounted-yet')

    def waiting_runner(self, **overrides):
        return self.runner(config=self.config(
            watch_dir=self.missing_dir(), wait_for_watch_dir=True, **overrides))

    def test_an_absent_folder_still_stops_the_gateway_by_default(self):
        # The regression guard. Defaulting to "wait" would turn a typo in
        # watch_dir into a gateway that looks healthy forever and sends
        # nothing — the exact failure this config check exists to prevent.
        runner = self.runner(config=self.config(watch_dir=self.missing_dir()))

        with self.assertRaises(gateway.GatewayError) as ctx:
            runner.sweep()

        self.assertIn('does not exist', str(ctx.exception))

    def test_a_declared_transient_folder_is_waited_for_instead(self):
        runner = self.waiting_runner()

        self.assertEqual(runner.sweep(), [])
        self.assertTrue(runner.watch_dir_missing)

    def test_the_absence_is_reported_once_and_not_every_sweep(self):
        # A gateway waiting a week for a stick is checked tens of thousands of
        # times. One warning per check would bury the refusal that actually
        # needs someone to act, which is the one line worth reading.
        runner = self.waiting_runner()
        logging.disable(logging.NOTSET)

        with self.assertLogs('apps.telemetry.gateway', level='WARNING') as logs:
            runner.sweep()
            runner.sweep()
            runner.sweep()

        waiting = [line for line in logs.output if 'is not there' in line]
        self.assertEqual(len(waiting), 1)

    def test_the_folder_appearing_clears_the_wait_and_its_files_are_sent(self):
        watch_dir = self.missing_dir()
        runner = self.waiting_runner()
        runner.sweep()
        self.assertTrue(runner.watch_dir_missing)

        os.makedirs(watch_dir)
        path = os.path.join(watch_dir, 'export.csv')
        with open(path, 'wb') as handle:
            handle.write(CSV)
        stamp = time.time() - 60
        os.utime(path, (stamp, stamp))

        results = runner.sweep()

        self.assertFalse(runner.watch_dir_missing)
        self.assertEqual(self.actions(results), {'export.csv': gateway.SENT})

    def test_the_setting_defaults_to_off(self):
        self.assertFalse(self.config().wait_for_watch_dir)

    def test_a_setting_that_is_not_a_boolean_is_refused(self):
        path = os.path.join(self.dir, 'gateway.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'api_url': 'https://example.test',
                       'device_token': 'nxdev_' + 'x' * 32,
                       'device': DEVICE_UUID,
                       'watch_dir': self.dir,
                       'wait_for_watch_dir': 'yes'}, handle)

        with self.assertRaises(gateway.GatewayError) as ctx:
            gateway.GatewayConfig.from_file(path)

        self.assertIn('wait_for_watch_dir', str(ctx.exception))

    def test_the_command_does_not_call_an_absent_folder_empty(self):
        """The one line an operator reads, and it must not mislead.

        "Nothing in the folder to send" and "the folder is not there" are
        different claims. The first reads as the instrument having produced
        nothing, which is reassuring and false.
        """
        config_path = os.path.join(self.dir, 'gateway.json')
        with open(config_path, 'w', encoding='utf-8') as handle:
            json.dump({'api_url': 'http://127.0.0.1:1',
                       'device_token': 'nxdev_' + 'x' * 32,
                       'device': DEVICE_UUID,
                       'watch_dir': self.missing_dir(),
                       'ledger_path': self.ledger_path,
                       'wait_for_watch_dir': True}, handle)
        self._restore_command_logging()

        out = StringIO()
        call_command('run_gateway', config=config_path, once=True, stdout=out)
        text = out.getvalue()

        self.assertIn('is not there at the moment', text)
        self.assertNotIn('Nothing in the folder to send', text)

    def _restore_command_logging(self):
        """Undo what the command's own logging setup does to a shared logger.

        The command attaches a stderr handler and switches off propagation on
        the module logger. That is right for a process it owns, and wrong to
        leave behind here: every later test in this file would have its log
        lines handled differently.
        """
        logger = logging.getLogger('apps.telemetry.gateway')
        handlers = list(logger.handlers)
        propagate, level = logger.propagate, logger.level

        def restore():
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)

        self.addCleanup(restore)


# ----------------------------------------------------------------------
# The whole leg, over real HTTP
# ----------------------------------------------------------------------

class GatewayEndToEndTests(LiveServerTestCase):
    """Folder -> gateway -> platform, with nothing stubbed.

    Every test above injects the transport, which is right for the sweep logic
    but leaves one question unanswered: does the request the gateway actually
    builds get accepted by the endpoint that actually exists? This class runs
    a real server, issues a real device credential, and lets the gateway's own
    HTTP call do the work — so the multipart shape, the ``Authorization:
    Device`` header, the credential's device scoping and the registry's parse
    all have to be right together, which is the only thing that reproduces the
    site machine's experience.

    It runs on the test database against a throwaway MEDIA_ROOT, so it never
    touches production data. Note the API base is ``live_server_url``: the
    frontend's ``.env.local`` points at ``api.nexucon.net``, and a test that
    followed it would write into the live registry.
    """

    def setUp(self):
        media_root = tempfile.mkdtemp(prefix='nexucon_gateway_e2e_')
        self._storage_override = override_settings(
            STORAGES={
                'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
                'staticfiles': {
                    'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
            },
            MEDIA_ROOT=media_root,
        )
        self._storage_override.enable()
        self.addCleanup(self._storage_override.disable)
        self.addCleanup(shutil.rmtree, media_root, True)

        self.user = User.objects.create_superuser(
            username='gateway_e2e@nexucon.com',
            email='gateway_e2e@nexucon.com', password='Password123!')
        self.project = Project.objects.create(name='Gateway Site',
                                              status='ACTIVE')
        self.device = FieldDevice.objects.create(
            device_id='PUNDIT-GATEWAY-01', device_type='pundit',
            assigned_project=self.project, is_active=True)
        _, self.token = DeviceTokenService.issue(
            device=self.device, label='site laptop', issued_by=self.user)

        self.dir = tempfile.mkdtemp(prefix='nexucon_gateway_e2e_dir_')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.watch_dir = os.path.join(self.dir, 'exports')
        os.makedirs(self.watch_dir)
        self.ledger_path = os.path.join(self.dir, 'ledger.json')

    def config(self, **overrides):
        values = dict(
            api_url=self.live_server_url,
            device_token=self.token,
            device=str(self.device.id),
            watch_dir=self.watch_dir,
            ledger_path=self.ledger_path,
            settle_seconds=1.0,
            context={'test_type': 'Pulse Velocity'},
        )
        values.update(overrides)
        return gateway.GatewayConfig(**values)

    def drop(self, name='export.csv', content=CSV, age=600):
        path = os.path.join(self.watch_dir, name)
        with open(path, 'wb') as handle:
            handle.write(content)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def sweep(self, config=None):
        results = []
        gateway.run(config or self.config(), once=True,
                    on_result=results.append)
        return results

    # -- the path the site actually takes -------------------------------

    def test_a_settled_export_becomes_a_session_with_no_manual_step(self):
        content = (
            'STRUCTURAL ELEMENT,FLOOR,TEST TYPE,POINT,PATH LENGTH L (MM),'
            'TRANSIT TIME T (US)\n'
            'Column C1,Ground Floor,Pulse Velocity,A,300,65.2\n'
            'Column C1,Ground Floor,Pulse Velocity,B,300,68.1\n'
        ).encode('utf-8')
        self.drop(name='site-export.csv', content=content)

        results = self.sweep()

        self.assertEqual([r.action for r in results], [gateway.SENT],
                         f'gateway did not send: {results}')
        session = TelemetrySession.objects.get()
        self.assertEqual(session.transport, TelemetrySession.TRANSPORT_FILE)
        self.assertEqual(session.status, TelemetrySession.STATUS_ENDED)
        self.assertEqual(session.sync_status, TelemetrySession.SYNC_PENDING)
        self.assertEqual(session.packet_count, 2)
        # The bytes the platform retained are the ones on the disk, unmodified.
        self.assertEqual(session.source_file_sha256,
                         hashlib.sha256(content).hexdigest())
        # And the credential pinned the session to its own instrument.
        self.assertEqual(session.device_id, self.device.id)
        # Nothing is in the registry: promotion is still a person's decision.
        self.assertEqual(PUNDITReading.objects.count(), 0)

    def test_the_same_file_swept_twice_yields_one_session(self):
        self.drop()
        self.sweep()

        again = self.sweep()

        self.assertEqual([r.action for r in again], [gateway.SKIPPED])
        self.assertEqual(TelemetrySession.objects.count(), 1)

    def test_a_second_upload_from_a_fresh_gateway_is_a_resend(self):
        """The wiped-ledger case, which the server has to catch on its own.

        A reinstalled laptop, or one whose ledger file was deleted, has no
        memory of having sent anything — so the platform's own digest check is
        what has to refuse the second copy. The fresh ledger here is the point:
        reusing the first one would only re-test the ledger.
        """
        self.drop()
        self.sweep()

        fresh = gateway.GatewayRunner(self.config(
            ledger_path=os.path.join(self.dir, 'fresh-ledger.json')))
        results = fresh.sweep()

        self.assertEqual([r.action for r in results], [gateway.DUPLICATE],
                         f'the platform did not recognise the resend: {results}')
        self.assertEqual(TelemetrySession.objects.count(), 1)
        self.assertEqual(TelemetryPacket.objects.count(), 1)

    def test_an_export_in_the_instrument_s_own_words_is_refused_by_name(self):
        self.drop(name='raw.csv', content=(
            'Location,Distance (mm),Time (us)\n'
            'A,300,65.2\n'
        ).encode('utf-8'))

        results = self.sweep()

        self.assertEqual([r.action for r in results], [gateway.REJECTED],
                         f'expected a refusal: {results}')
        self.assertIn('distance_mm', results[0].detail)
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_recording_the_mapping_lets_the_same_file_through(self):
        """The workflow the whole feature exists for, end to end.

        A real export is refused; an officer records the instrument's column
        names against the device; the operator re-offers the same bytes; the
        capture lands. No one edits the file and nothing is guessed.
        """
        content = (
            'Location,Distance (mm),Time (us)\n'
            'A,300,65.2\n'
            'B,300,68.1\n'
        ).encode('utf-8')
        self.drop(name='raw.csv', content=content)
        self.assertEqual([r.action for r in self.sweep()], [gateway.REJECTED])
        self.assertEqual(TelemetrySession.objects.count(), 0)

        self.device.column_mapping = {
            'Location': 'point',
            'Distance (mm)': 'path_length_l_mm',
            'Time (us)': 'transit_time_t_us',
        }
        self.device.save(update_fields=['column_mapping'])

        results = []
        gateway.run(self.config(), once=True, retry_rejected=True,
                    on_result=results.append)

        self.assertEqual([r.action for r in results], [gateway.SENT],
                         f'the mapped file did not import: {results}')
        session = TelemetrySession.objects.get()
        self.assertEqual(session.packet_count, 2)
        self.assertEqual(session.source_file_sha256,
                         hashlib.sha256(content).hexdigest())
        payloads = [p.payload for p in session.packets.order_by('sequence')]
        self.assertEqual([p['point_label'] for p in payloads], ['A', 'B'])
        self.assertEqual([p['path_length_mm'] for p in payloads], [300, 300])

    def test_a_revoked_credential_stops_the_gateway(self):
        """Revoking the token ends the push, whatever the gateway believes."""
        DeviceTokenService.revoke(DeviceToken.objects.get(), self.user)
        self.drop()

        results = self.sweep()

        self.assertEqual([r.action for r in results], [gateway.REJECTED],
                         f'a revoked credential must not be honoured: {results}')
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_a_credential_cannot_push_as_another_instrument(self):
        """The gateway is configured with the wrong device UUID.

        Refused rather than corrected: silently substituting the right device
        would hide a gateway mislabelling every reading it sends.
        """
        other = FieldDevice.objects.create(
            device_id='PUNDIT-GATEWAY-02', device_type='pundit',
            assigned_project=self.project, is_active=True)
        self.drop()

        results = self.sweep(self.config(device=str(other.id)))

        self.assertEqual([r.action for r in results], [gateway.REJECTED])
        self.assertIn('PUNDIT-GATEWAY-01', results[0].detail)
        self.assertEqual(TelemetrySession.objects.count(), 0)

    def test_an_export_whose_context_the_file_cannot_carry_still_imports(self):
        """A bare measurement export, with the element supplied by the app."""
        self.drop(content=(
            'POINT,PATH LENGTH L (MM),TRANSIT TIME T (US)\n'
            'A,300,65.2\n'
        ).encode('utf-8'))

        results = self.sweep(self.config(context={
            'test_type': 'Pulse Velocity', 'structural_element': 'Column C1'}))

        self.assertEqual([r.action for r in results], [gateway.SENT],
                         f'{results}')
        session = TelemetrySession.objects.get()
        self.assertEqual(session.session_config.get('structural_element'),
                         'Column C1')
