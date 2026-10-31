"""Shared offline cases, run independently by both production PR test jobs."""
import copy
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

from screenerV3.revenue_net_cash_cooldown import appearance_eligible_on


class MissingSheet(Exception):
    pass


class Tab:
    def __init__(self, title, ident):
        self.title, self.id, self.rows, self.note = title, ident, [], ''
    def update(self, range_name, values, value_input_option):
        offset = int(range_name[1:]) - 1
        self.rows[offset:offset + len(values)] = copy.deepcopy(values)
    def get_all_values(self): return copy.deepcopy(self.rows)
    def freeze(self, rows): pass
    def resize(self, rows, cols): pass
    def clear(self): self.rows = []
    def update_note(self, cell, note): self.note = note
    def get_note(self, cell): return self.note


class Sheet:
    def __init__(self, ranked):
        self.tabs = []
        self.add_worksheet(ranked, 100, 10)
    def worksheets(self): return list(self.tabs)
    def worksheet(self, title):
        for tab in self.tabs:
            if tab.title == title: return tab
        raise MissingSheet(title)
    def add_worksheet(self, title, rows, cols, index=None):
        tab = Tab(title, max((tab.id for tab in self.tabs), default=99) + 1)
        self.tabs.insert(len(self.tabs) if index is None else index, tab)
        return tab


def snapshot(sheet):
    return [(t.title, t.id, copy.deepcopy(t.rows), t.note) for t in sheet.tabs]


def title(day): return f'{day.day}-{day.strftime("%b")}-{day.year}'


