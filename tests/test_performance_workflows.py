"""Guard the account-wide assumptions on which higher throughput depends."""
from pathlib import Path
import re
import unittest


class WorkflowBudgetTests(unittest.TestCase):
    def test_production_workflows_remain_serialized_and_scheduled_at_03_uk(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        for name, command in (
            ('screener.yml', 'python cloud_screener.py'),
            ('book-margin-screen.yml', 'python book_margin_screen.py'),
            ('negative-ev-fcf-screen.yml', 'python negative_ev_fcf_screen.py'),
        ):
            with self.subTest(workflow=name):
                text = (root / name).read_text()
                self.assertRegex(text, r"cron: ['\"]0 3 \* \* 6['\"]")
                self.assertRegex(text, r'timezone: [\"]?Europe/London')
                self.assertEqual(text.count('group: production-screener'), 1)
                self.assertIn('cancel-in-progress: false', text)
                self.assertIn('queue: max', text)
                self.assertIn("FMP_REQUESTS_PER_MINUTE: '1800'", text)
                self.assertIn("FMP_CONCURRENCY: '30'", text)
                self.assertLess(text.index('run: sleep 61'), text.index(command))
                self.assertIn("github.event_name != 'pull_request'", text)
                self.assertIn('test_fmp_limits.py', text)


if __name__ == '__main__':
    unittest.main()
