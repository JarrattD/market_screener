"""Offline, fail-closed regression coverage for main-first production ordering."""
import copy
from datetime import date
import importlib.util
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from email.utils import formatdate
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('screener_ordering_gate', ROOT / 'scripts/wait_for_main_screen.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

REPOSITORY = 'JarrattD/market_screener'
SHA = 'a' * 40
MAIN_PATH = '.github/workflows/screener.yml'
CHILD_PATH = '.github/workflows/book-margin-screen.yml'


def run(ident=101, *, child=False, **overrides):
    repository = {'id': 123, 'full_name': REPOSITORY, 'name': 'market_screener',
                  'owner': {'login': 'JarrattD'}}
    result = {
        'id': ident, 'run_attempt': 1, 'workflow_id': 45 if child else 44,
        'path': CHILD_PATH if child else MAIN_PATH, 'event': 'schedule',
        'head_branch': 'main', 'head_sha': SHA,
        'created_at': '2026-10-24T02:00:00Z',
        'updated_at': '2026-10-24T04:00:00Z',
        'status': 'in_progress' if child else 'completed',
        'conclusion': None if child else 'success',
        'repository': copy.deepcopy(repository), 'head_repository': copy.deepcopy(repository),
        'html_url': f'https://github.com/{REPOSITORY}/actions/runs/{ident}',
    }
    result.update(overrides)
    return result


def payload(*runs):
    return {'total_count': len(runs), 'workflow_runs': list(runs)}


def child_env(child):
    return {
        'GITHUB_REPOSITORY': REPOSITORY,
        'GITHUB_REF': 'refs/heads/main',
        'GITHUB_SHA': child['head_sha'],
        'GITHUB_RUN_ID': str(child['id']),
        'GITHUB_RUN_ATTEMPT': str(child['run_attempt']),
        'GITHUB_EVENT_NAME': 'schedule',
        'GITHUB_WORKFLOW_REF': f'{REPOSITORY}/{CHILD_PATH}@refs/heads/main',
    }


class FakeClock:
    def __init__(self): self.now, self.sleeps = 0.0, []
    def __call__(self): return self.now
    def sleep(self, seconds):
        if seconds <= 0: raise AssertionError('Gate must wait rather than busy poll')
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAPI:
    def __init__(self, snapshots, jobs=None):
        self.snapshots, self.index, self.calls = list(snapshots), 0, []
        self.jobs = jobs
        self.current = None
    def __call__(self, path):
        self.calls.append(path)
        if '/workflows/' in path and '/runs' in path:
            value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
            self.index += 1
            if isinstance(value, Exception): raise value
            self.current = value
            return copy.deepcopy(value)
        if '/jobs' in path:
            if self.jobs is not None: return copy.deepcopy(self.jobs)
            parent = self.current['workflow_runs'][0]
            return {'total_count': 2, 'jobs': [
                {'id': 201, 'name': 'test-completion', 'status': 'completed', 'conclusion': 'success',
                 'run_id': parent['id'], 'run_attempt': parent['run_attempt'], 'head_sha': parent['head_sha']},
                {'id': 202, 'name': 'run-screener', 'status': 'completed', 'conclusion': 'success',
                 'run_id': parent['id'], 'run_attempt': parent['run_attempt'], 'head_sha': parent['head_sha']},
            ]}
        if '/actions/runs/' in path:
            if not self.current or not self.current['workflow_runs']:
                raise AssertionError('Cannot inspect an unselected main run')
            return copy.deepcopy(self.current['workflow_runs'][0])
        raise AssertionError(f'Unexpected API path: {path}')


class SelectorTests(unittest.TestCase):
    def test_workflow_paths_normalize_only_main_refs(self):
        for value in (MAIN_PATH, MAIN_PATH + '@main', MAIN_PATH + '@refs/heads/main'):
            self.assertEqual(gate.normalized_path(value), MAIN_PATH)
        for value in (None, MAIN_PATH + '@feature', MAIN_PATH + '@refs/tags/main'):
            with self.subTest(value=value), self.assertRaises(gate.GateError):
                gate.normalized_path(value)

    def test_original_created_date_is_london_date_not_update_or_wall_clock(self):
        item = run(created_at='2026-06-05T23:30:00Z', updated_at='2026-06-09T10:00:00Z')
        self.assertEqual(gate.run_batch_date(item), date(2026, 6, 6))
        # British summer time has ended: UTC23:30 is still the same London day.
        self.assertEqual(gate.run_batch_date(run(created_at='2026-11-06T23:30:00Z')), date(2026, 11, 6))

    def test_dst_transition_dates_remain_unambiguous(self):
        cases = [('2026-03-29T00:30:00Z', date(2026, 3, 29)),
                 ('2026-03-29T01:30:00Z', date(2026, 3, 29)),
                 ('2026-10-25T00:30:00Z', date(2026, 10, 25)),
                 ('2026-10-25T01:30:00Z', date(2026, 10, 25))]
        for timestamp, expected in cases:
            self.assertEqual(gate.run_batch_date(run(created_at=timestamp)), expected)

    def test_query_uses_london_midnight_boundaries_across_dst(self):
        for timestamp, expected in (
            ('2026-03-29T12:00:00Z', '2026-03-29T00:00:00Z..2026-03-29T22:59:59Z'),
            ('2026-10-25T12:00:00Z', '2026-10-24T23:00:00Z..2026-10-25T23:59:59Z'),
        ):
            path = gate._list_path(run(102, child=True, created_at=timestamp))
            query = parse_qs(urlsplit(path).query)
            self.assertEqual(query['created'], [expected])
            self.assertEqual(query['event'], ['schedule'])
            self.assertEqual(query['branch'], ['main'])
            self.assertEqual(query['per_page'], ['100'])

    def test_missing_malformed_or_naive_created_timestamp_fails_closed(self):
        for timestamp in (None, '', 'not-a-date', '2026-10-24T02:00:00'):
            with self.subTest(timestamp=timestamp), self.assertRaises(gate.GateError):
                gate.run_batch_date(run(created_at=timestamp))

    def test_selects_exact_same_batch_scheduled_main(self):
        child = run(102, child=True)
        main = run()
        self.assertEqual(gate.select_main_run(payload(main), child), main)
        self.assertIsNone(gate.select_main_run(payload(), child))

    def test_other_dates_and_non_schedule_events_cannot_unlock_gate(self):
        child = run(102, child=True)
        irrelevant = [run(91, created_at='2026-10-17T02:00:00Z'),
                      run(92, created_at='2026-10-31T03:00:00Z'),
                      run(93, event='pull_request'), run(94, event='issues'),
                      run(95, event='workflow_dispatch')]
        self.assertIsNone(gate.select_main_run(payload(*irrelevant), child))
        self.assertEqual(gate.select_main_run(payload(*irrelevant, run()), child)['id'], 101)

    def test_same_batch_wrong_commit_branch_repo_or_fork_fails_closed(self):
        child = run(102, child=True)
        for override in ({'head_sha': 'b' * 40}, {'head_branch': 'feature'},
                         {'repository': {'full_name': 'Other/market_screener'}},
                         {'head_repository': {'full_name': 'Other/market_screener'}}):
            with self.subTest(override=override), self.assertRaises(gate.GateError):
                gate.select_main_run(payload(run(**override)), child)

    def test_two_distinct_same_batch_runs_are_ambiguous_even_if_one_failed(self):
        child = run(102, child=True)
        with self.assertRaises(gate.GateError):
            gate.select_main_run(payload(run(), run(103, conclusion='failure')), child)

    def test_duplicate_payload_rows_fail_closed(self):
        child = run(102, child=True)
        with self.assertRaises(gate.GateError):
            gate.select_main_run(payload(run(), run()), child)

    def test_unrecognized_plain_workflow_paths_cannot_unlock_gate(self):
        child = run(102, child=True)
        for path in ('', '../screener.yml', 'https://example.invalid/screener.yml', CHILD_PATH):
            with self.subTest(path=path):
                self.assertIsNone(gate.select_main_run(payload(run(path=path)), child))

    def test_invalid_list_or_matching_run_metadata_fails_closed(self):
        child = run(102, child=True)
        for value in ({}, {'workflow_runs': None}, {'workflow_runs': ['bad']},
                      payload(run(id=None)), payload(run(run_attempt=0))):
            with self.subTest(value=value), self.assertRaises(gate.GateError):
                gate.select_main_run(value, child)


class ChildIdentityTests(unittest.TestCase):
    def test_matching_scheduled_child_metadata_is_accepted(self):
        child = run(102, child=True)
        gate.validate_child(child, child_env(child))

    def test_child_original_run_identity_rejects_mismatches(self):
        child = run(102, child=True)
        env = child_env(child)
        for key, bad in (('id', 999), ('run_attempt', 2), ('head_sha', 'b' * 40),
                         ('head_branch', 'feature'), ('event', 'pull_request'),
                         ('path', MAIN_PATH), ('repository', {'full_name': 'Other/repo'}),
                         ('head_repository', {'full_name': 'Other/repo'})):
            altered = copy.deepcopy(child)
            altered[key] = bad
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                gate.validate_child(altered, env)

    def test_child_environment_rejects_wrong_repository_branch_event_or_identity(self):
        child = run(102, child=True)
        for key, bad in (('GITHUB_REPOSITORY', 'Other/repo'), ('GITHUB_REF', 'refs/heads/feature'),
                         ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_RUN_ID', '999'),
                         ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_SHA', 'b' * 40)):
            env = child_env(child)
            env[key] = bad
            with self.subTest(key=key), self.assertRaises(gate.GateError):
                gate.validate_child(child, env)


class WaitGateTests(unittest.TestCase):
    def wait(self, api, *, timeout=1000, poll_seconds=240):
        clock = FakeClock()
        result = gate.wait_for_main(run(102, child=True), api, clock=clock,
                                    sleep=clock.sleep, timeout=timeout, poll_seconds=poll_seconds)
        return result, clock

    def test_waits_for_missing_pending_and_running_main_before_success(self):
        api = FakeAPI([payload(), payload(run(status='queued', conclusion=None)),
                       payload(run(status='in_progress', conclusion=None)), payload(run())])
        result, clock = self.wait(api)
        self.assertEqual(result['id'], 101)
        self.assertEqual(len(clock.sleeps), 3)
        self.assertTrue(any('/attempts/1/jobs' in path for path in api.calls))

    def test_success_checks_actual_production_and_test_jobs(self):
        api = FakeAPI([payload(run())])
        result, clock = self.wait(api)
        self.assertEqual(result['conclusion'], 'success')
        self.assertEqual(clock.sleeps, [])
        # Initial lookup plus final uniqueness/attempt recheck are both required.
        self.assertGreaterEqual(sum('/workflows/' in path and '/runs' in path for path in api.calls), 2)

    def test_unsuccessful_main_terminal_conclusions_fail_without_polling(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required'):
            api = FakeAPI([payload(run(conclusion=conclusion))])
            clock = FakeClock()
            with self.subTest(conclusion=conclusion), self.assertRaises(gate.GateError):
                gate.wait_for_main(run(102, child=True), api, clock=clock, sleep=clock.sleep)
            self.assertEqual(clock.sleeps, [])

    def test_missing_or_pending_main_has_bounded_timeout(self):
        for snapshot in (payload(), payload(run(status='queued', conclusion=None))):
            api, clock = FakeAPI([snapshot]), FakeClock()
            with self.assertRaises(gate.GateError):
                gate.wait_for_main(run(102, child=True), api, clock=clock,
                                   sleep=clock.sleep, timeout=250, poll_seconds=240)
            self.assertGreaterEqual(clock.now, 250)
            self.assertLessEqual(clock.now, 250)

    def test_aggregate_success_cannot_hide_missing_skipped_or_failed_production_job(self):
        for jobs in ([], [{'name': 'run-screener', 'status': 'completed', 'conclusion': 'skipped'}],
                     [{'name': 'run-screener', 'status': 'completed', 'conclusion': 'failure'},
                      {'name': 'test-completion', 'status': 'completed', 'conclusion': 'success'}],
                     [{'name': 'run-screener', 'status': 'completed', 'conclusion': 'success'}]):
            api = FakeAPI([payload(run())], jobs={'total_count': len(jobs), 'jobs': jobs})
            with self.subTest(jobs=jobs), self.assertRaises(gate.GateError):
                self.wait(api)

    def test_successful_jobs_with_wrong_run_attempt_or_commit_cannot_unlock(self):
        for key, bad in (('run_id', 999), ('run_attempt', 2), ('head_sha', 'b' * 40)):
            jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success',
                     'run_id': 101, 'run_attempt': 1, 'head_sha': SHA}
                    for name in ('test-completion', 'run-screener')]
            jobs[1][key] = bad
            api = FakeAPI([payload(run())], jobs={'total_count': 2, 'jobs': jobs})
            with self.subTest(key=key), self.assertRaises(gate.GateError): self.wait(api)

    def test_rerun_uses_current_attempt_jobs_not_previous_success(self):
        api = FakeAPI([payload(run(run_attempt=2))])
        result, _ = self.wait(api)
        self.assertEqual(result['run_attempt'], 2)
        self.assertTrue(any('/attempts/2/jobs' in path for path in api.calls))
        self.assertFalse(any('/attempts/1/jobs' in path for path in api.calls))

    def test_final_recheck_rejects_new_ambiguous_run(self):
        api = FakeAPI([payload(run()), payload(run(), run(103))])
        with self.assertRaises(gate.GateError): self.wait(api)

    def test_final_recheck_rejects_parent_attempt_changed(self):
        api = FakeAPI([payload(run()), payload(run(run_attempt=2, status='queued', conclusion=None))])
        with self.assertRaises(gate.GateError): self.wait(api)

    def test_pinned_main_run_id_cannot_change_mid_wait(self):
        api = FakeAPI([payload(run(status='in_progress', conclusion=None)), payload(run(103))])
        with self.assertRaises(gate.GateError): self.wait(api)

    def test_child_batch_does_not_roll_to_next_day_while_waiting(self):
        child = run(102, child=True, created_at='2026-06-05T23:30:00Z')
        main = run(created_at='2026-06-06T02:00:00Z')
        api, clock = FakeAPI([payload(main)]), FakeClock()
        result = gate.wait_for_main(child, api, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['id'], 101)


    def test_observed_running_attempt_cannot_regress_to_older_success(self):
        api = FakeAPI([payload(run(run_attempt=2, status='in_progress', conclusion=None)),
                       payload(run(run_attempt=1))])
        with self.assertRaises(gate.GateError): self.wait(api)
        self.assertFalse(any('/attempts/1/jobs' in path for path in api.calls))

    def test_final_recheck_cannot_regress_to_older_successful_attempt(self):
        api = FakeAPI([payload(run(run_attempt=2)), payload(run(run_attempt=1))])
        with self.assertRaises(gate.GateError): self.wait(api)
        self.assertTrue(any('/attempts/2/jobs' in path for path in api.calls))
        self.assertFalse(any('/attempts/1/jobs' in path for path in api.calls))

    def test_retryable_api_failure_obeys_retry_after_before_polling(self):
        api = FakeAPI([gate.RetryableError('temporarily_unavailable', retry_after_seconds=123), payload(run())])
        result, clock = self.wait(api)
        self.assertEqual(result['id'], 101)
        self.assertGreaterEqual(clock.sleeps[0], 123)

    def test_retryable_api_failure_times_out_without_releasing_child(self):
        api = FakeAPI([gate.RetryableError('temporarily_unavailable', retry_after_seconds=400)])
        clock = FakeClock()
        with self.assertRaises(gate.GateError):
            gate.wait_for_main(run(102, child=True), api, clock=clock,
                               sleep=clock.sleep, timeout=250, poll_seconds=240)
        self.assertEqual(clock.now, 250)

    def test_permanent_api_failure_is_not_ignored(self):
        api = FakeAPI([gate.GateError('identity_not_verifiable')])
        with self.assertRaises(gate.GateError): self.wait(api)
        self.assertEqual(len(api.calls), 1)

    def test_changed_attempt_waits_for_new_attempt_and_rechecks_its_jobs(self):
        api = FakeAPI([payload(run()),
                       payload(run(run_attempt=2, status='queued', conclusion=None)),
                       payload(run(run_attempt=2))])
        result, clock = self.wait(api)
        self.assertEqual(result['run_attempt'], 2)
        self.assertTrue(clock.sleeps)
        self.assertTrue(any('/attempts/1/jobs' in path for path in api.calls))
        self.assertTrue(any('/attempts/2/jobs' in path for path in api.calls))

    def test_unknown_parent_status_or_incomplete_jobs_fails_closed(self):
        for item in (run(status='unknown', conclusion=None), run(status='completed', conclusion=None)):
            with self.subTest(item=item), self.assertRaises(gate.GateError):
                self.wait(FakeAPI([payload(item)]))
        jobs = {'total_count': 3, 'jobs': [
            {'name': 'test-completion', 'status': 'completed', 'conclusion': 'success'},
            {'name': 'run-screener', 'status': 'completed', 'conclusion': 'success'}]}
        with self.assertRaises(gate.GateError): self.wait(FakeAPI([payload(run())], jobs=jobs))


class HTTPResponse:
    def __init__(self, body=b'{}', headers=None):
        self.body, self.headers = body, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit): return self.body[:limit]


