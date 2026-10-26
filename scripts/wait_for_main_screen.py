"""Read-only predecessor gate; never dispatches runs or accesses private data.

Keep the existing schedule/workflow identities for receipt/ranking eligibility.
This runs in a separate job BEFORE taking the production-screener lock.
"""
from __future__ import annotations

from datetime import datetime, time as daytime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

REPOSITORY = 'JarrattD/market_screener'
MAIN_WORKFLOW = '.github/workflows/screener.yml'
CHILD_WORKFLOWS = frozenset({'.github/workflows/revenue-net-cash-screen.yml', '.github/workflows/book-margin-screen.yml',
                            '.github/workflows/negative-ev-fcf-screen.yml'})
LONDON = ZoneInfo('Europe/London')
POLL_SECONDS = 240
TIMEOUT_SECONDS = 350 * 60
ROOT = '/repos/' + REPOSITORY + '/actions'


class GateError(RuntimeError):
    """Safe diagnostic, never includes provider bodies or credentials."""


class RetryableError(GateError):
    def __init__(self, reason, retry_after_seconds=POLL_SECONDS):
        super().__init__(reason)
        self.retry_after_seconds = max(POLL_SECONDS, retry_after_seconds)


def normalized_path(path):
    if not isinstance(path, str):
        raise GateError('Missing workflow path')
    if '@' not in path:
        return path
    path, suffix = path.rsplit('@', 1)
    if suffix not in ('main', 'refs/heads/main'):
        raise GateError('Unexpected workflow ref')
    return path


def run_batch_date(run):
    try:
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError('timezone required')
        return created.astimezone(LONDON).date()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise GateError('Invalid original workflow creation time') from None


def _positive_id(value):
    return not isinstance(value, bool) and str(value).isdigit() and int(value) > 0


def _validate_context(env):
    if (env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or not re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', ''))
            or not _positive_id(env.get('GITHUB_RUN_ID'))
            or not _positive_id(env.get('GITHUB_RUN_ATTEMPT'))
            or env.get('GITHUB_EVENT_NAME') not in ('schedule', 'workflow_dispatch')):
        raise GateError('Invalid production workflow context')
    expected = {REPOSITORY + '/' + path + '@refs/heads/main' for path in CHILD_WORKFLOWS}
    if env.get('GITHUB_WORKFLOW_REF') not in expected:
        raise GateError('Unexpected child workflow identity')


def validate_child(run, env):
    _validate_context(env)
    if (str(run.get('id')) != env['GITHUB_RUN_ID']
            or str(run.get('run_attempt')) != env['GITHUB_RUN_ATTEMPT']
            or run.get('event') != env['GITHUB_EVENT_NAME']
            or run.get('head_branch') != 'main'
            or run.get('head_sha') != env['GITHUB_SHA']
            or run.get('repository', {}).get('full_name') != REPOSITORY
            or run.get('head_repository', {}).get('full_name') != REPOSITORY
            or normalized_path(run.get('path')) not in CHILD_WORKFLOWS
            or (REPOSITORY + '/' + normalized_path(run.get('path')) + '@refs/heads/main')
               != env['GITHUB_WORKFLOW_REF']):
        raise GateError('Child workflow identity mismatch')
    run_batch_date(run)


def _complete_rows(payload, key):
    rows = payload.get(key) if isinstance(payload, dict) else None
    count = payload.get('total_count') if isinstance(payload, dict) else None
    if (not isinstance(rows, list) or not isinstance(count, int) or isinstance(count, bool)
            or count != len(rows) or count > 100 or not all(isinstance(row, dict) for row in rows)):
        raise GateError('Incomplete or malformed workflow metadata')
    return rows


def select_main_run(payload, child):
    batch = run_batch_date(child)
    candidates = []
    for run in _complete_rows(payload, 'workflow_runs'):
        if run.get('event') != 'schedule' or normalized_path(run.get('path')) != MAIN_WORKFLOW:
            continue
        if run_batch_date(run) != batch:
            continue
        if (run.get('repository', {}).get('full_name') != REPOSITORY
                or run.get('head_repository', {}).get('full_name') != REPOSITORY
                or run.get('head_branch') != 'main'
                or run.get('head_sha') != child.get('head_sha')
                or not _positive_id(run.get('id')) or not _positive_id(run.get('run_attempt'))):
            raise GateError('Scheduled Main/V2 predecessor identity mismatch')
        candidates.append(run)
    if len(candidates) > 1:
        raise GateError('Ambiguous scheduled Main/V2 predecessors for this batch')
    return candidates[0] if candidates else None


def _list_path(child):
    day = run_batch_date(child)
    start = datetime.combine(day, daytime.min, LONDON).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), daytime.min, LONDON).astimezone(timezone.utc)
    stamp = lambda value: value.isoformat(timespec='seconds').replace('+00:00', 'Z')
    query = urlencode({'event': 'schedule', 'branch': 'main', 'per_page': 100,
                       'created': stamp(start) + '..' + stamp(end - timedelta(seconds=1))})
    return ROOT + '/workflows/screener.yml/runs?' + query


def _finished_successfully(run):
    status = run.get('status')
    if status == 'completed':
        if run.get('conclusion') == 'success':
            return True
        raise GateError('Main/V2 did not complete successfully; dependent screens remain blocked')
    if status not in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
        raise GateError('Unknown Main/V2 execution status')
    return False


def _verify_main_jobs(payload, parent):
    jobs = _complete_rows(payload, 'jobs')
    for name in ('test-completion', 'run-screener'):
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            raise GateError('Main/V2 prerequisite jobs were not successfully executed')
        job = matches[0]
        if (str(job.get('run_id')) != str(parent['id'])
                or str(job.get('run_attempt')) != str(parent['run_attempt'])
                or job.get('head_sha') != parent['head_sha']):
            raise GateError('Main/V2 prerequisite job identity mismatch')


