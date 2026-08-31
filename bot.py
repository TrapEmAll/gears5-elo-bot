from __future__ import annotations

import os
import sqlite3
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from elo import MODES, balance_teams, calculate_match_changes, canonical_matchup, mode_label, parse_player_list, parse_player_stats, parse_team, stat_names, team_size

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "gears5_elo.sqlite3"))
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DEFAULT_RATING = 1000
DEFAULT_K_FACTOR = 32
ELO_TIERS = (
    (0, "Bronze", discord.Color.from_rgb(176, 112, 64)),
    (1000, "Silver", discord.Color.light_grey()),
    (1200, "Gold", discord.Color.gold()),
    (1400, "Onyx", discord.Color.dark_grey()),
    (1600, "Master", discord.Color.purple()),
)


class EloDatabase:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                games INTEGER NOT NULL DEFAULT 0,
                current_streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, mode)
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                winner INTEGER NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                map_name TEXT NOT NULL DEFAULT 'Unknown'
            );
            CREATE TABLE IF NOT EXISTS match_player_stats (
                match_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                captures INTEGER NOT NULL DEFAULT 0,
                breaks INTEGER NOT NULL DEFAULT 0,
                kills INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0,
                assists INTEGER NOT NULL DEFAULT 0,
                damage INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                rating_before INTEGER NOT NULL DEFAULT 0,
                rating_delta INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, user_id),
                FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS team_matchups (
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_a TEXT NOT NULL,
                team_b TEXT NOT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                team_a_wins INTEGER NOT NULL DEFAULT 0,
                team_b_wins INTEGER NOT NULL DEFAULT 0,
                team_a_captures INTEGER NOT NULL DEFAULT 0,
                team_a_breaks INTEGER NOT NULL DEFAULT 0,
                team_a_kills INTEGER NOT NULL DEFAULT 0,
                team_a_deaths INTEGER NOT NULL DEFAULT 0,
                team_a_assists INTEGER NOT NULL DEFAULT 0,
                team_a_damage INTEGER NOT NULL DEFAULT 0,
                team_a_score INTEGER NOT NULL DEFAULT 0,
                team_b_captures INTEGER NOT NULL DEFAULT 0,
                team_b_breaks INTEGER NOT NULL DEFAULT 0,
                team_b_kills INTEGER NOT NULL DEFAULT 0,
                team_b_deaths INTEGER NOT NULL DEFAULT 0,
                team_b_assists INTEGER NOT NULL DEFAULT 0,
                team_b_damage INTEGER NOT NULL DEFAULT 0,
                team_b_score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, mode, team_a, team_b)
            );
            CREATE TABLE IF NOT EXISTS team_performance (
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_key TEXT NOT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                captures INTEGER NOT NULL DEFAULT 0,
                breaks INTEGER NOT NULL DEFAULT 0,
                kills INTEGER NOT NULL DEFAULT 0,
                deaths INTEGER NOT NULL DEFAULT 0,
                assists INTEGER NOT NULL DEFAULT 0,
                damage INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, mode, team_key)
            );
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS elo_settings (
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                starting_rating INTEGER NOT NULL DEFAULT 1000,
                k_factor INTEGER NOT NULL DEFAULT 32,
                PRIMARY KEY (guild_id, mode)
            );
            CREATE TABLE IF NOT EXISTS challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                challenger_id INTEGER NOT NULL,
                opponent_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(match_player_stats)")}
        if "damage" not in columns:
            self.connection.execute("ALTER TABLE match_player_stats ADD COLUMN damage INTEGER NOT NULL DEFAULT 0")
        if "rating_before" not in columns:
            self.connection.execute("ALTER TABLE match_player_stats ADD COLUMN rating_before INTEGER NOT NULL DEFAULT 0")
        if "rating_delta" not in columns:
            self.connection.execute("ALTER TABLE match_player_stats ADD COLUMN rating_delta INTEGER NOT NULL DEFAULT 0")
        match_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(matches)")}
        if "season_id" not in match_columns:
            self.connection.execute("ALTER TABLE matches ADD COLUMN season_id INTEGER")
        if "map_name" not in match_columns:
            self.connection.execute("ALTER TABLE matches ADD COLUMN map_name TEXT NOT NULL DEFAULT 'Unknown'")
        rating_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(ratings)")}
        if "current_streak" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN current_streak INTEGER NOT NULL DEFAULT 0")
        if "best_streak" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN best_streak INTEGER NOT NULL DEFAULT 0")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def get_rating(self, guild_id: int, user_id: int, mode: str) -> int:
        row = self.connection.execute(
            "SELECT rating FROM ratings WHERE guild_id=? AND user_id=? AND mode=?",
            (guild_id, user_id, mode),
        ).fetchone()
        return row["rating"] if row else self.elo_settings(guild_id, mode)["starting_rating"]

    def elo_settings(self, guild_id: int, mode: str):
        row = self.connection.execute("SELECT starting_rating, k_factor FROM elo_settings WHERE guild_id=? AND mode=?", (guild_id, mode)).fetchone()
        return row or {"starting_rating": DEFAULT_RATING, "k_factor": DEFAULT_K_FACTOR}

    def set_elo_settings(self, guild_id: int, mode: str, starting_rating: int, k_factor: int):
        self.connection.execute("INSERT INTO elo_settings (guild_id, mode, starting_rating, k_factor) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, mode) DO UPDATE SET starting_rating=excluded.starting_rating, k_factor=excluded.k_factor", (guild_id, mode, starting_rating, k_factor))
        self.connection.commit()

    def active_season(self, guild_id: int):
        return self.connection.execute("SELECT * FROM seasons WHERE guild_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (guild_id,)).fetchone()

    def start_season(self, guild_id: int, name: str):
        if self.active_season(guild_id):
            raise ValueError("End the current season before starting a new one")
        cursor = self.connection.execute("INSERT INTO seasons (guild_id, name, started_at) VALUES (?, ?, ?)", (guild_id, name.strip(), datetime.now(timezone.utc).isoformat()))
        self.connection.commit()
        return self.connection.execute("SELECT * FROM seasons WHERE id=?", (cursor.lastrowid,)).fetchone()

    def end_season(self, guild_id: int):
        season = self.active_season(guild_id)
        if not season:
            raise ValueError("There is no active season")
        self.connection.execute("UPDATE seasons SET ended_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), season["id"]))
        self.connection.commit()
        return season

    def create_challenge(self, guild_id: int, mode: str, challenger_id: int, opponent_id: int):
        cursor = self.connection.execute("INSERT INTO challenges (guild_id, mode, challenger_id, opponent_id) VALUES (?, ?, ?, ?)", (guild_id, mode, challenger_id, opponent_id))
        self.connection.commit()
        return cursor.lastrowid

    def update_challenge(self, guild_id: int, challenge_id: int, opponent_id: int, status: str):
        result = self.connection.execute("UPDATE challenges SET status=? WHERE id=? AND guild_id=? AND opponent_id=? AND status='pending'", (status, challenge_id, guild_id, opponent_id))
        self.connection.commit()
        return result.rowcount

    def challenge(self, guild_id: int, challenge_id: int):
        return self.connection.execute("SELECT * FROM challenges WHERE guild_id=? AND id=?", (guild_id, challenge_id)).fetchone()

    def record_match(self, guild_id: int, mode: str, winner: int, team_one: list[int], team_two: list[int], stats: dict[int, dict[str, int]], created_by: int, map_name: str = "Unknown"):
        rated_one = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_one]
        rated_two = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_two]
        changes = calculate_match_changes(mode, rated_one, rated_two, winner, self.elo_settings(guild_id, mode)["k_factor"])
        season = self.active_season(guild_id)
        cursor = self.connection.execute(
            "INSERT INTO matches (guild_id, mode, winner, team_one, team_two, created_by, season_id, map_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by, season["id"] if season else None, (map_name or "Unknown").strip()[:100]),
        )
        match_id = cursor.lastrowid
        for user_id, values in stats.items():
            self.connection.execute(
                "INSERT INTO match_player_stats (match_id, guild_id, user_id, mode, captures, breaks, kills, deaths, assists, damage, score, rating_before, rating_delta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (match_id, guild_id, user_id, mode, values.get("captures", 0), values.get("breaks", 0), values.get("kills", 0), values.get("deaths", 0), values.get("assists", 0), values.get("damage", 0), values.get("score", 0), next(change.old_rating for change in changes if change.user_id == user_id), next(change.delta for change in changes if change.user_id == user_id)),
            )
        team_a, team_b, first_is_a = canonical_matchup(team_one, team_two)
        team_one_values = self._sum_team_stats(team_one, stats)
        team_two_values = self._sum_team_stats(team_two, stats)
        values_a = team_one_values if first_is_a else team_two_values
        values_b = team_two_values if first_is_a else team_one_values
        winner_is_a = (winner == 1) == first_is_a
        columns = ["captures", "breaks", "kills", "deaths", "assists", "damage", "score"]
        insert_columns = ", ".join(["guild_id", "mode", "team_a", "team_b", "games", "team_a_wins", "team_b_wins"] + [f"team_a_{column}" for column in columns] + [f"team_b_{column}" for column in columns])
        placeholders = ", ".join("?" for _ in insert_columns.split(", "))
        values = [guild_id, mode, team_a, team_b, 1, int(winner_is_a), int(not winner_is_a)]
        values.extend(values_a[column] for column in columns)
        values.extend(values_b[column] for column in columns)
        updates = ", ".join(["games=games+1", "team_a_wins=team_a_wins+excluded.team_a_wins", "team_b_wins=team_b_wins+excluded.team_b_wins"] + [f"team_a_{column}=team_a_{column}+excluded.team_a_{column}" for column in columns] + [f"team_b_{column}=team_b_{column}+excluded.team_b_{column}" for column in columns])
        self.connection.execute(f"INSERT INTO team_matchups ({insert_columns}) VALUES ({placeholders}) ON CONFLICT(guild_id, mode, team_a, team_b) DO UPDATE SET {updates}", values)
        self._update_team_performance(guild_id, mode, team_one, team_one_values, winner == 1)
        self._update_team_performance(guild_id, mode, team_two, team_two_values, winner == 2)
        for change in changes:
            did_win = (change.user_id in team_one and winner == 1) or (change.user_id in team_two and winner == 2)
            self.connection.execute(
                """
                INSERT INTO ratings (guild_id, user_id, mode, rating, wins, losses, games, current_streak, best_streak)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(guild_id, user_id, mode) DO UPDATE SET
                    rating=excluded.rating, wins=wins+excluded.wins,
                    losses=losses+excluded.losses, games=games+1,
                    current_streak=CASE WHEN excluded.wins=1 THEN current_streak+1 ELSE 0 END,
                    best_streak=MAX(best_streak, CASE WHEN excluded.wins=1 THEN current_streak+1 ELSE 0 END)
                """,
                (guild_id, change.user_id, mode, change.new_rating, int(did_win), int(not did_win), int(did_win), int(did_win)),
            )
        self.connection.commit()
        return changes

    def undo_latest_match(self, guild_id: int):
        match = self.connection.execute("SELECT * FROM matches WHERE guild_id=? ORDER BY id DESC LIMIT 1", (guild_id,)).fetchone()
        if not match:
            raise ValueError("There are no matches to undo")
        stats = self.connection.execute("SELECT * FROM match_player_stats WHERE match_id=?", (match["id"],)).fetchall()
        if not stats or any(row["rating_delta"] == 0 for row in stats):
            raise ValueError("This match predates undo support and cannot be safely reversed")
        team_one = list(map(int, match["team_one"].split(",")))
        team_two = list(map(int, match["team_two"].split(",")))
        stat_map = {row["user_id"]: dict(row) for row in stats}
        team_a, team_b, first_is_a = canonical_matchup(team_one, team_two)
        values_one = self._sum_team_stats(team_one, stat_map)
        values_two = self._sum_team_stats(team_two, stat_map)
        values_a = values_one if first_is_a else values_two
        values_b = values_two if first_is_a else values_one
        winner_is_a = (match["winner"] == 1) == first_is_a
        columns = ["captures", "breaks", "kills", "deaths", "assists", "damage", "score"]
        try:
            for row in stats:
                won = (row["user_id"] in team_one and match["winner"] == 1) or (row["user_id"] in team_two and match["winner"] == 2)
                self.connection.execute(
                    "UPDATE ratings SET rating=rating-?, wins=wins-?, losses=losses-?, games=games-1, current_streak=MAX(0, current_streak-?) WHERE guild_id=? AND user_id=? AND mode=?",
                    (row["rating_delta"], int(won), int(not won), int(won), guild_id, row["user_id"], match["mode"]),
                )
                self.connection.execute("DELETE FROM ratings WHERE guild_id=? AND user_id=? AND mode=? AND games<=0", (guild_id, row["user_id"], match["mode"]))
            self.connection.execute("DELETE FROM team_matchups WHERE guild_id=? AND mode=? AND team_a=? AND team_b=? AND games<=1", (guild_id, match["mode"], team_a, team_b))
            if self.connection.execute("SELECT changes()").fetchone()[0] == 0:
                updates = ["games=games-1", "team_a_wins=team_a_wins-?", "team_b_wins=team_b_wins-?"]
                params: list[int] = [int(winner_is_a), int(not winner_is_a)]
                for column in columns:
                    updates.append(f"team_a_{column}=team_a_{column}-?")
                    params.append(values_a[column])
                for column in columns:
                    updates.append(f"team_b_{column}=team_b_{column}-?")
                    params.append(values_b[column])
                params.extend([guild_id, match["mode"], team_a, team_b])
                self.connection.execute(f"UPDATE team_matchups SET {', '.join(updates)} WHERE guild_id=? AND mode=? AND team_a=? AND team_b=?", params)
            self._revert_team_performance(guild_id, match["mode"], team_one, values_one, match["winner"] == 1)
            self._revert_team_performance(guild_id, match["mode"], team_two, values_two, match["winner"] == 2)
            self.connection.execute("DELETE FROM matches WHERE id=?", (match["id"],))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return match

    @staticmethod
    def _sum_team_stats(player_ids: list[int], stats: dict[int, dict[str, int]]) -> dict[str, int]:
        return {column: sum(stats[player_id].get(column, 0) for player_id in player_ids) for column in ("captures", "breaks", "kills", "deaths", "assists", "damage", "score")}

    def _update_team_performance(self, guild_id: int, mode: str, player_ids: list[int], values: dict[str, int], won: bool):
        columns = ["captures", "breaks", "kills", "deaths", "assists", "damage", "score"]
        insert_columns = ", ".join(["guild_id", "mode", "team_key", "games", "wins", "losses"] + columns)
        placeholders = ", ".join("?" for _ in insert_columns.split(", "))
        params = [guild_id, mode, team_key(player_ids), 1, int(won), int(not won)] + [values[column] for column in columns]
        updates = ", ".join(["games=games+1", "wins=wins+excluded.wins", "losses=losses+excluded.losses"] + [f"{column}={column}+excluded.{column}" for column in columns])
        self.connection.execute(f"INSERT INTO team_performance ({insert_columns}) VALUES ({placeholders}) ON CONFLICT(guild_id, mode, team_key) DO UPDATE SET {updates}", params)

    def _revert_team_performance(self, guild_id: int, mode: str, player_ids: list[int], values: dict[str, int], won: bool):
        key = team_key(player_ids)
        deleted = self.connection.execute("DELETE FROM team_performance WHERE guild_id=? AND mode=? AND team_key=? AND games<=1", (guild_id, mode, key)).rowcount
        if deleted:
            return
        columns = ["captures", "breaks", "kills", "deaths", "assists", "damage", "score"]
        updates = ["games=games-1", "wins=wins-?", "losses=losses-?"]
        params: list[int] = [int(won), int(not won)]
        for column in columns:
            updates.append(f"{column}={column}-?")
            params.append(values[column])
        params.extend([guild_id, mode, key])
        self.connection.execute(f"UPDATE team_performance SET {', '.join(updates)} WHERE guild_id=? AND mode=? AND team_key=?", params)

    def matchup_stats(self, guild_id: int, mode: str, team_one: list[int], team_two: list[int]):
        team_a, team_b, first_is_a = canonical_matchup(team_one, team_two)
        row = self.connection.execute(
            "SELECT * FROM team_matchups WHERE guild_id=? AND mode=? AND team_a=? AND team_b=?",
            (guild_id, mode, team_a, team_b),
        ).fetchone()
        return row, first_is_a

    def team_chemistry(self, guild_id: int, mode: str, player_ids: list[int]):
        return self.connection.execute("SELECT * FROM team_performance WHERE guild_id=? AND mode=? AND team_key=?", (guild_id, mode, team_key(player_ids))).fetchone()

    def player_stat_summary(self, guild_id: int, user_id: int, mode: str):
        return self.connection.execute(
            """
            SELECT COUNT(*) AS matches, SUM(captures) AS captures, SUM(breaks) AS breaks,
                   SUM(kills) AS kills, SUM(deaths) AS deaths, SUM(assists) AS assists,
                   SUM(damage) AS damage,
                   SUM(score) AS score
            FROM match_player_stats WHERE guild_id=? AND user_id=? AND mode=?
            """,
            (guild_id, user_id, mode),
        ).fetchone()

    def leaderboard(self, guild_id: int, mode: str, limit: int = 10):
        return self.connection.execute(
            "SELECT user_id, rating, wins, losses, games FROM ratings WHERE guild_id=? AND mode=? ORDER BY rating DESC, wins DESC LIMIT ?",
            (guild_id, mode, limit),
        ).fetchall()

    def streak_leaderboard(self, guild_id: int, mode: str, limit: int = 10):
        return self.connection.execute(
            "SELECT user_id, current_streak, best_streak FROM ratings WHERE guild_id=? AND mode=? AND current_streak>0 ORDER BY current_streak DESC, best_streak DESC LIMIT ?",
            (guild_id, mode, limit),
        ).fetchall()

    def map_stats(self, guild_id: int, mode: str):
        return self.connection.execute(
            "SELECT map_name, COUNT(*) AS games, SUM(winner=1) AS team_one_wins, SUM(winner=2) AS team_two_wins FROM matches WHERE guild_id=? AND mode=? GROUP BY map_name ORDER BY games DESC, map_name",
            (guild_id, mode),
        ).fetchall()

    def match_history(self, guild_id: int, mode: str | None = None, limit: int = 10):
        limit = max(1, min(limit, 20))
        if mode:
            return self.connection.execute("SELECT m.*, s.name AS season_name FROM matches m LEFT JOIN seasons s ON s.id=m.season_id WHERE m.guild_id=? AND m.mode=? ORDER BY m.id DESC LIMIT ?", (guild_id, mode, limit)).fetchall()
        return self.connection.execute("SELECT m.*, s.name AS season_name FROM matches m LEFT JOIN seasons s ON s.id=m.season_id WHERE m.guild_id=? ORDER BY m.id DESC LIMIT ?", (guild_id, limit)).fetchall()

    def player_stats(self, guild_id: int, user_id: int):
        return self.connection.execute(
            "SELECT mode, rating, wins, losses, games FROM ratings WHERE guild_id=? AND user_id=? ORDER BY rating DESC",
            (guild_id, user_id),
        ).fetchall()

    def rating_history(self, guild_id: int, user_id: int, mode: str, limit: int = 50):
        limit = max(1, min(limit, 100))
        return self.connection.execute(
            """
            SELECT m.id, m.created_at, m.winner, s.rating_before, s.rating_delta,
                   s.kills, s.deaths, s.assists, s.damage, s.score
            FROM match_player_stats s
            JOIN matches m ON m.id=s.match_id
            WHERE s.guild_id=? AND s.user_id=? AND s.mode=?
            ORDER BY m.id DESC LIMIT ?
            """,
            (guild_id, user_id, mode, limit),
        ).fetchall()[::-1]

    def profile_rows(self, guild_id: int, user_id: int):
        return self.connection.execute(
            """
            SELECT r.mode, r.rating, r.wins, r.losses, r.games,
                   COALESCE(SUM(s.kills), 0) AS kills,
                   COALESCE(SUM(s.deaths), 0) AS deaths,
                   COALESCE(SUM(s.damage), 0) AS damage
            FROM ratings r LEFT JOIN match_player_stats s
              ON s.guild_id=r.guild_id AND s.user_id=r.user_id AND s.mode=r.mode
            WHERE r.guild_id=? AND r.user_id=?
            GROUP BY r.mode, r.rating, r.wins, r.losses, r.games
            ORDER BY r.games DESC, r.rating DESC
            """,
            (guild_id, user_id),
        ).fetchall()

    def achievement_rows(self, guild_id: int, user_id: int):
        return self.connection.execute(
            """
            SELECT r.mode, r.games, r.wins, r.losses, r.current_streak, r.best_streak,
                   COALESCE(SUM(s.kills), 0) AS kills, COALESCE(SUM(s.damage), 0) AS damage,
                   COALESCE(SUM(s.captures), 0) AS captures
            FROM ratings r LEFT JOIN match_player_stats s
              ON s.guild_id=r.guild_id AND s.user_id=r.user_id AND s.mode=r.mode
            WHERE r.guild_id=? AND r.user_id=?
            GROUP BY r.mode, r.games, r.wins, r.losses, r.current_streak, r.best_streak
            """,
            (guild_id, user_id),
        ).fetchall()


class GearsEloBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # This bot uses application (slash) commands only. Keeping the prefix
        # disabled avoids requiring Discord's privileged Message Content intent.
        super().__init__(command_prefix=None, intents=intents)
        self.database = EloDatabase(DATABASE_PATH)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Slash commands synced to server {GUILD_ID}.")
        else:
            await self.tree.sync()

    async def close(self):
        self.database.close()
        await super().close()


bot = GearsEloBot()

mode_choices = [app_commands.Choice(name=str(info["label"]), value=mode) for mode, info in MODES.items()]
queues: dict[tuple[int, str], list[int]] = {}


def elo_tier(rating: int):
    tier = ELO_TIERS[0]
    for candidate in ELO_TIERS:
        if rating >= candidate[0]:
            tier = candidate
    return tier


async def update_elo_role(guild: discord.Guild, user_id: int, mode: str, rating: int) -> bool:
    member = guild.get_member(user_id)
    if not member:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return False
    prefix = f"Gears Elo • {mode_label(mode)} • "
    target = discord.utils.get(guild.roles, name=prefix + elo_tier(rating)[1])
    if not target:
        return False
    try:
        old_roles = [role for role in member.roles if role.name.startswith(prefix) and role != target]
        if old_roles:
            await member.remove_roles(*old_roles, reason="Update Gears Elo tier")
        if target not in member.roles:
            await member.add_roles(target, reason="Update Gears Elo tier")
    except (discord.Forbidden, discord.HTTPException):
        return False
    return True