class AppearanceCooldownCases:
    def setUp(self):
        fake = types.SimpleNamespace(WorksheetNotFound=MissingSheet)
        p = patch.dict(sys.modules, {'gspread': fake})
        p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'GITHUB_RUN_ID': 'offline-test-run', 'GITHUB_SHA': 'f' * 40})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(self.output, 'RUN_OWNER', 'offline-test-run')
        p.start(); self.addCleanup(p.stop)
        self.sheet = Sheet(self.ranked)
        self.batch = date(2026, 11, 7)

    def publish(self, day=None, symbols=('A',), status='complete'):
        return self.output.publish(self.sheet, day or self.batch,
            [['Ticker', 'Name']] + [[s, 'Same company'] for s in symbols],
            [['Ticker', 'Status']] + [[s, 'match'] for s in symbols],
            {'status': status, 'matched_rows': len(symbols)}, self.config,
            [['Ticker', 'Record', 'JSON', 'UTC']] + [[s, 'input', '{}', 'now'] for s in symbols])

    def result(self, day=None): return self.sheet.worksheet(title(day or self.batch)).rows
    def counts(self, day=None):
        return dict(self.sheet.worksheet('Coverage ' + title(day or self.batch)).rows[1:])

    def load_legacy(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'appearance_cooldown_legacy.json').read_text())[self.kind]
        self.assertEqual(fixture['config'], self.config)
        self.assertEqual(fixture['criteria_hash'], self.output.stable_hash(self.config))
        for entry in fixture['tabs']:
            tab = self.sheet.add_worksheet(entry['title'], 100, 50)
            tab.id, tab.rows, tab.note = entry['id'], entry['rows'], entry['note']
        return fixture

    def assert_blocked_without_writes(self, error=Exception):
        before = snapshot(self.sheet)
        with self.assertRaises(error): self.publish(symbols=('A', 'B'))
        self.assertEqual(snapshot(self.sheet), before)

    def test_legacy_24_and_31_oct_receipts_suppress_next_week(self):
        self.load_legacy()
        before = snapshot(self.sheet)
        self.publish(symbols=('LEGACY24', 'NEW', 'LEGACY31'))
        self.assertEqual(self.result()[1:], [['NEW', 'Same company']])
        for old in before:
            tab = self.sheet.worksheet(old[0])
            self.assertEqual((tab.id, tab.rows, tab.note), old[1:])
        for day in (date(2026, 10, 24), date(2026, 10, 31)):
            receipt = self.output.resume_complete(self.sheet, day, self.config)
            self.assertEqual(receipt['commit'], 'ff577314d7f24fe4ef501cd2f2a9fd0659ae034d')

    def test_legacy_same_day_resume_is_unchanged(self):
        self.load_legacy()
        day = date(2026, 10, 31)
        before = snapshot(self.sheet)
        receipt = self.output.resume_complete(self.sheet, day, self.config)
        self.assertEqual(self.output.publish(self.sheet, day, self.result(day),
                         [['ignored']], {'status': 'complete'}, self.config), receipt)
        self.assertEqual(snapshot(self.sheet), before)

    def test_legacy_criteria_hash_is_not_sorted_json_hash(self):
        self.load_legacy()
        parsed = json.loads(dict(self.sheet.worksheet('Coverage 31-Oct-2026').rows[1:])['Criteria'])
        self.assertNotEqual(self.output.stable_hash(parsed), self.output.stable_hash(self.config))
        self.assertIsNotNone(self.output.resume_complete(self.sheet, date(2026, 10, 31), parsed, historical=True))
        with self.assertRaises(ValueError):
            self.output.resume_complete(self.sheet, date(2026, 10, 31), parsed)

    def test_historical_unknown_schema_or_numeric_type_change_is_not_canonicalized(self):
        self.load_legacy()
        changed = copy.deepcopy(self.config)
        changed['unknown_schema_key'] = True
        with self.assertRaises(ValueError):
            self.output.resume_complete(self.sheet, date(2026, 10, 31), changed, historical=True)
        changed = copy.deepcopy(self.config)
        if self.kind == 'book_margin': changed['market_cap_usd_max_inclusive'] = 75000000.0
        else: changed['data_freshness']['quote_max_age_days'] = 7.0
        with self.assertRaises(ValueError):
            self.output.resume_complete(self.sheet, date(2026, 10, 31), changed, historical=True)

    def test_latest_actual_appearance_controls_not_first_appearance(self):
        self.load_legacy()
        self.publish(date(2027, 10, 25), ('LEGACY24',))
        self.assertEqual(len(self.result(date(2027, 10, 25))), 1)
        self.publish(date(2027, 10, 31), ('LEGACY24',))
        self.assertEqual(len(self.result(date(2027, 10, 31))), 2)

    def test_anniversary_exact_boundary_and_prior_day(self):
        self.publish(date(2025, 11, 8), ('A',))
        self.publish(symbols=('A', 'B'))
        self.assertEqual(self.result()[1:], [['B', 'Same company']])
        self.publish(date(2026, 11, 8), ('A',))
        self.assertEqual(self.result(date(2026, 11, 8))[1:], [['A', 'Same company']])

    def test_leap_day_and_month_end_boundaries(self):
        for prior, anniversary in ((date(2024, 2, 29), date(2025, 2, 28)),
                                   (date(2023, 2, 28), date(2024, 2, 28)),
                                   (date(2025, 1, 31), date(2026, 1, 31)),
                                   (date(2025, 4, 30), date(2026, 4, 30))):
            with self.subTest(prior=prior):
                self.sheet = Sheet(self.ranked)
                self.assertEqual(appearance_eligible_on(prior), anniversary)
                self.publish(prior, ('A',))
                self.publish(anniversary - timedelta(days=1), ('A',))
                self.assertEqual(len(self.result(anniversary - timedelta(days=1))), 1)
                self.publish(anniversary, ('A',))
                self.assertEqual(len(self.result(anniversary)), 2)

    def test_more_than_52_actual_result_tabs_in_mixed_physical_order(self):
        for i in range(60): self.publish(date(2026, 1, 1) + timedelta(days=i), (f'H{i}',))
        self.sheet.tabs[1:] = reversed(self.sheet.tabs[1:])
        history = [tab for tab in self.sheet.tabs if tab.title == '29-Feb-2026']
        self.assertEqual(history, [])  # no invented date arithmetic
        self.assertGreater(self.sheet.tabs.index(self.sheet.worksheet('1-Mar-2026')), 52)
        old_order = [t.id for t in self.sheet.tabs]
        self.publish(symbols=('H59', 'NEW'))
        self.assertEqual(self.result()[1:], [['NEW', 'Same company']])
        self.assertEqual([t.id for t in self.sheet.tabs if t.id in old_order], old_order)
        self.assertEqual([t.title for t in self.sheet.tabs[:2]], [self.ranked, title(self.batch)])

    def test_helpers_and_unverified_or_staging_dates_are_not_appearances(self):
        helpers = ['Run Map', 'Metrics', 'Refresh Log', 'Read me', 'Configuration',
                   'Coverage 1-Nov-2026', 'Inputs 1-Nov-2026', 'Latest assessment audit',
                   'staging 1-Nov-2026', 'provisional 1-Nov-2026', 'audit 1-Nov-2026']
        for name in helpers:
            tab = self.sheet.add_worksheet(name, 100, 10)
            tab.rows, tab.note = [['Ticker'], ['A']], self.output.PREFIX + 'not a receipt'
        self.sheet.tabs[0].rows = [['Ticker'], ['A']]
        for name, note in [('1-Nov-2026', ''), ('2-Nov-2026', self.output.STAGING + 'other'),
                           ('3-Nov-2026', self.output.SEALED + '{}')]:
            tab = self.sheet.add_worksheet(name, 100, 10)
            tab.rows, tab.note = [['Ticker'], ['A']], note
        self.publish()
        self.assertEqual(self.result()[1:], [['A', 'Same company']])

    def test_exact_tickers_no_case_exchange_or_issuer_alias_matching(self):
        self.publish(date(2026, 10, 31), ('ABC',))
        self.publish(symbols=('ABC.L', 'ABC', 'abc', ' ABC', 'OTHER'))
        self.assertEqual([r[0] for r in self.result()[1:]], ['ABC.L', 'abc', ' ABC', 'OTHER'])

    def test_coverage_counts_and_all_candidate_audit_inputs_preserved(self):
        self.publish(date(2026, 10, 31), ('A',))
        self.publish(symbols=('B', 'A', 'C'), status='complete_with_unresolved')
        counts = self.counts()
        self.assertEqual([counts[k] for k in ('qualifying_rows_before_cooldown',
                         'cooldown_suppressed_rows', 'matched_rows')], ['3', '1', '2'])
        self.assertEqual(counts['status'], 'complete_with_unresolved')
        self.assertEqual([r[0] for r in self.result()[1:]], ['B', 'C'])
        self.assertEqual([r[0] for r in self.sheet.worksheet('Latest assessment audit').rows[1:]], ['B', 'A', 'C'])
        self.assertEqual([r[0] for r in self.sheet.worksheet('Inputs ' + title(self.batch)).rows[1:]], ['B', 'A', 'C'])

    def test_audit_and_input_only_candidates_never_enter_results(self):
        self.output.publish(self.sheet, self.batch, [['Ticker']], [['Ticker'], ['AUDIT_ONLY']],
            {'status': 'complete'}, self.config,
            [['Ticker', 'Record', 'JSON', 'UTC'], ['INPUT_ONLY', 'input', '{}', 'now']])
        self.assertEqual(self.result(), [['Ticker']])
        self.assertEqual(self.counts()['matched_rows'], '0')

    def test_all_suppressed_or_no_qualifiers_are_valid_completed_empty_results(self):
        for symbols, qualifying, suppressed in [(('A',), '1', '1'), ((), '0', '0')]:
            with self.subTest(symbols=symbols):
                self.sheet = Sheet(self.ranked)
                self.publish(date(2026, 10, 31), ('A',))
                receipt = self.publish(symbols=symbols)
                self.assertEqual(receipt['status'], 'complete')
                self.assertEqual(self.result(), [['Ticker', 'Name']])
                self.assertEqual([self.counts()[k] for k in ('qualifying_rows_before_cooldown',
                                 'cooldown_suppressed_rows', 'matched_rows')], [qualifying, suppressed, '0'])
                self.assertEqual(self.output.resume_complete(self.sheet, self.batch, self.config), receipt)

    def test_empty_completed_history_is_valid(self):
        self.publish(date(2026, 10, 31), ())
        self.publish()
        self.assertEqual(len(self.result()), 2)

    def test_expired_history_does_not_require_unnecessary_content_reads(self):
        self.publish(date(2025, 11, 7), ('A',))
        tab = self.sheet.worksheet('7-Nov-2025')
        with patch.object(tab, 'get_note', side_effect=AssertionError('expired history read')):
            self.publish()
        self.assertEqual(len(self.result()), 2)

    def test_same_day_retry_filters_history_but_not_own_membership(self):
        self.publish(date(2026, 10, 31), ('A',))
        receipt = self.publish(symbols=('A', 'B'))
        before = snapshot(self.sheet)
        self.assertEqual(self.publish(symbols=('A', 'B')), receipt)
        self.assertEqual(snapshot(self.sheet), before)
        self.assertEqual(self.output.resume_complete(self.sheet, self.batch, self.config), receipt)
        with self.assertRaises(ValueError): self.publish(symbols=('C',))
        self.assertEqual(snapshot(self.sheet), before)

    def test_completed_current_resume_does_not_reprocess_history(self):
        receipt = self.publish()
        with patch.object(self.sheet, 'worksheets', side_effect=AssertionError('history must not be read')):
            self.assertEqual(self.publish(), receipt)
            self.assertEqual(self.output.resume_complete(self.sheet, self.batch, self.config), receipt)

    def test_bad_historical_receipts_fail_closed_without_writes(self):
        for changes in ({'tab_id': -1}, {'batch_date': '2026-10-30'}, {'status': 'partial'},
                        {'criteria_hash': '0' * 64}, {'content_hash': '0' * 64}, {'coverage_hash': '0' * 64},
                        {'inputs_hash': '0' * 64}):
            with self.subTest(changes=changes):
                self.sheet = Sheet(self.ranked); self.load_legacy()
                tab = self.sheet.worksheet('31-Oct-2026')
                receipt = json.loads(tab.note[len(self.output.PREFIX):]); receipt.update(changes)
                tab.note = self.output.PREFIX + json.dumps(receipt)
                self.assert_blocked_without_writes(ValueError)

    def test_missing_tampered_and_unsealed_historical_evidence_fails_closed(self):
        for name in ('Coverage 31-Oct-2026', 'Inputs 31-Oct-2026'):
            for failure in ('missing', 'content', 'seal'):
                with self.subTest(name=name, failure=failure):
                    self.sheet = Sheet(self.ranked); self.load_legacy()
                    tab = self.sheet.worksheet(name)
                    if failure == 'missing': self.sheet.tabs.remove(tab)
                    elif failure == 'content': tab.rows[0][0] = 'tampered'
                    else: tab.note = self.output.STAGING + self.output.RUN_OWNER
                    self.assert_blocked_without_writes()

    def test_historical_companion_seal_owner_must_match_receipt_origin(self):
        for name in ('Coverage 31-Oct-2026', 'Inputs 31-Oct-2026'):
            with self.subTest(name=name):
                self.sheet = Sheet(self.ranked); self.load_legacy()
                tab = self.sheet.worksheet(name)
                seal = json.loads(tab.note[len(self.output.SEALED):])
                seal['owner'] = 'another-origin-run'
                tab.note = self.output.SEALED + json.dumps(seal)
                self.assert_blocked_without_writes(ValueError)

    def test_historical_receipt_requires_nonempty_production_origin_and_commit(self):
        for key in ('origin_run_id', 'commit'):
            for value in (None, '', '   ', 123):
                with self.subTest(key=key, value=value):
                    self.sheet = Sheet(self.ranked); self.load_legacy()
                    tab = self.sheet.worksheet('31-Oct-2026')
                    receipt = json.loads(tab.note[len(self.output.PREFIX):])
                    receipt[key] = value
                    tab.note = self.output.PREFIX + json.dumps(receipt)
                    self.assert_blocked_without_writes(ValueError)

    def test_local_receipt_can_resume_same_day_but_is_not_production_history(self):
        with patch.dict(os.environ), patch.object(self.output, 'RUN_OWNER', 'local-offline-test'):
            os.environ.pop('GITHUB_RUN_ID', None); os.environ.pop('GITHUB_SHA', None)
            day = date(2026, 10, 31)
            receipt = self.publish(day)
            self.assertIsNone(receipt['origin_run_id'])
            self.assertEqual(self.output.resume_complete(self.sheet, day, self.config), receipt)
            self.assertEqual(self.publish(day), receipt)
        self.assert_blocked_without_writes(ValueError)

    def test_exact_historical_matrix_consumed_is_reverified_after_second_read(self):
        self.load_legacy()
        tab = self.sheet.worksheet('31-Oct-2026')
        actual = tab.get_all_values()
        changed = [actual[0], ['DIFFERENT', 'Concurrent edit']]
        with patch.object(tab, 'get_all_values', side_effect=[actual, changed]):
            self.assert_blocked_without_writes(ValueError)

    def test_history_listing_and_each_read_failure_block_without_writes(self):
        self.load_legacy()
        for tab_name, method in ((None, 'worksheets'), ('31-Oct-2026', 'get_note'),
                                 ('31-Oct-2026', 'get_all_values'),
                                 ('Coverage 31-Oct-2026', 'get_all_values'),
                                 ('Inputs 31-Oct-2026', 'get_all_values')):
            target = self.sheet if tab_name is None else self.sheet.worksheet(tab_name)
            with self.subTest(tab=tab_name, method=method), patch.object(target, method, side_effect=RuntimeError('read failed')):
                self.assert_blocked_without_writes(RuntimeError)

    def test_invalid_future_or_malformed_completed_dates_fail_closed(self):
        for name, note in [('31-Feb-2026', ''), ('8-Nov-2026', ''),
                           ('31-Oct-2026', self.output.PREFIX + '{bad json')]:
            with self.subTest(name=name):
                self.sheet = Sheet(self.ranked)
                self.sheet.add_worksheet(name, 100, 10).note = note
                self.assert_blocked_without_writes(ValueError)

    def test_ranked_must_stay_leftmost_before_any_new_write(self):
        self.sheet.tabs[0].title = 'Unexpected'
        self.assert_blocked_without_writes(ValueError)

    def test_existing_pr_workflow_watches_shared_cooldown_code_and_tests(self):
        filename = 'book-margin-screen.yml' if self.kind == 'book_margin' else 'negative-ev-fcf-screen.yml'
        workflow = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / filename).read_text()
        for path in ('screenerV3/appearance_cooldown.py', 'screenerV3/revenue_net_cash_cooldown.py',
                     'tests/appearance_cooldown_cases.py', 'tests/fixtures/appearance_cooldown_legacy.json'):
            self.assertIn("      - '" + path + "'", workflow)
        self.assertIn("github.event_name != 'pull_request'", workflow)
