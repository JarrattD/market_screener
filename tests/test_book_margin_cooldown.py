import unittest
from appearance_cooldown_cases import AppearanceCooldownCases
from book_margin_screen import CONFIG
from screenerV3 import book_margin_output


class BookMarginCooldownTests(AppearanceCooldownCases, unittest.TestCase):
    output, config = book_margin_output, CONFIG
    ranked, kind = 'Book Margin Ranked', 'book_margin'


if __name__ == '__main__': unittest.main()
