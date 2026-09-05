from contextlib import closing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard


class MatchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "test.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE matches (id INTEGER PRIMARY KEY, guild_id INTEGER, mode TEXT, winner INTEGER, team_one TEXT, team_two TEXT, map_name TEXT, created_at TEXT)")
            connection.executemany("INSERT INTO matches VALUES (?, ?, ?, 1, ?, ?, ?, '2026-09-05')", [
                (1, 10, "control_1v1", "12", "3", "Checkout"),
                (2, 10, "gnashers_2v2", "4,12", "5,6", "Checkout"),
                (3, 10, "control_1v1", "112", "3", "Checkout"),
                (4, 20, "control_1v1", "12", "3", "Checkout"),
                (5, 10, "control_1v1", "3", "12", "Foundation"),
                (6, 10, "control_1v1", "3", "12", "Checkout"),
            ])
        self.path_patch = patch.object(dashboard, "DATABASE_PATH", path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.env_patch = patch.dict(os.environ, {"DASHBOARD_GUILD_ID": "10"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.client = dashboard.app.test_client()

    def test_default_order_and_guild(self):
        response = self.client.get("/api/matches")
        self.assertEqual([row["id"] for row in response.json], [6, 5, 3, 2, 1])
        self.assertNotIn("Link", response.headers)

    def test_combined_filters_and_exact_player_membership(self):
        response = self.client.get("/api/matches?mode=control_1v1&map=checkout&player=12")
        self.assertEqual([row["id"] for row in response.json], [6, 1])
        self.assertEqual([row["id"] for row in self.client.get("/api/matches?player=12").json], [6, 5, 2, 1])

    def test_cursor_preserves_filters_and_avoids_duplicates(self):
        response = self.client.get("/api/matches?guild_id=10&map=Checkout&player=12&limit=2")
        self.assertEqual([row["id"] for row in response.json], [6, 2])
        next_url = response.headers["Link"].split(">")[0][1:]
        with closing(sqlite3.connect(dashboard.DATABASE_PATH)) as connection, connection:
            connection.execute("INSERT INTO matches VALUES (7, 10, 'control_1v1', 1, '12', '3', 'Checkout', '2026-09-05')")
        response = self.client.get(next_url)
        self.assertEqual([row["id"] for row in response.json], [1])
        self.assertNotIn("Link", response.headers)

    def test_invalid_filters(self):
        for query in ("mode=invalid", "player=0", "player=-1", "player=abc", "player=9223372036854775808", "before=-1", "before=abc"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/matches?" + query).status_code, 400)

    def test_limits_and_empty_results(self):
        self.assertEqual(len(self.client.get("/api/matches?limit=0").json), 1)
        self.assertEqual(len(self.client.get("/api/matches?limit=bad").json), 5)
        self.assertEqual(self.client.get("/api/matches?map=' OR 1=1--").json, [])
        self.assertEqual(self.client.get("/api/matches?before=1").json, [])

    def test_html_filters_links_and_empty_state(self):
        response = self.client.get("/matches?guild_id=10&player=12&limit=1")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('name=guild_id value="10"', html)
        self.assertIn('/match/6?guild_id=10', html)
        self.assertIn('Older matches', html)
        self.assertIn('player=12', html)
        self.assertIn('No matches found.', self.client.get('/matches?player=999').get_data(as_text=True))
        html = self.client.get('/matches?map=<script>alert(1)</script>').get_data(as_text=True)
        self.assertNotIn('<script>', html)


if __name__ == "__main__":
    unittest.main()
