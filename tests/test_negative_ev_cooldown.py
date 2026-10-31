import unittest
from appearance_cooldown_cases import AppearanceCooldownCases
from negative_ev_fcf_screen import CONFIG
from screenerV3 import negative_ev_output


class NegativeEVCooldownTests(AppearanceCooldownCases, unittest.TestCase):
    output, config = negative_ev_output, CONFIG
    ranked, kind = 'Negative EV Ranked', 'negative_ev'


if __name__ == '__main__': unittest.main()