def wait_for_main(child, get_json, *, clock=time.monotonic, sleep=time.sleep,
                  timeout=TIMEOUT_SECONDS, poll_seconds=POLL_SECONDS):
    if timeout <= 0 or poll_seconds < POLL_SECONDS:
        raise GateError('Invalid dependency wait configuration')
    deadline = clock() + timeout
    path = _list_path(child)
    pinned_id = None
    highest_attempt = 0
    while clock() < deadline:
        delay = poll_seconds
        try:
            parent = select_main_run(get_json(path), child)
            if parent is not None:
                parent_id = str(parent['id'])
                if pinned_id is not None and pinned_id != parent_id:
                    raise GateError('Scheduled Main/V2 predecessor changed while waiting')
                pinned_id = parent_id
                attempt = int(parent['run_attempt'])
                if attempt < highest_attempt:
                    raise GateError('Stale Main/V2 attempt metadata; no downstream screening started')
                highest_attempt = attempt
                if _finished_successfully(parent):
                    jobs_path = ROOT + f'/runs/{parent_id}/attempts/{attempt}/jobs?per_page=100'
                    _verify_main_jobs(get_json(jobs_path), parent)
                    # Re-list, rather than trust cached success or choose a different
                    # successful run: detect a late duplicate or a newly started rerun.
                    current = select_main_run(get_json(path), child)
                    if current is None or str(current['id']) != pinned_id:
                        raise GateError('Scheduled Main/V2 predecessor changed during verification')
                    current_attempt = int(current['run_attempt'])
                    if current_attempt < highest_attempt:
                        raise GateError('Stale Main/V2 attempt metadata during verification')
                    highest_attempt = current_attempt
                    if _finished_successfully(current) and current_attempt == attempt:
                        return current
            elif pinned_id is not None:
                raise GateError('Scheduled Main/V2 predecessor disappeared')
        except RetryableError as exc:
            delay = max(delay, exc.retry_after_seconds)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(delay, remaining))
    raise GateError('Timed out waiting for verified Main/V2 success; no downstream screening started')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicGitHub:
    """Anonymous GET only; pace even setup/final verification requests by headers."""
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time, sleep=time.sleep,
                 timeout=TIMEOUT_SECONDS, opener=None):
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.deadline = clock() + timeout
        self.next_request = 0.0
        self.opener = opener or build_opener(_NoRedirect())

    def _server_delay(self, headers):
        delay = 0.0
        value = headers.get('Retry-After')
        if value:
            try:
                delay = max(0, float(value))
            except ValueError:
                try:
                    until = parsedate_to_datetime(value)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    delay = max(0, until.timestamp() - self.wall_clock())
                except (TypeError, ValueError, OverflowError):
                    pass
        try:
            if int(headers.get('X-RateLimit-Remaining', '60')) <= 5:
                delay = max(delay, float(headers['X-RateLimit-Reset']) - self.wall_clock() + 5)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        return delay

    def get_json(self, path):
        if not path.startswith(ROOT + '/') or '..' in path.split('?', 1)[0]:
            raise GateError('Unexpected metadata endpoint')
        delay = self.next_request - self.clock()
        if delay > 0:
            if self.clock() + delay >= self.deadline:
                raise GateError('GitHub metadata cooldown exceeds dependency wait budget')
            self.sleep(delay)
        if self.clock() >= self.deadline:
            raise GateError('Dependency wait budget exhausted')
        request = Request('https://api.github.com' + path, method='GET', headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'market-screener-main-first-gate',
            'X-GitHub-Api-Version': '2022-11-28',
            'Cache-Control': 'no-cache',
        })
        try:
            with self.opener.open(request, timeout=min(30, self.deadline - self.clock())) as response:
                self.next_request = max(self.next_request, self.clock() + self._server_delay(response.headers))
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise GateError('Oversized workflow metadata')
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise GateError('Invalid workflow metadata shape')
                return data
        except HTTPError as exc:
            delay = self._server_delay(exc.headers)
            if exc.code == 429 or (exc.code == 403 and delay > 0) or exc.code >= 500:
                raise RetryableError('GitHub metadata temporarily unavailable', delay) from None
            raise GateError('GitHub metadata access failed; no fallback dispatch') from None
        except (OSError, IncompleteRead):
            raise RetryableError('GitHub metadata network failure') from None
        except (UnicodeError, ValueError):
            raise GateError('Invalid GitHub metadata response') from None


def main(env=None):
    env = os.environ if env is None else env
    try:
        _validate_context(env)
        if env['GITHUB_EVENT_NAME'] == 'workflow_dispatch':
            print('Explicit standalone manual recovery: no schedule dependency or additional run is created.')
            return 0
        api = PublicGitHub()
        # Initial identity fetch follows the same finite retry/reset policy.
        while True:
            try:
                child = api.get_json(ROOT + '/runs/' + env['GITHUB_RUN_ID'])
                break
            except RetryableError as exc:
                remaining = api.deadline - api.clock()
                if remaining <= 0:
                    raise GateError('Timed out obtaining original workflow identity') from None
                api.sleep(min(exc.retry_after_seconds, remaining))
        validate_child(child, env)
        parent = wait_for_main(child, api.get_json, clock=api.clock, sleep=api.sleep,
                               timeout=max(.001, api.deadline - api.clock()))
        print(f"Verified Main/V2 predecessor run {parent['id']} attempt {parent['run_attempt']} "
              f"for London batch {run_batch_date(child).isoformat()}.")
        return 0
    except GateError as exc:
        print('Main-first dependency gate blocked: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

