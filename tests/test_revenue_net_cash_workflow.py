"""Offline production-origin and shared-budget contract for the new workflow."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATH = '.github/workflows/revenue-net-cash-screen.yml'
SPEC = importlib.util.spec_from_file_location('revenue_ordering', ROOT / 'scripts/wait_for_main_screen.py')
gate = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(gate)

class WorkflowTests(unittest.TestCase):
    def setUp(self): self.text = (ROOT / PATH).read_text()
    def test_weekly_london_and_main_only_production(self):
        self.assertIn("cron: '0 3 * * 6'", self.text)
        self.assertIn('timezone: Europe/London', self.text)
        screen = self.text.split('  screen:',1)[1]
        self.assertIn("github.event_name != 'pull_request'", screen)
        self.assertIn("github.ref == 'refs/heads/main'", screen)
        self.assertIn("github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'",screen)
        self.assertNotIn('\n  push:', self.text)
        self.assertNotIn('\n  issues:', self.text)
    def test_registered_child_and_self_trigger(self):
        self.assertIn(PATH, gate.CHILD_WORKFLOWS)
        self.assertIn("- '"+PATH+"'", self.text)
        self.assertEqual(gate.MAIN_WORKFLOW, '.github/workflows/screener.yml')
    def test_shared_queue_budget_handoff_and_test_dependency(self):
        screen = self.text.split('  screen:',1)[1]
        for value in ('needs: [test, wait-for-main]', 'group: production-screener',
                      'queue: max', 'cancel-in-progress: false', "FMP_REQUESTS_PER_MINUTE: '1800'",
                      "FMP_CONCURRENCY: '30'", 'run: sleep 61'):
            self.assertIn(value, screen)
        gate_job = self.text.split('  wait-for-main:',1)[1].split('  screen:',1)[0]
        self.assertNotIn('concurrency:', gate_job)
        self.assertIn('needs: test', gate_job)
    def test_no_new_credentials_or_artifacts(self):
        self.assertIn('secrets.FMP_KEY', self.text)
        self.assertIn('secrets.SERVICE_ACCOUNT_JSON', self.text)
        self.assertIn('secrets.REVENUE_NET_CASH_SHEET_ID', self.text)
        self.assertNotIn('upload-artifact', self.text)
        self.assertNotIn('ownership', self.text.lower())
        self.assertIn('if: always()', self.text)
        self.assertIn('contents: read', self.text)
    def test_manual_context_accepted_without_api(self):
        env = {'GITHUB_REPOSITORY':gate.REPOSITORY, 'GITHUB_REF':'refs/heads/main',
               'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1',
               'GITHUB_EVENT_NAME':'workflow_dispatch',
               'GITHUB_WORKFLOW_REF':gate.REPOSITORY+'/'+PATH+'@refs/heads/main'}
        with patch.object(gate.PublicGitHub, 'get_json', side_effect=AssertionError('No HTTP for manual context')):
            self.assertEqual(gate.main(env), 0)
    def test_pr_context_cannot_pass_gate(self):
        env = {'GITHUB_REPOSITORY':gate.REPOSITORY,'GITHUB_REF':'refs/pull/1/merge',
               'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1',
               'GITHUB_EVENT_NAME':'pull_request','GITHUB_WORKFLOW_REF':gate.REPOSITORY+'/'+PATH+'@refs/heads/main'}
        with self.assertRaises(gate.GateError): gate._validate_context(env)

if __name__ == '__main__': unittest.main()
