import unittest

from elo import calculate_match_changes, parse_match_stats, parse_team


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

    def test_parse_control_stats(self):
        stats = parse_match_stats(
            "1 captures=2 breaks=3 kills=10 deaths=4 assists=5 score=99\n"
            "2 captures=1 breaks=2 kills=8 deaths=6 assists=4 score=80",
            "control_1v1",
            [1, 2],
        )
        self.assertEqual(stats[1]["captures"], 2)

    def test_gnashers_rejects_control_only_stats(self):
        with self.assertRaises(ValueError):
            parse_match_stats("1 kills=10 deaths=3 score=100 captures=1", "gnashers_1v1", [1])


if __name__ == "__main__":
    unittest.main()
