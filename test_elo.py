import unittest

from elo import balance_teams, calculate_match_changes, calculate_trueskill_changes, canonical_matchup, gow2_rank, parse_match_stats, parse_player_stats, parse_team, team_key, trueskill_display


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
            "1 captures=2 breaks=3 kills=10 deaths=4 assists=5 damage=900 score=99\n"
            "2 captures=1 breaks=2 kills=8 deaths=6 assists=4 damage=800 score=80",
            "control_1v1",
            [1, 2],
        )
        self.assertEqual(stats[1]["captures"], 2)

    def test_gnashers_rejects_control_only_stats(self):
        with self.assertRaises(ValueError):
            parse_match_stats("1 kills=10 deaths=3 damage=500 score=100 captures=1", "gnashers_1v1", [1])

    def test_parse_semicolon_separated_stats(self):
        stats = parse_match_stats(
            "1 kills=10 deaths=3 damage=500 score=100; 2 kills=8 deaths=5 damage=450 score=90",
            "gnashers_1v1",
            [1, 2],
        )
        self.assertEqual(stats[2]["kills"], 8)

    def test_parse_player_stats(self):
        stats = parse_player_stats("kills=10 deaths=3 damage=500 score=100", "gnashers_1v1")
        self.assertEqual(stats["score"], 100)

    def test_gnashers_2v2_tracks_assists(self):
        stats = parse_player_stats("kills=10 deaths=3 assists=6 damage=500 score=100", "gnashers_2v2")
        self.assertEqual(stats["assists"], 6)

    def test_team_matchup_is_order_independent(self):
        self.assertEqual(team_key([2, 1]), "1,2")
        first = canonical_matchup([1, 2], [3, 4])
        second = canonical_matchup([4, 3], [2, 1])
        self.assertEqual(first[:2], second[:2])
        self.assertNotEqual(first[2], second[2])

    def test_balance_teams(self):
        team_one, team_two = balance_teams([(1, 1200), (2, 1100), (3, 1000), (4, 900)])
        self.assertEqual(len(team_one), 2)
        self.assertEqual(len(team_two), 2)
        self.assertEqual(sum(dict([(1, 1200), (2, 1100), (3, 1000), (4, 900)])[player] for player in team_one), 2100)

    def test_trueskill_favors_an_underdog(self):
        changes = calculate_trueskill_changes("control_1v1", [(1, 20.0, 4.0)], [(2, 30.0, 4.0)], 1)
        self.assertGreater(changes[0].delta, 0)
        self.assertLess(changes[1].delta, 0)

    def test_trueskill_reduces_uncertainty(self):
        change = calculate_trueskill_changes("gnashers_1v1", [(1, 25.0, 8.333333)], [(2, 25.0, 8.333333)], 1)[0]
        self.assertLess(change.new_sigma, change.old_sigma)

    def test_true_skill_display_and_gow2_rank(self):
        self.assertEqual(trueskill_display(25.0, 25.0 / 3), 1000)
        self.assertEqual(gow2_rank(1500), (5, "Wings"))


if __name__ == "__main__":
    unittest.main()
