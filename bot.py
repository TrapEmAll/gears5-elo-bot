from __future__ import annotations

import os
import json
import shutil
import sqlite3
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from elo import MODES, balance_teams, calculate_match_changes, canonical_matchup, mode_label, parse_player_list, parse_player_stats, parse_team, stat_names, team_size

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "gears5_elo.sqlite3"))
BACKUP_DIRECTORY = DATABASE_PATH.parent / f"{DATABASE_PATH.stem}_backups"
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
            CREATE TABLE IF NOT EXISTS captains (
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, mode, team)
            );
            CREATE TABLE IF NOT EXISTS pending_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                winner INTEGER NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                map_name TEXT NOT NULL DEFAULT 'Unknown',
                confirmed_by TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS availability (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS scheduled_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                target_wins INTEGER NOT NULL,
                team_one_wins INTEGER NOT NULL DEFAULT 0,
                team_two_wins INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS veto_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                maps TEXT NOT NULL,
                banned TEXT NOT NULL DEFAULT '[]',
                picked TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS match_votes (
                match_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (match_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS player_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS team_presets (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                players TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                PRIMARY KEY (guild_id, name)
            );
            CREATE TABLE IF NOT EXISTS match_annotations (
                match_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                replay_url TEXT NOT NULL DEFAULT '',
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS command_roles (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, command_name)
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

    def reset_ratings(self, guild_id: int):
        rows = self.connection.execute("SELECT user_id, mode FROM ratings WHERE guild_id=?", (guild_id,)).fetchall()
        for row in rows:
            starting_rating = self.elo_settings(guild_id, row["mode"])["starting_rating"]
            self.connection.execute("UPDATE ratings SET rating=?, current_streak=0 WHERE guild_id=? AND user_id=? AND mode=?", (starting_rating, guild_id, row["user_id"], row["mode"]))
        self.connection.commit()

    def team_leaderboard(self, guild_id: int, mode: str, limit: int = 10):
        return self.connection.execute("SELECT team_key, games, wins, losses, score, kills, damage FROM team_performance WHERE guild_id=? AND mode=? ORDER BY wins DESC, games DESC, team_key LIMIT ?", (guild_id, mode, limit)).fetchall()

    def search_players(self, guild_id: int, query: str, limit: int = 15):
        pattern = f"%{query.lower()}%"
        return self.connection.execute("SELECT DISTINCT user_id FROM ratings WHERE guild_id=? AND CAST(user_id AS TEXT) LIKE ? LIMIT ?", (guild_id, pattern, limit)).fetchall()

    def opponent_records(self, guild_id: int, mode: str, user_id: int):
        rows = self.connection.execute("SELECT winner, team_one, team_two FROM matches WHERE guild_id=? AND mode=? AND (instr(','||team_one||',', ','||?||',')>0 OR instr(','||team_two||',', ','||?||',')>0)", (guild_id, mode, user_id, user_id)).fetchall()
        records: dict[int, list[int]] = {}
        for row in rows:
            first = [int(value) for value in row["team_one"].split(",")]
            second = [int(value) for value in row["team_two"].split(",")]
            own = first if user_id in first else second
            opponents = second if own is first else first
            won = (row["winner"] == 1 and own is first) or (row["winner"] == 2 and own is second)
            for opponent in opponents:
                values = records.setdefault(opponent, [0, 0])
                values[0 if won else 1] += 1
        return sorted(records.items(), key=lambda item: (-(item[1][0] + item[1][1]), item[0]))

    def annotate_match(self, guild_id: int, match_id: int, note: str, replay_url: str, updated_by: int):
        result = self.connection.execute("INSERT INTO match_annotations (match_id, guild_id, note, replay_url, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET note=excluded.note, replay_url=excluded.replay_url, updated_by=excluded.updated_by, updated_at=excluded.updated_at", (match_id, guild_id, note.strip(), replay_url.strip(), updated_by, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()
        return result

    def annotation(self, guild_id: int, match_id: int):
        return self.connection.execute("SELECT * FROM match_annotations WHERE guild_id=? AND match_id=?", (guild_id, match_id)).fetchone()

    def set_command_role(self, guild_id: int, command_name: str, role_id: int):
        self.connection.execute("INSERT INTO command_roles (guild_id, command_name, role_id) VALUES (?, ?, ?) ON CONFLICT(guild_id, command_name) DO UPDATE SET role_id=excluded.role_id", (guild_id, command_name.lstrip("/"), role_id))
        self.connection.commit()

    def command_role(self, guild_id: int, command_name: str):
        row = self.connection.execute("SELECT role_id FROM command_roles WHERE guild_id=? AND command_name=?", (guild_id, command_name.lstrip("/"))).fetchone()
        return row["role_id"] if row else None

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.connection.commit()
        target = sqlite3.connect(destination)
        self.connection.backup(target)
        target.close()

    def restore(self, source: Path):
        source_connection = sqlite3.connect(source)
        source_connection.backup(self.connection)
        source_connection.close()
        self.connection.commit()

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

    def set_captain(self, guild_id: int, mode: str, team: int, user_id: int):
        self.connection.execute("INSERT INTO captains (guild_id, mode, team, user_id) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, mode, team) DO UPDATE SET user_id=excluded.user_id", (guild_id, mode, team, user_id))
        self.connection.commit()

    def captain(self, guild_id: int, mode: str, team: int):
        row = self.connection.execute("SELECT user_id FROM captains WHERE guild_id=? AND mode=? AND team=?", (guild_id, mode, team)).fetchone()
        return row["user_id"] if row else None

    def create_pending_match(self, guild_id: int, mode: str, winner: int, team_one: list[int], team_two: list[int], stats: dict[int, dict[str, int]], created_by: int, map_name: str):
        cursor = self.connection.execute("INSERT INTO pending_matches (guild_id, mode, winner, team_one, team_two, stats_json, created_by, map_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), json.dumps(stats), created_by, map_name))
        self.connection.commit()
        return cursor.lastrowid

    def pending_match(self, guild_id: int, pending_id: int):
        return self.connection.execute("SELECT * FROM pending_matches WHERE guild_id=? AND id=?", (guild_id, pending_id)).fetchone()

    def confirm_pending_match(self, guild_id: int, pending_id: int, user_id: int):
        row = self.pending_match(guild_id, pending_id)
        if not row:
            return None
        confirmed = set(json.loads(row["confirmed_by"]))
        confirmed.add(user_id)
        self.connection.execute("UPDATE pending_matches SET confirmed_by=? WHERE guild_id=? AND id=?", (json.dumps(sorted(confirmed)), guild_id, pending_id))
        self.connection.commit()
        return self.pending_match(guild_id, pending_id)

    def delete_pending_match(self, guild_id: int, pending_id: int):
        self.connection.execute("DELETE FROM pending_matches WHERE guild_id=? AND id=?", (guild_id, pending_id))
        self.connection.commit()

    def audit(self, guild_id: int, actor_id: int, action: str, details: str):
        self.connection.execute("INSERT INTO audit_log (guild_id, actor_id, action, details) VALUES (?, ?, ?, ?)", (guild_id, actor_id, action, details))
        self.connection.commit()

    def vote(self, guild_id: int, match_id: int, user_id: int, vote: str):
        self.connection.execute("INSERT INTO match_votes (match_id, guild_id, user_id, vote) VALUES (?, ?, ?, ?) ON CONFLICT(match_id, user_id) DO UPDATE SET vote=excluded.vote", (match_id, guild_id, user_id, vote))
        self.connection.commit()

    def votes(self, guild_id: int, match_id: int):
        return self.connection.execute("SELECT vote, COUNT(*) AS count FROM match_votes WHERE guild_id=? AND match_id=? GROUP BY vote", (guild_id, match_id)).fetchall()

    def add_note(self, guild_id: int, user_id: int, author_id: int, note: str):
        cursor = self.connection.execute("INSERT INTO player_notes (guild_id, user_id, author_id, note) VALUES (?, ?, ?, ?)", (guild_id, user_id, author_id, note.strip()))
        self.connection.commit()
        return cursor.lastrowid

    def notes(self, guild_id: int, user_id: int):
        return self.connection.execute("SELECT * FROM player_notes WHERE guild_id=? AND user_id=? ORDER BY id DESC", (guild_id, user_id)).fetchall()

    def delete_note(self, guild_id: int, note_id: int):
        result = self.connection.execute("DELETE FROM player_notes WHERE guild_id=? AND id=?", (guild_id, note_id))
        self.connection.commit()
        return result.rowcount

    def save_preset(self, guild_id: int, name: str, mode: str, players: list[int], created_by: int):
        self.connection.execute("INSERT INTO team_presets (guild_id, name, mode, players, created_by) VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, name) DO UPDATE SET mode=excluded.mode, players=excluded.players, created_by=excluded.created_by", (guild_id, name.strip(), mode, ",".join(map(str, players)), created_by))
        self.connection.commit()

    def preset(self, guild_id: int, name: str):
        return self.connection.execute("SELECT * FROM team_presets WHERE guild_id=? AND name=?", (guild_id, name.strip())).fetchone()

    def presets(self, guild_id: int):
        return self.connection.execute("SELECT * FROM team_presets WHERE guild_id=? ORDER BY name", (guild_id,)).fetchall()

    def delete_preset(self, guild_id: int, name: str):
        result = self.connection.execute("DELETE FROM team_presets WHERE guild_id=? AND name=?", (guild_id, name.strip()))
        self.connection.commit()
        return result.rowcount

    def player_history(self, guild_id: int, user_id: int, limit: int = 10):
        limit = max(1, min(limit, 20))
        return self.connection.execute("SELECT m.*, s.kills, s.deaths, s.assists, s.damage, s.score, s.rating_delta FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE s.guild_id=? AND s.user_id=? ORDER BY m.id DESC LIMIT ?", (guild_id, user_id, limit)).fetchall()

    def map_player_stats(self, guild_id: int, mode: str, user_id: int):
        return self.connection.execute("SELECT m.map_name, COUNT(*) AS games, SUM(s.kills) AS kills, SUM(s.deaths) AS deaths, SUM(s.damage) AS damage, SUM(s.score) AS score FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE m.guild_id=? AND m.mode=? AND s.user_id=? GROUP BY m.map_name ORDER BY games DESC, m.map_name", (guild_id, mode, user_id)).fetchall()

    def set_availability(self, guild_id: int, user_id: int, status: str):
        self.connection.execute("INSERT INTO availability (guild_id, user_id, status, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at", (guild_id, user_id, status, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()

    def availability_rows(self, guild_id: int, status: str | None = None):
        query = "SELECT user_id, status, updated_at FROM availability WHERE guild_id=?"
        params: list[object] = [guild_id]
        if status:
            query += " AND status=?"
            params.append(status)
        return self.connection.execute(query + " ORDER BY status, user_id", params).fetchall()

    def create_schedule(self, guild_id: int, channel_id: int, mode: str, team_one: list[int], team_two: list[int], scheduled_at: str, created_by: int):
        cursor = self.connection.execute("INSERT INTO scheduled_matches (guild_id, channel_id, mode, team_one, team_two, scheduled_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)", (guild_id, channel_id, mode, ",".join(map(str, team_one)), ",".join(map(str, team_two)), scheduled_at, created_by))
        self.connection.commit()
        return cursor.lastrowid

    def due_schedules(self):
        return self.connection.execute("SELECT * FROM scheduled_matches WHERE notified=0 AND scheduled_at<=?", (datetime.now(timezone.utc).isoformat(),)).fetchall()

    def mark_schedule_notified(self, schedule_id: int):
        self.connection.execute("UPDATE scheduled_matches SET notified=1 WHERE id=?", (schedule_id,))
        self.connection.commit()

    def create_series(self, guild_id: int, mode: str, team_one: list[int], team_two: list[int], target_wins: int, created_by: int):
        cursor = self.connection.execute("INSERT INTO series (guild_id, mode, team_one, team_two, target_wins, created_by) VALUES (?, ?, ?, ?, ?, ?)", (guild_id, mode, ",".join(map(str, team_one)), ",".join(map(str, team_two)), target_wins, created_by))
        self.connection.commit()
        return cursor.lastrowid

    def get_series(self, guild_id: int, series_id: int):
        return self.connection.execute("SELECT * FROM series WHERE guild_id=? AND id=?", (guild_id, series_id)).fetchone()

    def update_series(self, guild_id: int, series_id: int, winner: int):
        column = "team_one_wins" if winner == 1 else "team_two_wins"
        self.connection.execute(f"UPDATE series SET {column}={column}+1 WHERE guild_id=? AND id=? AND status='open'", (guild_id, series_id))
        row = self.get_series(guild_id, series_id)
        if row and max(row["team_one_wins"], row["team_two_wins"]) >= row["target_wins"]:
            self.connection.execute("UPDATE series SET status='complete' WHERE guild_id=? AND id=?", (guild_id, series_id))
        self.connection.commit()
        return self.get_series(guild_id, series_id)

    def create_veto(self, guild_id: int, mode: str, team_one: list[int], team_two: list[int], maps: list[str], created_by: int):
        cursor = self.connection.execute("INSERT INTO veto_sessions (guild_id, mode, team_one, team_two, maps, created_by) VALUES (?, ?, ?, ?, ?, ?)", (guild_id, mode, ",".join(map(str, team_one)), ",".join(map(str, team_two)), json.dumps(maps), created_by))
        self.connection.commit()
        return cursor.lastrowid

    def get_veto(self, guild_id: int, veto_id: int):
        return self.connection.execute("SELECT * FROM veto_sessions WHERE guild_id=? AND id=?", (guild_id, veto_id)).fetchone()

    def update_veto(self, guild_id: int, veto_id: int, banned: list[str], picked: str | None = None):
        self.connection.execute("UPDATE veto_sessions SET banned=?, picked=?, status=? WHERE guild_id=? AND id=?", (json.dumps(banned), picked, "complete" if picked else "open", guild_id, veto_id))
        self.connection.commit()
        return self.get_veto(guild_id, veto_id)

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

    def leaderboard(self, guild_id: int, mode: str, metric: str = "rating", limit: int = 10):
        allowed = {"rating": "r.rating", "wins": "r.wins", "winrate": "CAST(r.wins AS REAL) / NULLIF(r.games, 0)", "kills": "COALESCE(SUM(s.kills), 0)", "damage": "COALESCE(SUM(s.damage), 0)", "score": "COALESCE(SUM(s.score), 0)", "assists": "COALESCE(SUM(s.assists), 0)"}
        order = allowed.get(metric, allowed["rating"])
        return self.connection.execute(
            f"SELECT r.user_id, r.rating, r.wins, r.losses, r.games, COALESCE(SUM(s.kills), 0) AS kills, COALESCE(SUM(s.damage), 0) AS damage, COALESCE(SUM(s.score), 0) AS score, COALESCE(SUM(s.assists), 0) AS assists FROM ratings r LEFT JOIN match_player_stats s ON s.guild_id=r.guild_id AND s.user_id=r.user_id AND s.mode=r.mode WHERE r.guild_id=? AND r.mode=? GROUP BY r.user_id, r.rating, r.wins, r.losses, r.games ORDER BY {order} DESC, r.wins DESC LIMIT ?",
            (guild_id, mode, limit),
        ).fetchall()

    def rivalry(self, guild_id: int, mode: str, first_id: int, second_id: int):
        return self.connection.execute(
            """
            SELECT SUM(CASE WHEN (m.winner=1 AND instr(','||m.team_one||',', ','||?||',')>0) OR (m.winner=2 AND instr(','||m.team_two||',', ','||?||',')>0) THEN 1 ELSE 0 END) AS first_wins,
                   SUM(CASE WHEN (m.winner=1 AND instr(','||m.team_one||',', ','||?||',')>0) OR (m.winner=2 AND instr(','||m.team_two||',', ','||?||',')>0) THEN 1 ELSE 0 END) AS second_wins,
                   COUNT(*) AS games
            FROM matches m WHERE m.guild_id=? AND m.mode=?
              AND ((instr(','||m.team_one||',', ','||?||',')>0 AND instr(','||m.team_two||',', ','||?||',')>0)
                OR (instr(','||m.team_two||',', ','||?||',')>0 AND instr(','||m.team_one||',', ','||?||',')>0))
            """,
            (first_id, first_id, second_id, second_id, guild_id, mode, first_id, second_id, first_id, second_id),
        ).fetchone()

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


def has_command_access(interaction: discord.Interaction, command_name: str) -> bool:
    role_id = bot.database.command_role(interaction.guild_id, command_name)
    if not role_id or interaction.user.guild_permissions.manage_guild:
        return True
    return any(role.id == role_id for role in getattr(interaction.user, "roles", []))


class LeaderboardView(discord.ui.View):
    def __init__(self, guild_id: int, mode: str, metric: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.mode = mode
        self.metric = metric

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = bot.database.leaderboard(self.guild_id, self.mode, self.metric)
        if not rows:
            await interaction.response.send_message("No leaderboard data yet.", ephemeral=True)
            return
        value_key = "rating" if self.metric == "rating" else self.metric
        embed = discord.Embed(title=f"{mode_label(self.mode)} leaderboard", description=f"Sorted by **{self.metric}**", colour=discord.Colour.red())
        embed.description += "\n\n" + "\n".join(f"**{index}.** <@{row['user_id']}> — {row[value_key] if value_key != 'rating' else row['rating']}" for index, row in enumerate(rows, 1))
        await interaction.response.edit_message(embed=embed, view=self)


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
            pending_id = bot.database.create_pending_match(interaction.guild_id, self.mode, self.winner, self.team_one, self.team_two, self.stats, interaction.user.id, self.map_name)
        except sqlite3.Error as error:
            await interaction.response.send_message(f"Could not save match for confirmation: {error}", ephemeral=True)
            return
        await interaction.response.send_message(f"📝 Match **#{pending_id}** is ready for confirmation. One player from each team must use `/match_confirm match_id:{pending_id}`. Use `/match_cancel match_id:{pending_id}` to discard it.")


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


@bot.tree.command(name="season_reset", description="Reset current ratings for a fresh season")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_reset(interaction: discord.Interaction):
    bot.database.reset_ratings(interaction.guild_id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "season_reset", "ratings reset; match history preserved")
    await interaction.response.send_message("✅ Current ratings and season records were reset. Historical matches remain available through `/history` and `/myhistory`.")


@bot.tree.command(name="teamleaderboard", description="Rank recurring teams in a mode")
@app_commands.describe(mode="Game mode", limit="Number of teams")
@app_commands.choices(mode=mode_choices)
async def teamleaderboard(interaction: discord.Interaction, mode: app_commands.Choice[str], limit: int = 10):
    rows = bot.database.team_leaderboard(interaction.guild_id, mode.value, limit)
    if not rows:
        await interaction.response.send_message("No recurring teams have recorded matches yet.")
        return
    lines = []
    for index, row in enumerate(rows, 1):
        roster = " + ".join(f"<@{user_id}>" for user_id in row["team_key"].split(","))
        lines.append(f"{index}. {roster} — **{row['wins']}-{row['losses']}** · {row['games']} games · {row['damage']} damage")
    await interaction.response.send_message(f"**{mode_label(mode.value)} team rankings**\n" + "\n".join(lines))


@bot.tree.command(name="player_search", description="Search server members by name")
@app_commands.describe(query="Name fragment")
async def player_search(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    matches = [member for member in interaction.guild.members if query.lower() in f"{member.display_name} {member.name}".lower()][:15]
    if not matches:
        await interaction.response.send_message("No matching players found.")
        return
    await interaction.response.send_message("**Players found**\n" + "\n".join(f"{member.mention} — `{member.id}`" for member in matches))


@bot.tree.command(name="opponents", description="Show a player's opponent records")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def opponents(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    records = bot.database.opponent_records(interaction.guild_id, mode.value, member.id)
    if not records:
        await interaction.response.send_message(f"{member.mention} has no opponent records in that mode.")
        return
    lines = [f"<@{opponent}> — **{wins}-{losses}**" for opponent, (wins, losses) in records[:20]]
    await interaction.response.send_message(f"**{member.display_name}'s opponents — {mode_label(mode.value)}**\n" + "\n".join(lines))


@bot.tree.command(name="match_attach", description="Attach notes or a replay link to a match")
@app_commands.describe(match_id="Match number", note="Optional match note", replay_url="Optional clip or replay URL")
async def match_attach(interaction: discord.Interaction, match_id: int, note: str = "", replay_url: str = ""):
    exists = bot.database.connection.execute("SELECT id FROM matches WHERE guild_id=? AND id=?", (interaction.guild_id, match_id)).fetchone()
    if not exists:
        await interaction.response.send_message("That match was not found.", ephemeral=True)
        return
    if replay_url and not replay_url.startswith(("https://", "http://")):
        await interaction.response.send_message("Replay links must start with http:// or https://.", ephemeral=True)
        return
    bot.database.annotate_match(interaction.guild_id, match_id, note, replay_url, interaction.user.id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_attached", f"match #{match_id}")
    await interaction.response.send_message(f"Attached details to match **#{match_id}**.")


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


@bot.tree.command(name="captain_set", description="Set the captain for one side of a mode")
@app_commands.describe(mode="Game mode", team="Team side", captain="Player who can confirm for this side")
@app_commands.choices(mode=mode_choices, team=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
@app_commands.checks.has_permissions(manage_guild=True)
async def captain_set(interaction: discord.Interaction, mode: app_commands.Choice[str], team: app_commands.Choice[str], captain: discord.Member):
    bot.database.set_captain(interaction.guild_id, mode.value, int(team.value), captain.id)
    await interaction.response.send_message(f"Set {captain.mention} as Team {team.value} captain for **{mode_label(mode.value)}**.")


@bot.tree.command(name="match_confirm", description="Confirm a pending match result")
@app_commands.describe(match_id="Pending match number")
async def match_confirm(interaction: discord.Interaction, match_id: int):
    if not has_command_access(interaction, "match_confirm"):
        await interaction.response.send_message("You do not have the role required to confirm matches.", ephemeral=True)
        return
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row:
        await interaction.response.send_message("That pending match was not found.", ephemeral=True)
        return
    team_one = [int(value) for value in row["team_one"].split(",")]
    team_two = [int(value) for value in row["team_two"].split(",")]
    team = 1 if interaction.user.id in team_one else 2 if interaction.user.id in team_two else 0
    if not team:
        await interaction.response.send_message("Only players in this match can confirm it.", ephemeral=True)
        return
    assigned_captain = bot.database.captain(interaction.guild_id, row["mode"], team)
    if assigned_captain and assigned_captain != interaction.user.id:
        await interaction.response.send_message(f"Only the assigned Team {team} captain can confirm this result.", ephemeral=True)
        return
    confirmed = set(json.loads(row["confirmed_by"]))
    if interaction.user.id in confirmed:
        await interaction.response.send_message("You already confirmed this match.", ephemeral=True)
        return
    row = bot.database.confirm_pending_match(interaction.guild_id, match_id, interaction.user.id)
    confirmed = set(json.loads(row["confirmed_by"]))
    confirmed_teams = {1 if user_id in team_one else 2 for user_id in confirmed}
    if confirmed_teams != {1, 2}:
        await interaction.response.send_message(f"Confirmation saved ({len(confirmed_teams)}/2 teams). A player from the other team still needs to confirm.")
        return
    stats = {int(user_id): values for user_id, values in json.loads(row["stats_json"]).items()}
    changes = bot.database.record_match(interaction.guild_id, row["mode"], row["winner"], team_one, team_two, stats, row["created_by"], row["map_name"])
    bot.database.delete_pending_match(interaction.guild_id, match_id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_recorded", f"pending match #{match_id}; mode={row['mode']}")
    change_text = " · ".join(f"<@{change.user_id}> {change.new_rating} ({change.delta:+d})" for change in changes)
    if interaction.guild:
        for change in changes:
            await update_elo_role(interaction.guild, change.user_id, row["mode"], change.new_rating)
    await interaction.response.send_message(f"✅ **{mode_label(row['mode'])} recorded** — Team {row['winner']} wins\n{change_text}\nStats saved for {len(stats)} players.")


@bot.tree.command(name="match_cancel", description="Discard a pending match result")
@app_commands.describe(match_id="Pending match number")
async def match_cancel(interaction: discord.Interaction, match_id: int):
    if not has_command_access(interaction, "match_cancel"):
        await interaction.response.send_message("You do not have the role required to cancel matches.", ephemeral=True)
        return
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row or interaction.user.id != row["created_by"]:
        await interaction.response.send_message("Only the match submitter can cancel that pending result.", ephemeral=True)
        return
    bot.database.delete_pending_match(interaction.guild_id, match_id)
    await interaction.response.send_message(f"Discarded pending match **#{match_id}**.")


@bot.tree.command(name="match", description="Record a completed private Gears 5 match")
@app_commands.describe(mode="Game mode", winner="Which team won", team_one="Comma-separated mentions/IDs", team_two="Comma-separated mentions/IDs", map_name="Optional map name")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def match(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: app_commands.Choice[str], team_one: str, team_two: str, map_name: str | None = None):
    if not has_command_access(interaction, "match"):
        await interaction.response.send_message("You do not have the role required to submit matches.", ephemeral=True)
        return
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


@bot.tree.command(name="rematch", description="Reuse teams from a prior match and enter a new result")
@app_commands.describe(match_id="Previous match number", winner="Winner of the rematch")
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def rematch(interaction: discord.Interaction, match_id: int, winner: app_commands.Choice[str]):
    row = bot.database.match_history(interaction.guild_id, limit=100)
    previous = next((item for item in row if item["id"] == match_id), None)
    if not previous:
        await interaction.response.send_message("That match was not found.", ephemeral=True)
        return
    first = [int(value) for value in previous["team_one"].split(",")]
    second = [int(value) for value in previous["team_two"].split(",")]
    await interaction.response.send_modal(PlayerStatsModal(previous["mode"], int(winner.value), first, second, first + second, {}, 0, previous["map_name"]))


@bot.tree.command(name="availability", description="Set your availability for finding matches")
@app_commands.describe(status="Your current availability")
@app_commands.choices(status=[app_commands.Choice(name="Available", value="available"), app_commands.Choice(name="Busy", value="busy"), app_commands.Choice(name="Offline", value="offline")])
async def availability(interaction: discord.Interaction, status: app_commands.Choice[str]):
    bot.database.set_availability(interaction.guild_id, interaction.user.id, status.value)
    bot.database.audit(interaction.guild_id, interaction.user.id, "availability", status.value)
    await interaction.response.send_message(f"Set your status to **{status.value}**.")


@bot.tree.command(name="available", description="List players by availability")
async def available(interaction: discord.Interaction):
    rows = bot.database.availability_rows(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("Nobody has set an availability status yet.")
        return
    lines = [f"**{row['status'].title()}**: <@{row['user_id']}>" for row in rows]
    await interaction.response.send_message("**Player availability**\n" + "\n".join(lines))


@bot.tree.command(name="rivalry", description="Show the head-to-head record between two players")
@app_commands.describe(mode="Game mode", first="First player", second="Second player")
@app_commands.choices(mode=mode_choices)
async def rivalry(interaction: discord.Interaction, mode: app_commands.Choice[str], first: discord.Member, second: discord.Member):
    row = bot.database.rivalry(interaction.guild_id, mode.value, first.id, second.id)
    if not row or not row["games"]:
        await interaction.response.send_message("Those players have not faced each other in that mode.")
        return
    await interaction.response.send_message(f"**{first.display_name} vs {second.display_name} — {mode_label(mode.value)}**\nGames: **{row['games']}**\n{first.mention}: **{row['first_wins']} wins**\n{second.mention}: **{row['second_wins']} wins**")


@bot.tree.command(name="match_vote", description="Approve or dispute a pending match result")
@app_commands.describe(match_id="Pending match number", decision="Your decision")
@app_commands.choices(decision=[app_commands.Choice(name="Approve", value="approve"), app_commands.Choice(name="Dispute", value="dispute")])
async def match_vote(interaction: discord.Interaction, match_id: int, decision: app_commands.Choice[str]):
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row:
        await interaction.response.send_message("That pending match was not found.", ephemeral=True)
        return
    players = {int(value) for value in row["team_one"].split(",") + row["team_two"].split(",")}
    if interaction.user.id not in players:
        await interaction.response.send_message("Only players in the match can vote.", ephemeral=True)
        return
    bot.database.vote(interaction.guild_id, match_id, interaction.user.id, decision.value)
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_vote", f"match #{match_id}: {decision.value}")
    if decision.value == "dispute":
        await interaction.response.send_message(f"⚠️ Match **#{match_id}** disputed. The submitter can cancel it and re-enter corrected stats.")
        return
    updated = bot.database.confirm_pending_match(interaction.guild_id, match_id, interaction.user.id)
    confirmed = json.loads(updated["confirmed_by"])
    await interaction.response.send_message(f"Approval recorded for match **#{match_id}** ({len(confirmed)} confirmation(s)). Use `/match_confirm match_id:{match_id}` when both sides have approved.")


@bot.tree.command(name="note_add", description="Add an admin note to a player")
@app_commands.describe(player="Player", note="Note text")
@app_commands.checks.has_permissions(manage_guild=True)
async def note_add(interaction: discord.Interaction, player: discord.Member, note: str):
    note_id = bot.database.add_note(interaction.guild_id, player.id, interaction.user.id, note)
    bot.database.audit(interaction.guild_id, interaction.user.id, "note_added", f"note #{note_id} for {player.id}")
    await interaction.response.send_message(f"Added private admin note **#{note_id}** for {player.display_name}.", ephemeral=True)


@bot.tree.command(name="notes", description="View admin notes for a player")
@app_commands.describe(player="Player")
@app_commands.checks.has_permissions(manage_guild=True)
async def notes(interaction: discord.Interaction, player: discord.Member):
    rows = bot.database.notes(interaction.guild_id, player.id)
    if not rows:
        await interaction.response.send_message("No notes found.", ephemeral=True)
        return
    await interaction.response.send_message("**Admin notes**\n" + "\n".join(f"#{row['id']} ({row['created_at'][:10]}): {row['note']}" for row in rows), ephemeral=True)


@bot.tree.command(name="note_delete", description="Delete an admin note")
@app_commands.describe(note_id="Note number")
@app_commands.checks.has_permissions(manage_guild=True)
async def note_delete(interaction: discord.Interaction, note_id: int):
    if not bot.database.delete_note(interaction.guild_id, note_id):
        await interaction.response.send_message("That note was not found.", ephemeral=True)
        return
    bot.database.audit(interaction.guild_id, interaction.user.id, "note_deleted", f"note #{note_id}")
    await interaction.response.send_message(f"Deleted note **#{note_id}**.", ephemeral=True)


@bot.tree.command(name="preset_save", description="Save a frequent team roster")
@app_commands.describe(name="Preset name", mode="Game mode", players="Comma-separated players")
@app_commands.choices(mode=mode_choices)
async def preset_save(interaction: discord.Interaction, name: str, mode: app_commands.Choice[str], players: str):
    try:
        roster = parse_team(players, team_size(mode.value))
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    bot.database.save_preset(interaction.guild_id, name, mode.value, roster, interaction.user.id)
    await interaction.response.send_message(f"Saved team preset **{name}** for {mode_label(mode.value)}.")


@bot.tree.command(name="presets", description="List saved team presets")
async def presets(interaction: discord.Interaction):
    rows = bot.database.presets(interaction.guild_id)
    await interaction.response.send_message("**Team presets**\n" + ("\n".join(f"**{row['name']}** — {mode_label(row['mode'])}: " + " + ".join(f"<@{x}>" for x in row['players'].split(",")) for row in rows) if rows else "No presets saved."))


@bot.tree.command(name="preset_delete", description="Delete a saved team preset")
@app_commands.describe(name="Preset name")
async def preset_delete(interaction: discord.Interaction, name: str):
    if not bot.database.delete_preset(interaction.guild_id, name):
        await interaction.response.send_message("That preset was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"Deleted preset **{name}**.")


@bot.tree.command(name="series_start", description="Start a best-of-3 or best-of-5 series")
@app_commands.describe(mode="Game mode", team_one="Team 1 players", team_two="Team 2 players", format="Series format")
@app_commands.choices(mode=mode_choices, format=[app_commands.Choice(name="Best of 3", value="2"), app_commands.Choice(name="Best of 5", value="3")])
async def series_start(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str, format: app_commands.Choice[str]):
    try:
        first = parse_team(team_one, team_size(mode.value))
        second = parse_team(team_two, team_size(mode.value))
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    series_id = bot.database.create_series(interaction.guild_id, mode.value, first, second, int(format.value), interaction.user.id)
    await interaction.response.send_message(f"Started **BO{int(format.value) * 2 - 1} series #{series_id}** for {mode_label(mode.value)}. Use `/series_update series_id:{series_id} winner:Team 1` after each game.")


@bot.tree.command(name="series_update", description="Add a game result to a series")
@app_commands.describe(series_id="Series number", winner="Winning side")
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def series_update(interaction: discord.Interaction, series_id: int, winner: app_commands.Choice[str]):
    row = bot.database.update_series(interaction.guild_id, series_id, int(winner.value))
    if not row:
        await interaction.response.send_message("That open series was not found.", ephemeral=True)
        return
    status = "COMPLETE" if row["status"] == "complete" else "in progress"
    await interaction.response.send_message(f"Series **#{series_id}** is **{status}**: Team 1 **{row['team_one_wins']}** — Team 2 **{row['team_two_wins']}**.")


@bot.tree.command(name="series_status", description="Show a series score")
@app_commands.describe(series_id="Series number")
async def series_status(interaction: discord.Interaction, series_id: int):
    row = bot.database.get_series(interaction.guild_id, series_id)
    if not row:
        await interaction.response.send_message("That series was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"**Series #{series_id}** — {mode_label(row['mode'])}\nTeam 1: **{row['team_one_wins']}** · Team 2: **{row['team_two_wins']}** · {row['status'].title()}")


@bot.tree.command(name="schedule", description="Schedule a match reminder")
@app_commands.describe(mode="Game mode", team_one="Team 1 players", team_two="Team 2 players", when="UTC ISO time, e.g. 2026-09-01T20:00:00+00:00")
@app_commands.choices(mode=mode_choices)
async def schedule(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str, when: str):
    try:
        first = parse_team(team_one, team_size(mode.value))
        second = parse_team(team_two, team_size(mode.value))
        scheduled_at = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        if datetime.fromisoformat(scheduled_at) <= datetime.now(timezone.utc):
            raise ValueError("The scheduled time must be in the future")
    except ValueError as error:
        await interaction.response.send_message(f"Could not schedule match: {error}", ephemeral=True)
        return
    schedule_id = bot.database.create_schedule(interaction.guild_id, interaction.channel_id, mode.value, first, second, scheduled_at, interaction.user.id)
    await interaction.response.send_message(f"⏰ Scheduled match **#{schedule_id}** for **{scheduled_at[:16].replace('T', ' ')} UTC**.")


@bot.tree.command(name="veto_start", description="Start a map ban and pick session")
@app_commands.describe(mode="Game mode", team_one="Team 1 players", team_two="Team 2 players", maps="Comma-separated map names")
@app_commands.choices(mode=mode_choices)
async def veto_start(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str, maps: str):
    try:
        first = parse_team(team_one, team_size(mode.value))
        second = parse_team(team_two, team_size(mode.value))
        map_list = [item.strip() for item in maps.split(",") if item.strip()]
        if len(map_list) < 2:
            raise ValueError("Enter at least two maps")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    veto_id = bot.database.create_veto(interaction.guild_id, mode.value, first, second, map_list, interaction.user.id)
    await interaction.response.send_message(f"🗺️ Veto session **#{veto_id}** started with: {', '.join(map_list)}. Use `/veto_ban` or `/veto_pick`.")


@bot.tree.command(name="veto_ban", description="Ban a map from a veto session")
@app_commands.describe(veto_id="Veto session number", map_name="Map to ban")
async def veto_ban(interaction: discord.Interaction, veto_id: int, map_name: str):
    row = bot.database.get_veto(interaction.guild_id, veto_id)
    if not row:
        await interaction.response.send_message("That veto session was not found.", ephemeral=True)
        return
    maps = json.loads(row["maps"]); banned = json.loads(row["banned"]); map_name = map_name.strip()
    if map_name not in maps or map_name in banned or row["picked"]:
        await interaction.response.send_message("That map cannot be banned in this session.", ephemeral=True)
        return
    banned.append(map_name); bot.database.update_veto(interaction.guild_id, veto_id, banned)
    remaining = [item for item in maps if item not in banned]
    await interaction.response.send_message(f"Banned **{map_name}**. Remaining maps: {', '.join(remaining)}")


@bot.tree.command(name="veto_pick", description="Pick the final map in a veto session")
@app_commands.describe(veto_id="Veto session number", map_name="Map to pick")
async def veto_pick(interaction: discord.Interaction, veto_id: int, map_name: str):
    row = bot.database.get_veto(interaction.guild_id, veto_id)
    if not row:
        await interaction.response.send_message("That veto session was not found.", ephemeral=True)
        return
    maps = json.loads(row["maps"]); banned = json.loads(row["banned"]); map_name = map_name.strip()
    if map_name not in maps or map_name in banned or row["picked"]:
        await interaction.response.send_message("That map cannot be picked in this session.", ephemeral=True)
        return
    bot.database.update_veto(interaction.guild_id, veto_id, banned, map_name)
    await interaction.response.send_message(f"✅ **{map_name}** selected for veto session **#{veto_id}**.")


@bot.tree.command(name="leaderboard", description="Show the top ratings for a mode")
@app_commands.describe(mode="Game mode", metric="Ranking metric")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(metric=[app_commands.Choice(name="Elo", value="rating"), app_commands.Choice(name="Wins", value="wins"), app_commands.Choice(name="Win rate", value="winrate"), app_commands.Choice(name="Kills", value="kills"), app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Score", value="score"), app_commands.Choice(name="Assists", value="assists")])
async def leaderboard(interaction: discord.Interaction, mode: app_commands.Choice[str], metric: app_commands.Choice[str] | None = None):
    metric_value = metric.value if metric else "rating"
    rows = bot.database.leaderboard(interaction.guild_id, mode.value, metric_value)
    if not rows:
        await interaction.response.send_message(f"No matches have been recorded for **{mode_label(mode.value)}** yet.")
        return
    values = {"rating": "Elo", "wins": "wins", "winrate": "win rate", "kills": "kills", "damage": "damage", "score": "score", "assists": "assists"}
    embed = discord.Embed(title=f"{mode_label(mode.value)} leaderboard", description=f"Sorted by **{values[metric_value]}**", colour=discord.Colour.red())
    embed.description += "\n\n" + "\n".join(f"**{index}.** <@{row['user_id']}> — {row['rating']} Elo · {row['wins']}-{row['losses']} · {row[metric_value] if metric_value != 'winrate' else row['wins'] / row['games'] * 100:.1f}" for index, row in enumerate(rows, 1))
    await interaction.response.send_message(embed=embed, view=LeaderboardView(interaction.guild_id, mode.value, metric_value))


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


@bot.tree.command(name="myhistory", description="Show your recent matches with full personal stats")
@app_commands.describe(limit="Number of matches, from 1 to 20")
async def myhistory(interaction: discord.Interaction, limit: int = 10):
    rows = bot.database.player_history(interaction.guild_id, interaction.user.id, limit)
    if not rows:
        await interaction.response.send_message("You have no recorded matches yet.")
        return
    lines = [f"**#{row['id']} {mode_label(row['mode'])}** — Team {row['winner']} won · K/D {row['kills']}/{row['deaths']} · Damage {row['damage']} · Score {row['score']} · Elo {row['rating_delta']:+d}" for row in rows]
    await interaction.response.send_message("**Your recent match history**\n" + "\n".join(lines))


@bot.tree.command(name="mapplayer", description="Show a player's performance by map")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def mapplayer(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.map_player_stats(interaction.guild_id, mode.value, member.id)
    if not rows:
        await interaction.response.send_message(f"{member.mention} has no map data for {mode_label(mode.value)}.")
        return
    lines = [f"**{row['map_name']}** — {row['games']} games · K/D {row['kills']}/{row['deaths']} · Damage {row['damage']} · Score {row['score']}" for row in rows]
    await interaction.response.send_message(f"**{member.display_name} map analytics — {mode_label(mode.value)}**\n" + "\n".join(lines))


@bot.tree.command(name="announce", description="Post a leaderboard announcement")
@app_commands.describe(mode="Game mode", metric="Leaderboard metric")
@app_commands.choices(mode=mode_choices, metric=[app_commands.Choice(name="Elo", value="rating"), app_commands.Choice(name="Wins", value="wins"), app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Kills", value="kills")])
@app_commands.checks.has_permissions(manage_guild=True)
async def announce(interaction: discord.Interaction, mode: app_commands.Choice[str], metric: app_commands.Choice[str] | None = None):
    metric_value = metric.value if metric else "rating"
    rows = bot.database.leaderboard(interaction.guild_id, mode.value, metric_value, 5)
    if not rows:
        await interaction.response.send_message("No leaderboard data yet.", ephemeral=True)
        return
    names = {"rating": "Elo", "wins": "wins", "damage": "damage", "kills": "kills"}
    lines = [f"{index}. <@{row['user_id']}> — {row[metric_value] if metric_value != 'rating' else row['rating']} {names[metric_value]}" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message(f"📣 **{mode_label(mode.value)} leaderboard announcement**\n" + "\n".join(lines))


@bot.tree.command(name="match_edit", description="Correct a player's recorded stats in a match")
@app_commands.describe(match_id="Recorded match number", player="Player whose stats need correction", stats_line="Complete stat line, e.g. kills=10 deaths=4 damage=200 score=100")
@app_commands.checks.has_permissions(manage_guild=True)
async def match_edit(interaction: discord.Interaction, match_id: int, player: discord.Member, stats_line: str):
    match_row = bot.database.connection.execute("SELECT mode FROM matches WHERE guild_id=? AND id=?", (interaction.guild_id, match_id)).fetchone()
    if not match_row:
        await interaction.response.send_message("That match was not found.", ephemeral=True)
        return
    try:
        values = parse_player_stats(stats_line, match_row["mode"])
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    result = bot.database.connection.execute("UPDATE match_player_stats SET captures=?, breaks=?, kills=?, deaths=?, assists=?, damage=?, score=? WHERE guild_id=? AND match_id=? AND user_id=?", (values.get("captures", 0), values.get("breaks", 0), values.get("kills", 0), values.get("deaths", 0), values.get("assists", 0), values.get("damage", 0), values.get("score", 0), interaction.guild_id, match_id, player.id))
    bot.database.connection.commit()
    if not result.rowcount:
        await interaction.response.send_message("That player was not part of the match.", ephemeral=True)
        return
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_edited", f"match #{match_id}; player={player.id}")
    await interaction.response.send_message(f"Corrected {player.mention}'s stats in match **#{match_id}**. Elo was not changed; use `/undo` and re-enter the match if the result or ratings also need correction.", ephemeral=True)


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
    if not has_command_access(interaction, "undo"):
        await interaction.response.send_message("You do not have the role required to undo matches.", ephemeral=True)
        return
    try:
        removed = bot.database.undo_latest_match(interaction.guild_id)
    except (ValueError, sqlite3.Error) as error:
        await interaction.response.send_message(f"Could not undo match: {error}", ephemeral=True)
        return
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_undone", f"match #{removed['id']}; mode={removed['mode']}")
    await interaction.response.send_message(f"Undid match **#{removed['id']}** ({mode_label(removed['mode'])}). Re-enter it with `/match` if needed.")


@bot.tree.command(name="audit", description="Show recent administrative bot actions")
@app_commands.describe(limit="Number of entries, from 1 to 20")
@app_commands.checks.has_permissions(manage_guild=True)
async def audit(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(limit, 20))
    rows = bot.database.connection.execute("SELECT actor_id, action, details, created_at FROM audit_log WHERE guild_id=? ORDER BY id DESC LIMIT ?", (interaction.guild_id, limit)).fetchall()
    if not rows:
        await interaction.response.send_message("No audit entries yet.")
        return
    lines = [f"{row['created_at'][:16]} — <@{row['actor_id']}> — **{row['action']}** — {row['details']}" for row in rows]
    await interaction.response.send_message("**Recent audit log**\n" + "\n".join(lines))


@bot.tree.command(name="permission_set", description="Require a Discord role for a command")
@app_commands.describe(command="Command name without slash", role="Required role")
@app_commands.checks.has_permissions(manage_guild=True)
async def permission_set(interaction: discord.Interaction, command: str, role: discord.Role):
    bot.database.set_command_role(interaction.guild_id, command, role.id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "permission_set", f"/{command.lstrip('/')} requires {role.id}")
    await interaction.response.send_message(f"Configured **/{command.lstrip('/')}** to require {role.mention} (managers can still use it).")


@bot.tree.command(name="backup_now", description="Create a database backup")
@app_commands.checks.has_permissions(manage_guild=True)
async def backup_now(interaction: discord.Interaction):
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIRECTORY / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3"
    bot.database.backup(destination)
    bot.database.audit(interaction.guild_id, interaction.user.id, "backup_created", destination.name)
    await interaction.response.send_message(f"Created database backup `{destination.name}`.", ephemeral=True)


@bot.tree.command(name="backup_restore", description="Restore a database backup by filename")
@app_commands.describe(filename="Backup filename from the backup folder")
@app_commands.checks.has_permissions(administrator=True)
async def backup_restore(interaction: discord.Interaction, filename: str):
    source = BACKUP_DIRECTORY / Path(filename).name
    if not source.is_file() or source.suffix != ".sqlite3":
        await interaction.response.send_message("That backup file was not found.", ephemeral=True)
        return
    bot.database.backup(BACKUP_DIRECTORY / f"pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3")
    bot.database.restore(source)
    bot.database.audit(interaction.guild_id, interaction.user.id, "backup_restored", source.name)
    await interaction.response.send_message(f"Restored `{source.name}`. A pre-restore backup was created automatically.", ephemeral=True)


@tasks.loop(hours=24)
async def automatic_backup():
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIRECTORY / f"automatic-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3"
    bot.database.backup(destination)


@tasks.loop(minutes=1)
async def scheduled_reminders():
    for row in bot.database.due_schedules():
        channel = bot.get_channel(row["channel_id"])
        if channel:
            players = row["team_one"].split(",") + row["team_two"].split(",")
            mentions = " ".join(f"<@{player_id}>" for player_id in players)
            await channel.send(f"⏰ Match reminder — **{mode_label(row['mode'])}** is scheduled now. {mentions}")
        bot.database.mark_schedule_notified(row["id"])


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")
    if not scheduled_reminders.is_running():
        scheduled_reminders.start()
    if not automatic_backup.is_running():
        automatic_backup.start()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    bot.run(TOKEN)