class PlayerStatsModal(discord.ui.Modal):
    def __init__(self, mode: str, winner: int, team_one: list[int], team_two: list[int], player_ids: list[int], stats: dict[int, dict[str, int]], index: int, map_name: str):
        self.mode = mode
        self.winner = winner
        self.team_one = team_one
        self.team_two = team_two
        self.player_ids = player_ids
        self.stats = stats
        self.index = index
        self.map_name = map_name
        player_id = player_ids[index]
        player_name = bot.get_user(player_id)
        name = player_name.display_name if player_name else str(player_id)
        super().__init__(title=f"Stats: {name}"[:45], timeout=600)
        self.stat_input = discord.ui.TextInput(
            label=f"Enter stats for {name}"[:45],
            placeholder=("kills=15 deaths=8 assists=4 damage=500 score=250" if mode == "gnashers_2v2" else "kills=15 deaths=8 damage=500 score=250") if mode.startswith("gnashers_") else "captures=3 breaks=5 kills=15 deaths=8 assists=7 damage=500 score=250",
            style=discord.TextStyle.short,
            required=True,
            max_length=500,
        )
        self.add_item(self.stat_input)

    async def on_submit(self, interaction: discord.Interaction):
        player_id = self.player_ids[self.index]
        try:
            self.stats[player_id] = parse_player_stats(str(self.stat_input.value), self.mode)
        except ValueError as error:
            await interaction.response.send_message(f"Invalid stats for <@{player_id}>: {error}. Start `/match` again to retry.", ephemeral=True)
            return

        next_index = self.index + 1
        if next_index < len(self.player_ids):
            await interaction.response.send_modal(PlayerStatsModal(self.mode, self.winner, self.team_one, self.team_two, self.player_ids, self.stats, next_index, self.map_name))
            return

        try:
            changes = bot.database.record_match(interaction.guild_id, self.mode, self.winner, self.team_one, self.team_two, self.stats, interaction.user.id, self.map_name)
        except sqlite3.Error as error:
            await interaction.response.send_message(f"Could not record match: {error}", ephemeral=True)
            return
        change_text = " · ".join(f"<@{change.user_id}> {change.new_rating} ({change.delta:+d})" for change in changes)
        mvp_id, mvp_stats = max(self.stats.items(), key=lambda item: (item[1].get("score", 0), item[1].get("kills", 0), item[1].get("damage", 0)))
        team_one_score = sum(self.stats[player_id].get("score", 0) for player_id in self.team_one)
        team_two_score = sum(self.stats[player_id].get("score", 0) for player_id in self.team_two)
        role_updates = 0
        if interaction.guild:
            for change in changes:
                if await update_elo_role(interaction.guild, change.user_id, self.mode, change.new_rating):
                    role_updates += 1
        role_text = f" Tier roles updated for {role_updates} players." if role_updates else ""
        await interaction.response.send_message(f"**{mode_label(self.mode)} recorded** — Team {self.winner} wins\n🏅 MVP: <@{mvp_id}> ({mvp_stats.get('score', 0)} score, {mvp_stats.get('kills', 0)} kills)\n📊 Team scores: **{team_one_score}** — **{team_two_score}**\n{change_text}\nStats saved for {len(self.stats)} players.{role_text}")


