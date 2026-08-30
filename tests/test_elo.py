import unittest

from elo import calculate_match_changes, parse_team


class EloTests(unittest.TestCase):
    def test_underdog_win_gains_more(self):
        changes = calculate_match_changes("control_1v1", [(1, 800)], [(2, 1200)], 1)
        self.assertEqual(changes[0].delta, 29)
        self.assertEqual(changes[1].delta, -29)

    def test_team_rating_change_is_shared(self):
        changes = calculate_match_changes("gnashers_2v2", [(1, 1000), (2, 1000)], [(3, 1000), (4, 1000)], 1)
        self.assertEqual([change.delta for change in changes], [16, 16, -16, -16])

    def test_parse_mentions(self):
        self.assertEqual(parse_team("<@123>, <@!456>", 2), [123, 456])

    def test_reject_wrong_team_size(self):
        with self.assertRaises(ValueError):
            calculate_match_changes("control_3v3", [(1, 1000)], [(2, 1000)], 1)


if __name__ == "__main__":
    unittest.main()