class HTTPOpener:
    def __init__(self, responses): self.responses, self.calls = iter(responses), []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception): raise response
        return response


class PublicHTTPTests(unittest.TestCase):
    def api(self, responses, *, timeout=1000):
        clock, opener = FakeClock(), HTTPOpener(responses)
        return gate.PublicGitHub(clock=clock, wall_clock=lambda: 1000 + clock.now,
                                 sleep=clock.sleep, timeout=timeout, opener=opener), clock, opener

    def test_public_requests_are_get_only_without_credentials(self):
        api, clock, opener = self.api([HTTPResponse(json.dumps({'id': 102}).encode())])
        with patch.dict('os.environ', {'GITHUB_TOKEN': 'fake-secret-not-to-send', 'GH_TOKEN': 'fake-secret-not-to-send'}):
            self.assertEqual(api.get_json(gate.ROOT + '/runs/102'), {'id': 102})
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'https://api.github.com' + gate.ROOT + '/runs/102')
        self.assertIsNone(request.data)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn('authorization', headers)
        self.assertEqual(headers.get('cache-control'), 'no-cache')
        self.assertNotIn('cookie', headers)
        self.assertNotIn('fake-secret-not-to-send', repr(headers))
        self.assertLessEqual(timeout, 30)

    def test_unexpected_endpoint_or_redirect_is_never_followed(self):
        api, _, opener = self.api([])
        for path in ('https://example.invalid/', '/repos/Other/repo/actions/runs',
                     gate.ROOT + '/../secrets'):
            with self.subTest(path=path), self.assertRaises(gate.GateError):
                api.get_json(path)
        self.assertEqual(opener.calls, [])
        self.assertIsNone(gate._NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://example.invalid'))

    def test_429_and_503_are_retryable_and_honor_retry_after(self):
        for status in (429, 503):
            error = gate.HTTPError('https://api.github.com/redacted', status, 'secret body', {'Retry-After': '600'}, None)
            api, _, _ = self.api([error])
            with self.subTest(status=status), self.assertRaises(gate.RetryableError) as caught:
                api.get_json(gate.ROOT + '/runs/102')
            self.assertGreaterEqual(caught.exception.retry_after_seconds, 600)
            self.assertNotIn('secret', str(caught.exception))

    def test_403_with_exhausted_public_quota_is_retryable_but_other403_is_not(self):
        headers = {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1600'}
        quota = gate.HTTPError('https://api.github.com/redacted', 403, 'secret body', headers, None)
        api, _, _ = self.api([quota])
        with self.assertRaises(gate.RetryableError) as caught:
            api.get_json(gate.ROOT + '/runs/102')
        self.assertGreaterEqual(caught.exception.retry_after_seconds, 605)
        denied = gate.HTTPError('https://api.github.com/redacted', 403, 'secret body', {}, None)
        api, _, _ = self.api([denied])
        with self.assertRaises(gate.GateError) as caught:
            api.get_json(gate.ROOT + '/runs/102')
        self.assertNotIsInstance(caught.exception, gate.RetryableError)
        self.assertNotIn('secret', str(caught.exception))

    def test_success_headers_preserve_public_quota_headroom_for_next_request(self):
        api, clock, opener = self.api([
            HTTPResponse(headers={'X-RateLimit-Remaining': '5', 'X-RateLimit-Reset': '1300'}), HTTPResponse()])
        api.get_json(gate.ROOT + '/runs/102')
        api.get_json(gate.ROOT + '/runs/102')
        self.assertGreaterEqual(clock.now, 305)
        self.assertEqual(len(opener.calls), 2)

    def test_retry_after_http_date_is_not_shortened(self):
        api, clock, _ = self.api([
            HTTPResponse(headers={'Retry-After': formatdate(1600, usegmt=True)}), HTTPResponse()])
        api.get_json(gate.ROOT + '/runs/102')
        api.get_json(gate.ROOT + '/runs/102')
        self.assertGreaterEqual(clock.now, 600)

    def test_server_cooldown_beyond_deadline_fails_without_extra_request(self):
        api, _, opener = self.api([HTTPResponse(headers={'Retry-After': '600'})], timeout=250)
        api.get_json(gate.ROOT + '/runs/102')
        with self.assertRaises(gate.GateError): api.get_json(gate.ROOT + '/runs/102')
        self.assertEqual(len(opener.calls), 1)

    def test_invalid_oversize_or_non_object_metadata_fails_closed(self):
        for body in (b'not json secret', b'[]', b'null', b'\xff', b'x' * 2_000_001):
            api, _, _ = self.api([HTTPResponse(body)])
            with self.subTest(length=len(body)), self.assertRaises(gate.GateError) as caught:
                api.get_json(gate.ROOT + '/runs/102')
            self.assertNotIn('secret', str(caught.exception))

    def test_network_failure_is_retryable_and_sanitized(self):
        errors = [gate.URLError('secret provider details'),
                  ConnectionResetError('secret socket details'),
                  OSError('secret transport details'),
                  gate.IncompleteRead(b'secret partial body', 100)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                api, _, _ = self.api([error])
                with self.assertRaises(gate.RetryableError) as caught:
                    api.get_json(gate.ROOT + '/runs/102')
                self.assertNotIn('secret', str(caught.exception))

    def test_two_ordinary_pollers_leave_anonymous_hourly_headroom(self):
        self.assertGreaterEqual(gate.POLL_SECONDS, 240)
        polls_per_hour = 2 * 3600 / gate.POLL_SECONDS
        self.assertLessEqual(polls_per_hour, 30)
        # Allow setup/final checks as well as the steady-state two pollers.
        self.assertLess(polls_per_hour + 12, 60)


class GateEntrypointTests(unittest.TestCase):
    def test_explicit_main_branch_manual_dispatch_is_standalone_without_http(self):
        child = run(102, child=True)
        env = child_env(child)
        env['GITHUB_EVENT_NAME'] = 'workflow_dispatch'
        with patch.object(gate, 'PublicGitHub') as constructor, redirect_stdout(io.StringIO()):
            self.assertEqual(gate.main(env), 0)
        constructor.assert_not_called()

    def test_manual_dispatch_on_wrong_branch_and_pr_context_fail_before_http(self):
        for key, value in (('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_EVENT_NAME', 'pull_request')):
            env = child_env(run(102, child=True))
            env[key] = value
            with patch.object(gate, 'PublicGitHub') as constructor, redirect_stderr(io.StringIO()):
                self.assertEqual(gate.main(env), 1)
            constructor.assert_not_called()


class WorkflowOrderingTests(unittest.TestCase):
    def jobs(self, source):
        tail = source.split('\njobs:\n', 1)[1]
        matches = list(re.finditer(r'^  ([a-zA-Z0-9_-]+):\s*$', tail, re.M))
        return {match.group(1): tail[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(tail)]
                for index, match in enumerate(matches)}

    def test_independent_screens_gate_before_taking_the_production_lock(self):
        for filename in ('book-margin-screen.yml', 'negative-ev-fcf-screen.yml'):
            with self.subTest(filename=filename):
                source = (ROOT / '.github/workflows' / filename).read_text()
                jobs = self.jobs(source)
                gates = [(key, block) for key, block in jobs.items() if 'python scripts/wait_for_main_screen.py' in block]
                self.assertEqual(len(gates), 1)
                name, block = gates[0]
                self.assertRegex(block, r'needs:\s*(?:test|\[\s*test\s*\])')
                self.assertNotIn('concurrency:', block)
                self.assertNotIn('secrets.', block)
                self.assertRegex(jobs['screen'], r'needs:[^\n]*\b' + re.escape(name) + r'\b')
                self.assertIn('group: production-screener', jobs['screen'])
                self.assertIn('queue: max', jobs['screen'])
                self.assertIn('cancel-in-progress: false', jobs['screen'])
                self.assertIn('test_screener_ordering.py', jobs['test'])
                self.assertIn("'scripts/wait_for_main_screen.py'", source)
                self.assertIn("'tests/test_screener_ordering.py'", source)

    def test_schedules_and_grants_remain_unchanged(self):
        for filename in ('screener.yml', 'book-margin-screen.yml', 'negative-ev-fcf-screen.yml'):
            source = (ROOT / '.github/workflows' / filename).read_text()
            self.assertRegex(source, r"cron: ['\"]0 3 \* \* 6['\"]")
            self.assertRegex(source, r'timezone: [\"]?Europe/London')
            self.assertNotIn('workflow_run:', source)
            self.assertNotIn('actions: write', source)
            self.assertNotIn('actions: read', source)
            self.assertNotIn('GITHUB_TOKEN', source)
            self.assertNotIn('GH_TOKEN', source)
        main = (ROOT / '.github/workflows/screener.yml').read_text()
        self.assertIn('issues:', main)
        self.assertIn('pull_request:', main)
        self.assertIn('research-completion-${{ github.run_id }}-${{ github.run_attempt }}', main)


if __name__ == '__main__':
    unittest.main()