@bot.tree.command(name="modes", description="Show the Gears 5 modes tracked by this bot")
async def modes(interaction: discord.Interaction):
    lines = [f"• {mode_label(mode)} — {team_size(mode)}v{team_size(mode)}" for mode in MODES]
    await interaction.response.send_message("**Tracked modes**\n" + "\n".join(lines))


@bot.tree.command(name="settings", description="Show Elo settings for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def settings(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    row = bot.database.elo_settings(interaction.guild_id, mode.value)
    await interaction.response.send_message(f"**{mode_label(mode.value)} Elo settings**\nStarting rating: **{row['starting_rating']}**\nK-factor: **{row['k_factor']}**")


@bot.tree.command(name="setelo", description="Set starting rating and K-factor for a mode")
@app_commands.describe(mode="Game mode", starting_rating="Starting rating for new players", k_factor="How quickly ratings move")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(manage_guild=True)
async def setelo(interaction: discord.Interaction, mode: app_commands.Choice[str], starting_rating: int, k_factor: int):
    if not 100 <= starting_rating <= 5000 or not 1 <= k_factor <= 100:
        await interaction.response.send_message("Starting rating must be 100–5000 and K-factor must be 1–100.", ephemeral=True)
        return
    bot.database.set_elo_settings(interaction.guild_id, mode.value, starting_rating, k_factor)
    await interaction.response.send_message(f"Updated **{mode_label(mode.value)}**: starting rating **{starting_rating}**, K-factor **{k_factor}**.")


@bot.tree.command(name="roles_setup", description="Create Elo tier roles for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(manage_guild=True)
async def roles_setup(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("This command must be used inside a server.", ephemeral=True)
        return
    me = interaction.guild.me
    if not me or not me.guild_permissions.manage_roles:
        await interaction.response.send_message("I need the Manage Roles permission before I can create or assign Elo roles.", ephemeral=True)
        return
    prefix = f"Gears Elo • {mode_label(mode.value)} • "
    created = []
    existing = []
    for _, tier_name, colour in ELO_TIERS:
        name = prefix + tier_name
        if discord.utils.get(interaction.guild.roles, name=name):
            existing.append(tier_name)
            continue
        try:
            await interaction.guild.create_role(name=name, colour=colour, reason="Set up Gears Elo tier rewards")
            created.append(tier_name)
        except (discord.Forbidden, discord.HTTPException) as error:
            await interaction.response.send_message(f"Could not create Elo roles: {error}", ephemeral=True)
            return
    created_text = ", ".join(created) or "none"
    existing_text = ", ".join(existing) or "none"
    await interaction.response.send_message(f"**{mode_label(mode.value)} tier roles ready.**\nCreated: {created_text}\nAlready existed: {existing_text}\nThe bot will assign them after each `/match`. Make sure the bot's highest role is above these roles.")


@bot.tree.command(name="balance", description="Create balanced teams from a player list")
@app_commands.describe(mode="Game mode", players="Comma-separated player mentions or IDs")
@app_commands.choices(mode=mode_choices)
async def balance(interaction: discord.Interaction, mode: app_commands.Choice[str], players: str):
    try:
        player_ids = parse_player_list(players, team_size(mode.value) * 2, team_size(mode.value) * 2)
        balanced_one = [(player_id, bot.database.get_rating(interaction.guild_id, player_id, mode.value)) for player_id in player_ids]
        team_one, team_two = balance_teams(balanced_one)
    except ValueError as error:
        await interaction.response.send_message(f"Could not balance teams: {error}", ephemeral=True)
        return
    average_one = sum(bot.database.get_rating(interaction.guild_id, player_id, mode.value) for player_id in team_one) / len(team_one)
    average_two = sum(bot.database.get_rating(interaction.guild_id, player_id, mode.value) for player_id in team_two) / len(team_two)
    await interaction.response.send_message(f"**Balanced {mode_label(mode.value)} teams**\nTeam 1: {' + '.join(f'<@{player_id}>' for player_id in team_one)}\nTeam 2: {' + '.join(f'<@{player_id}>' for player_id in team_two)}\nAverage Elo: **{average_one:.0f}** vs **{average_two:.0f}**")


@bot.tree.command(name="queue_join", description="Join the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_join(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    key = (interaction.guild_id, mode.value)
    queue = queues.setdefault(key, [])
    if interaction.user.id in queue:
        await interaction.response.send_message("You are already in that queue.", ephemeral=True)
        return
    queue.append(interaction.user.id)
    needed = team_size(mode.value) * 2
    if len(queue) < needed:
        await interaction.response.send_message(f"<@{interaction.user.id}> joined **{mode_label(mode.value)}** queue ({len(queue)}/{needed}).")
        return
    players = queue[:needed]
    del queue[:needed]
    rated = [(player_id, bot.database.get_rating(interaction.guild_id, player_id, mode.value)) for player_id in players]
    team_one, team_two = balance_teams(rated)
    await interaction.response.send_message(f"**{mode_label(mode.value)} lobby ready!**\nTeam 1: {' + '.join(f'<@{player_id}>' for player_id in team_one)}\nTeam 2: {' + '.join(f'<@{player_id}>' for player_id in team_two)}\nUse `/match` to record the result.")


@bot.tree.command(name="queue_leave", description="Leave the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_leave(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    queue = queues.get((interaction.guild_id, mode.value), [])
    if interaction.user.id not in queue:
        await interaction.response.send_message("You are not in that queue.", ephemeral=True)
        return
    queue.remove(interaction.user.id)
    await interaction.response.send_message(f"<@{interaction.user.id}> left **{mode_label(mode.value)}** queue.")


@bot.tree.command(name="queue", description="Show the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_status(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    queue = queues.get((interaction.guild_id, mode.value), [])
    needed = team_size(mode.value) * 2
    names = ", ".join(f"<@{player_id}>" for player_id in queue) or "Nobody"
    await interaction.response.send_message(f"**{mode_label(mode.value)} queue** ({len(queue)}/{needed})\n{names}")


@bot.tree.command(name="season", description="Show the active season")
async def season(interaction: discord.Interaction):
    active = bot.database.active_season(interaction.guild_id)
    if not active:
        await interaction.response.send_message("There is no active season. A manager can use `/season_start` to begin one.")
        return
    await interaction.response.send_message(f"**Active season:** {active['name']}\nStarted: {active['started_at'][:10]}\nNew matches are being recorded in this season.")


@bot.tree.command(name="season_start", description="Start a named season")
@app_commands.describe(name="Season name, such as Season 1")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_start(interaction: discord.Interaction, name: str):
    try:
        active = bot.database.start_season(interaction.guild_id, name)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(f"Started **{active['name']}**. Future matches will be tagged to this season.")


@bot.tree.command(name="season_end", description="End the active season")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_end(interaction: discord.Interaction):
    try:
        ended = bot.database.end_season(interaction.guild_id)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(f"Ended **{ended['name']}**. Start another season with `/season_start` when ready.")


@bot.tree.command(name="challenge", description="Challenge another player to a 1v1 match")
@app_commands.describe(mode="1v1 game mode", opponent="Player to challenge")
@app_commands.choices(mode=[choice for choice in mode_choices if team_size(choice.value) == 1])
async def challenge(interaction: discord.Interaction, mode: app_commands.Choice[str], opponent: discord.Member):
    if opponent.id == interaction.user.id or opponent.bot:
        await interaction.response.send_message("Choose another human player.", ephemeral=True)
        return
    challenge_id = bot.database.create_challenge(interaction.guild_id, mode.value, interaction.user.id, opponent.id)
    await interaction.response.send_message(f"⚔️ <@{interaction.user.id}> challenged <@{opponent.id}> to **{mode_label(mode.value)}** (challenge **#{challenge_id}**). Use `/challenge_accept challenge_id:{challenge_id}` to accept.")


@bot.tree.command(name="challenge_accept", description="Accept a pending challenge")
@app_commands.describe(challenge_id="Challenge number")
async def challenge_accept(interaction: discord.Interaction, challenge_id: int):
    row = bot.database.challenge(interaction.guild_id, challenge_id)
    if not row or row["opponent_id"] != interaction.user.id:
        await interaction.response.send_message("That challenge was not found for you.", ephemeral=True)
        return
    if bot.database.update_challenge(interaction.guild_id, challenge_id, interaction.user.id, "accepted") == 0:
        await interaction.response.send_message("That challenge is no longer pending.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Challenge **#{challenge_id}** accepted. Play the match, then record it with `/match`.")


@bot.tree.command(name="challenge_decline", description="Decline a pending challenge")
@app_commands.describe(challenge_id="Challenge number")
async def challenge_decline(interaction: discord.Interaction, challenge_id: int):
    row = bot.database.challenge(interaction.guild_id, challenge_id)
    if not row or row["opponent_id"] != interaction.user.id:
        await interaction.response.send_message("That challenge was not found for you.", ephemeral=True)
        return
    bot.database.update_challenge(interaction.guild_id, challenge_id, interaction.user.id, "declined")
    await interaction.response.send_message(f"Challenge **#{challenge_id}** declined.")


@bot.tree.command(name="match", description="Record a completed private Gears 5 match")
@app_commands.describe(mode="Game mode", winner="Which team won", team_one="Comma-separated mentions/IDs", team_two="Comma-separated mentions/IDs", map_name="Optional map name")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def match(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: app_commands.Choice[str], team_one: str, team_two: str, map_name: str | None = None):
    try:
        size = team_size(mode.value)
        first = parse_team(team_one, size)
        second = parse_team(team_two, size)
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
        player_ids = first + second
        await interaction.response.send_modal(PlayerStatsModal(mode.value, int(winner.value), first, second, player_ids, {}, 0, map_name or "Unknown"))
        return
    except (ValueError, sqlite3.Error) as error:
        await interaction.response.send_message(f"Could not record match: {error}", ephemeral=True)
        return


@bot.tree.command(name="leaderboard", description="Show the top ratings for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def leaderboard(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.leaderboard(interaction.guild_id, mode.value)
    if not rows:
        await interaction.response.send_message(f"No matches have been recorded for **{mode_label(mode.value)}** yet.")
        return
    lines = [f"{index}. <@{row['user_id']}> — **{row['rating']} Elo** · {row['wins']}-{row['losses']} · {row['wins'] / row['games'] * 100:.0f}% win rate · {row['games']} games" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message(f"**{mode_label(mode.value)} leaderboard**\n" + "\n".join(lines))


@bot.tree.command(name="streaks", description="Show the current win-streak leaders for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def streaks(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.streak_leaderboard(interaction.guild_id, mode.value)
    if not rows:
        await interaction.response.send_message(f"No active win streaks in **{mode_label(mode.value)}**.")
        return
    lines = [f"{index}. <@{row['user_id']}> — **{row['current_streak']}** straight wins (best: {row['best_streak']})" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message(f"**{mode_label(mode.value)} streak leaders**\n" + "\n".join(lines))


@bot.tree.command(name="rating", description="Show a player's ratings across all modes")
@app_commands.describe(player="Optional player; defaults to you")
async def rating(interaction: discord.Interaction, player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.player_stats(interaction.guild_id, member.id)
    if not rows:
        await interaction.response.send_message(f"<@{member.id}> has no recorded matches yet.")
        return
    lines = [f"{mode_label(row['mode'])}: **{row['rating']}** ({row['wins']}-{row['losses']})" for row in rows]
    await interaction.response.send_message(f"**{member.display_name}'s ratings**\n" + "\n".join(lines))


@bot.tree.command(name="trend", description="Show a player's Elo and performance trend")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you", metric="Performance metric to chart")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(metric=[
    app_commands.Choice(name="Damage", value="damage"),
    app_commands.Choice(name="Kills", value="kills"),
    app_commands.Choice(name="Score", value="score"),
])
async def trend(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None, metric: app_commands.Choice[str] | None = None):
    member = player or interaction.user
    rows = bot.database.rating_history(interaction.guild_id, member.id, mode.value)
    if not rows:
        await interaction.response.send_message(f"<@{member.id}> has no recorded matches for **{mode_label(mode.value)}** yet.", ephemeral=True)
        return

    metric_name = metric.value if metric else "damage"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        await interaction.response.send_message("Charts are unavailable because the plotting dependency is not installed. Run `python -m pip install -r requirements.txt` and restart the bot.", ephemeral=True)
        return

    match_numbers = list(range(1, len(rows) + 1))
    ratings = [row["rating_before"] + row["rating_delta"] for row in rows]
    performance = [row[metric_name] for row in rows]
    figure, axis = plt.subplots(figsize=(8, 4.5))
    axis.plot(match_numbers, ratings, marker="o", color="#d7263d", linewidth=2, label="Elo")
    axis.set_xlabel("Match number")
    axis.set_ylabel("Elo", color="#d7263d")
    axis.grid(alpha=0.25)
    performance_axis = axis.twinx()
    performance_axis.plot(match_numbers, performance, marker="s", color="#1b998b", linewidth=2, label=metric_name.title())
    performance_axis.set_ylabel(metric_name.title(), color="#1b998b")
    figure.suptitle(f"{member.display_name} — {mode_label(mode.value)}")
    figure.tight_layout()
    image = BytesIO()
    figure.savefig(image, format="png", dpi=140)
    plt.close(figure)
    image.seek(0)
    await interaction.response.send_message(
        f"**{member.display_name} — {mode_label(mode.value)} trend**\nShowing {len(rows)} matches. Elo: **{ratings[-1]}** · Average {metric_name}: **{sum(performance) / len(performance):.1f}**",
        file=discord.File(image, filename="gears-elo-trend.png"),
    )


@bot.tree.command(name="profile", description="Show a complete player profile")
@app_commands.describe(player="Optional player; defaults to you")
async def profile(interaction: discord.Interaction, player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.profile_rows(interaction.guild_id, member.id)
    if not rows:
        await interaction.response.send_message(f"<@{member.id}> has no recorded matches yet.")
        return
    favorite = rows[0]
    lines = []
    for row in rows:
        win_rate = row["wins"] / row["games"] * 100 if row["games"] else 0
        kd = row["kills"] / row["deaths"] if row["deaths"] else float(row["kills"])
        avg_damage = row["damage"] / row["games"] if row["games"] else 0
        lines.append(f"{mode_label(row['mode'])}: **{row['rating']} Elo** · {row['wins']}-{row['losses']} · {win_rate:.0f}% wins · K/D {kd:.2f} · {avg_damage:.0f} avg damage")
    await interaction.response.send_message(f"**{member.display_name}'s profile**\nFavorite mode: **{mode_label(favorite['mode'])}**\n" + "\n".join(lines))


@bot.tree.command(name="achievements", description="Show a player's earned badges")
@app_commands.describe(player="Optional player; defaults to you")
async def achievements(interaction: discord.Interaction, player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.achievement_rows(interaction.guild_id, member.id)
    badges: list[str] = []
    for row in rows:
        mode = mode_label(row["mode"])
        if row["games"] >= 1:
            badges.append(f"🎮 First Match — {mode}")
        if row["games"] >= 10:
            badges.append(f"🏆 Veteran — {mode}")
        if row["wins"] > row["losses"]:
            badges.append(f"📈 Winning Record — {mode}")
        if row["best_streak"] >= 5:
            badges.append(f"🔥 Unstoppable — {mode}")
        if row["kills"] >= 100:
            badges.append(f"💀 Slayer — {mode}")
        if row["damage"] >= 10000:
            badges.append(f"💥 Damage Dealer — {mode}")
        if row["mode"].startswith("control_") and row["captures"] >= 25:
            badges.append(f"🚩 Objective Player — {mode}")
    if not badges:
        await interaction.response.send_message(f"<@{member.id}> has no achievements yet. Play a match to get started.")
        return
    await interaction.response.send_message(f"**{member.display_name}'s achievements**\n" + "\n".join(dict.fromkeys(badges)))


@bot.tree.command(name="stats", description="Show a player's match-stat totals and averages")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def stats(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    row = bot.database.player_stat_summary(interaction.guild_id, member.id, mode.value)
    if not row or not row["matches"]:
        await interaction.response.send_message(f"<@{member.id}> has no recorded stats for **{mode_label(mode.value)}** yet.")
        return
    matches_played = row["matches"]
    names = stat_names(mode.value)
    totals = " · ".join(f"{name.title()}: **{row[name]}**" for name in names)
    averages = " · ".join(f"{name.title()}: **{row[name] / matches_played:.1f}**" for name in names)
    await interaction.response.send_message(f"**{member.display_name} — {mode_label(mode.value)} stats**\nMatches: **{matches_played}**\nTotals — {totals}\nAverages — {averages}")


@bot.tree.command(name="teamstats", description="Show the head-to-head record for two exact teams")
@app_commands.describe(mode="Game mode", team_one="Comma-separated mentions/IDs", team_two="Comma-separated mentions/IDs")
@app_commands.choices(mode=mode_choices)
async def teamstats(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str):
    try:
        size = team_size(mode.value)
        first = parse_team(team_one, size)
        second = parse_team(team_two, size)
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
    except ValueError as error:
        await interaction.response.send_message(f"Could not read teams: {error}", ephemeral=True)
        return
    row, first_is_a = bot.database.matchup_stats(interaction.guild_id, mode.value, first, second)
    if not row:
        await interaction.response.send_message(f"No recorded matches yet for this exact matchup in **{mode_label(mode.value)}**.")
        return
    first_wins = row["team_a_wins"] if first_is_a else row["team_b_wins"]
    second_wins = row["team_b_wins"] if first_is_a else row["team_a_wins"]
    first_name = " + ".join(f"<@{player_id}>" for player_id in first)
    second_name = " + ".join(f"<@{player_id}>" for player_id in second)
    first_prefix = "team_a_" if first_is_a else "team_b_"
    second_prefix = "team_b_" if first_is_a else "team_a_"
    first_totals = " · ".join(f"{name.title()}: **{row[first_prefix + name]}**" for name in stat_names(mode.value))
    second_totals = " · ".join(f"{name.title()}: **{row[second_prefix + name]}**" for name in stat_names(mode.value))
    await interaction.response.send_message(
        f"**{mode_label(mode.value)} team matchup**\n{first_name}: **{first_wins} wins**\n{second_name}: **{second_wins} wins**\nGames: **{row['games']}**\n"
        f"{first_name} totals — {first_totals}\n{second_name} totals — {second_totals}"
    )


@bot.tree.command(name="chemistry", description="Show an exact team's overall chemistry")
@app_commands.describe(mode="Game mode", team="Comma-separated mentions/IDs for the team")
@app_commands.choices(mode=mode_choices)
async def chemistry(interaction: discord.Interaction, mode: app_commands.Choice[str], team: str):
    try:
        players = parse_team(team, team_size(mode.value))
    except ValueError as error:
        await interaction.response.send_message(f"Could not read team: {error}", ephemeral=True)
        return
    row = bot.database.team_chemistry(interaction.guild_id, mode.value, players)
    if not row:
        await interaction.response.send_message(f"No matches recorded for this roster in **{mode_label(mode.value)}** yet.")
        return
    roster = " + ".join(f"<@{player_id}>" for player_id in players)
    win_rate = row["wins"] / row["games"] * 100
    totals = " · ".join(f"{name.title()}: **{row[name]}**" for name in stat_names(mode.value))
    await interaction.response.send_message(f"**Team chemistry — {mode_label(mode.value)}**\n{roster}\nRecord: **{row['wins']}-{row['losses']}** · Chemistry: **{win_rate:.0f}%** · Games: **{row['games']}**\nTeam totals — {totals}")


@bot.tree.command(name="mapstats", description="Show match counts and wins by map")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def mapstats(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.map_stats(interaction.guild_id, mode.value)
    if not rows:
        await interaction.response.send_message(f"No map data recorded for **{mode_label(mode.value)}** yet.")
        return
    lines = [f"**{row['map_name']}** — {row['games']} games · Team 1: {row['team_one_wins']} wins · Team 2: {row['team_two_wins']} wins" for row in rows]
    await interaction.response.send_message(f"**{mode_label(mode.value)} map stats**\n" + "\n".join(lines))


@bot.tree.command(name="history", description="Show recent recorded matches")
@app_commands.describe(mode="Optional game mode", limit="Number of matches, from 1 to 20")
@app_commands.choices(mode=mode_choices)
async def history(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None, limit: int = 10):
    rows = bot.database.match_history(interaction.guild_id, mode.value if mode else None, limit)
    if not rows:
        await interaction.response.send_message("No recorded matches found.")
        return
    lines = []
    for row in rows:
        first = " + ".join(f"<@{user_id}>" for user_id in row["team_one"].split(","))
        second = " + ".join(f"<@{user_id}>" for user_id in row["team_two"].split(","))
        season_text = f" · {row['season_name']}" if row["season_name"] else ""
        lines.append(f"**#{row['id']} {mode_label(row['mode'])}** · {row['map_name']} · Team {row['winner']} won{season_text}\n{first} vs {second}")
    await interaction.response.send_message("**Recent match history**\n" + "\n".join(lines))


@bot.tree.command(name="undo", description="Undo the latest match in this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def undo(interaction: discord.Interaction):
    try:
        removed = bot.database.undo_latest_match(interaction.guild_id)
    except (ValueError, sqlite3.Error) as error:
        await interaction.response.send_message(f"Could not undo match: {error}", ephemeral=True)
        return
    await interaction.response.send_message(f"Undid match **#{removed['id']}** ({mode_label(removed['mode'])}). Re-enter it with `/match` if needed.")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    bot.run(TOKEN)
