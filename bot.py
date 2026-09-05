from __future__ import annotations

import os
import asyncio
import json
import logging
import random
import secrets
import shutil
import sqlite3
import urllib.request
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from elo import MODES, balance_teams, calculate_match_changes, calculate_trueskill_changes, canonical_matchup, expected_score, gow2_rank, mode_label, parse_player_list, parse_player_stats, parse_team, stat_names, team_key, team_size, trueskill_display, TRUESKILL_MU, TRUESKILL_SIGMA

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
        existing_schema = path.exists() and path.stat().st_size > 0
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        schema_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if existing_schema and schema_version < 2:
            backup_directory = path.parent / f"{path.stem}_backups"
            backup_directory.mkdir(parents=True, exist_ok=True)
            backup_path = backup_directory / f"{path.stem}_pre_migration_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.sqlite3"
            shutil.copy2(path, backup_path)
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
                provisional_games INTEGER NOT NULL DEFAULT 0,
                peak_rating INTEGER NOT NULL DEFAULT 1000,
                mu REAL NOT NULL DEFAULT 25.0,
                sigma REAL NOT NULL DEFAULT 8.3333333333,
                skill_rating INTEGER NOT NULL DEFAULT 1000,
                gow2_rank INTEGER NOT NULL DEFAULT 1,
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
                mu_before REAL,
                sigma_before REAL,
                mu_after REAL,
                sigma_after REAL,
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
                rating_floor INTEGER NOT NULL DEFAULT 0,
                provisional_games INTEGER NOT NULL DEFAULT 5,
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
            CREATE TABLE IF NOT EXISTS matchmaking_queue (
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, mode, user_id)
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
            CREATE TABLE IF NOT EXISTS rating_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                before_rating INTEGER NOT NULL,
                after_rating INTEGER NOT NULL,
                before_mu REAL NOT NULL,
                before_sigma REAL NOT NULL,
                after_mu REAL NOT NULL,
                after_sigma REAL NOT NULL,
                before_peak INTEGER NOT NULL,
                after_peak INTEGER NOT NULL,
                reason TEXT NOT NULL,
                rolled_back INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                format TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'registration',
                created_by INTEGER NOT NULL,
                bracket TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS tournament_entries (
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                team_name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tournament_id, user_id),
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                players TEXT NOT NULL,
                team_one TEXT NOT NULL DEFAULT '[]',
                team_two TEXT NOT NULL DEFAULT '[]',
                turn INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lobby_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                team_one TEXT NOT NULL,
                team_two TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                checked_in TEXT NOT NULL DEFAULT '[]',
                no_shows TEXT NOT NULL DEFAULT '[]',
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS temporary_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER PRIMARY KEY,
                delete_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS custom_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                metric TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS map_rotation (
                guild_id INTEGER PRIMARY KEY,
                maps TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS webhook_settings (
                guild_id INTEGER PRIMARY KEY,
                webhook_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS dashboard_shares (
                guild_id INTEGER NOT NULL,
                token TEXT PRIMARY KEY,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nickname_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id INTEGER PRIMARY KEY,
                maintenance INTEGER NOT NULL DEFAULT 0,
                announcement_channel_id INTEGER,
                dashboard_refresh_seconds INTEGER NOT NULL DEFAULT 30
            );
            CREATE TABLE IF NOT EXISTS player_profiles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                gamertag TEXT NOT NULL DEFAULT '',
                aliases TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS announcement_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                metric TEXT NOT NULL DEFAULT 'rating',
                interval_minutes INTEGER NOT NULL,
                next_run TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL
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
        if "provisional_games" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN provisional_games INTEGER NOT NULL DEFAULT 0")
        if "peak_rating" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN peak_rating INTEGER NOT NULL DEFAULT 1000")
        if "mu" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN mu REAL NOT NULL DEFAULT 25.0")
        if "sigma" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN sigma REAL NOT NULL DEFAULT 8.3333333333")
        if "skill_rating" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN skill_rating INTEGER NOT NULL DEFAULT 1000")
        if "gow2_rank" not in rating_columns:
            self.connection.execute("ALTER TABLE ratings ADD COLUMN gow2_rank INTEGER NOT NULL DEFAULT 1")
        stats_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(match_player_stats)")}
        for column in ("mu_before", "sigma_before", "mu_after", "sigma_after"):
            if column not in stats_columns:
                self.connection.execute(f"ALTER TABLE match_player_stats ADD COLUMN {column} REAL")
        # Existing 1000-scale Elo values become a conservative TrueSkill seed.
        self.connection.execute("UPDATE ratings SET mu=25.0 + (rating - 1000) / 40.0, skill_rating=rating WHERE mu=25.0 AND rating<>1000")
        for row in self.connection.execute("SELECT guild_id, user_id, mode, skill_rating FROM ratings").fetchall():
            self.connection.execute("UPDATE ratings SET gow2_rank=? WHERE guild_id=? AND user_id=? AND mode=?", (gow2_rank(row["skill_rating"])[0], row["guild_id"], row["user_id"], row["mode"]))
        settings_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(elo_settings)")}
        if "rating_floor" not in settings_columns:
            self.connection.execute("ALTER TABLE elo_settings ADD COLUMN rating_floor INTEGER NOT NULL DEFAULT 0")
        if "provisional_games" not in settings_columns:
            self.connection.execute("ALTER TABLE elo_settings ADD COLUMN provisional_games INTEGER NOT NULL DEFAULT 5")
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_matches_guild_mode ON matches(guild_id, mode, id DESC);
            CREATE INDEX IF NOT EXISTS idx_stats_guild_user_mode ON match_player_stats(guild_id, user_id, mode);
            CREATE INDEX IF NOT EXISTS idx_stats_guild_mode ON match_player_stats(guild_id, mode);
            CREATE INDEX IF NOT EXISTS idx_ratings_guild_mode_rating ON ratings(guild_id, mode, rating DESC);
            CREATE INDEX IF NOT EXISTS idx_profiles_guild_gamertag ON player_profiles(guild_id, gamertag);
            """
        )
        self.connection.execute("PRAGMA user_version = 2")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def get_rating(self, guild_id: int, user_id: int, mode: str) -> int:
        row = self.connection.execute(
            "SELECT rating FROM ratings WHERE guild_id=? AND user_id=? AND mode=?",
            (guild_id, user_id, mode),
        ).fetchone()
        return row["rating"] if row else self.elo_settings(guild_id, mode)["starting_rating"]

    def adjust_rating(self, guild_id: int, user_id: int, mode: str, delta: int, actor_id: int = 0, reason: str = "") -> tuple[int, int, int, str]:
        """Apply an admin rating adjustment without changing match statistics."""
        if delta == 0:
            raise ValueError("The adjustment must not be zero.")
        row = self.connection.execute("SELECT * FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (guild_id, user_id, mode)).fetchone()
        settings = self.elo_settings(guild_id, mode)
        old_rating = int(row["rating"]) if row else int(settings["starting_rating"])
        new_rating = max(int(settings["rating_floor"]), old_rating + delta)
        applied_delta = new_rating - old_rating
        old_mu, sigma = self.get_trueskill(guild_id, user_id, mode)
        new_mu = old_mu + applied_delta / 40.0
        rank_number, rank_name = gow2_rank(new_rating)
        before_peak = int(row["peak_rating"]) if row else old_rating
        peak_rating = max(new_rating, before_peak)
        self.connection.execute(
            "INSERT INTO ratings (guild_id, user_id, mode, rating, peak_rating, mu, sigma, skill_rating, gow2_rank) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, mode) DO UPDATE SET rating=excluded.rating, peak_rating=excluded.peak_rating, mu=excluded.mu, sigma=excluded.sigma, skill_rating=excluded.skill_rating, gow2_rank=excluded.gow2_rank",
            (guild_id, user_id, mode, new_rating, peak_rating, new_mu, sigma, new_rating, rank_number),
        )
        self.connection.execute(
            "INSERT INTO rating_adjustments (guild_id, user_id, mode, actor_id, delta, before_rating, after_rating, before_mu, before_sigma, after_mu, after_sigma, before_peak, after_peak, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, mode, actor_id, applied_delta, old_rating, new_rating, old_mu, sigma, new_mu, sigma, before_peak, peak_rating, reason.strip()[:500]),
        )
        self.connection.commit()
        return old_rating, new_rating, rank_number, rank_name

    def rating_adjustments(self, guild_id: int, user_id: int | None = None, mode: str | None = None, limit: int = 10):
        clauses = ["guild_id=?"]
        params: list[object] = [guild_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if mode:
            clauses.append("mode=?")
            params.append(mode)
        params.append(max(1, min(limit, 25)))
        return self.connection.execute(f"SELECT * FROM rating_adjustments WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", params).fetchall()

    def rating_adjustment(self, guild_id: int, adjustment_id: int):
        return self.connection.execute("SELECT * FROM rating_adjustments WHERE guild_id=? AND id=?", (guild_id, adjustment_id)).fetchone()

    def rollback_rating_adjustment(self, guild_id: int, adjustment_id: int) -> sqlite3.Row:
        adjustment = self.connection.execute("SELECT * FROM rating_adjustments WHERE guild_id=? AND id=?", (guild_id, adjustment_id)).fetchone()
        if not adjustment:
            raise ValueError("That rating adjustment was not found in this server.")
        if adjustment["rolled_back"]:
            raise ValueError("That rating adjustment has already been rolled back.")
        current = self.connection.execute("SELECT rating FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (guild_id, adjustment["user_id"], adjustment["mode"])).fetchone()
        if not current or int(current["rating"]) != int(adjustment["after_rating"]):
            raise ValueError("The player’s rating has changed since this adjustment; rollback was refused to protect newer changes.")
        self.connection.execute("UPDATE ratings SET rating=?, peak_rating=?, mu=?, sigma=?, skill_rating=?, gow2_rank=? WHERE guild_id=? AND user_id=? AND mode=?", (adjustment["before_rating"], adjustment["before_peak"], adjustment["before_mu"], adjustment["before_sigma"], adjustment["before_rating"], gow2_rank(adjustment["before_rating"])[0], guild_id, adjustment["user_id"], adjustment["mode"]))
        self.connection.execute("UPDATE rating_adjustments SET rolled_back=1 WHERE guild_id=? AND id=?", (guild_id, adjustment_id))
        self.connection.commit()
        return adjustment

    def get_trueskill(self, guild_id: int, user_id: int, mode: str) -> tuple[float, float]:
        row = self.connection.execute("SELECT mu, sigma FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (guild_id, user_id, mode)).fetchone()
        if not row:
            starting = self.elo_settings(guild_id, mode)["starting_rating"]
            return TRUESKILL_MU + (starting - DEFAULT_RATING) / 40.0, TRUESKILL_SIGMA
        return float(row["mu"] or TRUESKILL_MU), float(row["sigma"] or TRUESKILL_SIGMA)

    def get_rank(self, guild_id: int, user_id: int, mode: str) -> tuple[int, str, int]:
        row = self.connection.execute("SELECT rating, gow2_rank FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (guild_id, user_id, mode)).fetchone()
        rating = row["rating"] if row else self.elo_settings(guild_id, mode)["starting_rating"]
        rank_number, rank_name = gow2_rank(rating)
        return rank_number, rank_name, rating

    def elo_settings(self, guild_id: int, mode: str):
        row = self.connection.execute("SELECT starting_rating, k_factor, rating_floor, provisional_games FROM elo_settings WHERE guild_id=? AND mode=?", (guild_id, mode)).fetchone()
        return row or {"starting_rating": DEFAULT_RATING, "k_factor": DEFAULT_K_FACTOR, "rating_floor": 0, "provisional_games": 5}

    def set_elo_settings(self, guild_id: int, mode: str, starting_rating: int, k_factor: int, rating_floor: int | None = None, provisional_games: int | None = None):
        current = self.elo_settings(guild_id, mode)
        floor = current["rating_floor"] if rating_floor is None else rating_floor
        provisional = current["provisional_games"] if provisional_games is None else provisional_games
        self.connection.execute("INSERT INTO elo_settings (guild_id, mode, starting_rating, k_factor, rating_floor, provisional_games) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, mode) DO UPDATE SET starting_rating=excluded.starting_rating, k_factor=excluded.k_factor, rating_floor=excluded.rating_floor, provisional_games=excluded.provisional_games", (guild_id, mode, starting_rating, k_factor, floor, provisional))
        self.connection.commit()

    def server_settings(self, guild_id: int):
        row = self.connection.execute("SELECT * FROM server_settings WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            return row
        self.connection.execute("INSERT INTO server_settings (guild_id) VALUES (?)", (guild_id,))
        self.connection.commit()
        return self.connection.execute("SELECT * FROM server_settings WHERE guild_id=?", (guild_id,)).fetchone()

    def set_maintenance(self, guild_id: int, enabled: bool):
        self.server_settings(guild_id)
        self.connection.execute("UPDATE server_settings SET maintenance=? WHERE guild_id=?", (int(enabled), guild_id))
        self.connection.commit()

    def set_announcement_channel(self, guild_id: int, channel_id: int):
        self.server_settings(guild_id)
        self.connection.execute("UPDATE server_settings SET announcement_channel_id=? WHERE guild_id=?", (channel_id, guild_id))
        self.connection.commit()

    def set_dashboard_refresh(self, guild_id: int, seconds: int):
        self.server_settings(guild_id)
        self.connection.execute("UPDATE server_settings SET dashboard_refresh_seconds=? WHERE guild_id=?", (seconds, guild_id))
        self.connection.commit()

    def set_profile(self, guild_id: int, user_id: int, gamertag: str, aliases: list[str]):
        self.connection.execute(
            "INSERT INTO player_profiles (guild_id,user_id,gamertag,aliases) VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET gamertag=excluded.gamertag, aliases=excluded.aliases",
            (guild_id, user_id, gamertag.strip()[:50], json.dumps(aliases[:20])),
        )
        self.connection.commit()

    def profile(self, guild_id: int, user_id: int):
        return self.connection.execute("SELECT * FROM player_profiles WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()

    def schedule_announcement(self, guild_id: int, channel_id: int, mode: str, metric: str, interval_minutes: int, created_by: int):
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
        cursor = self.connection.execute(
            "INSERT INTO announcement_schedules (guild_id,channel_id,mode,metric,interval_minutes,next_run,created_by) VALUES (?,?,?,?,?,?,?)",
            (guild_id, channel_id, mode, metric, interval_minutes, next_run, created_by),
        )
        self.connection.commit()
        return cursor.lastrowid

    def due_announcements(self):
        return self.connection.execute("SELECT * FROM announcement_schedules WHERE enabled=1 AND next_run<=?", (datetime.now(timezone.utc).isoformat(),)).fetchall()

    def advance_announcement(self, schedule_id: int, interval_minutes: int):
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
        self.connection.execute("UPDATE announcement_schedules SET next_run=? WHERE id=?", (next_run, schedule_id))
        self.connection.commit()

    def delete_announcement(self, guild_id: int, schedule_id: int):
        deleted = self.connection.execute("DELETE FROM announcement_schedules WHERE guild_id=? AND id=?", (guild_id, schedule_id)).rowcount
        self.connection.commit()
        return deleted

    def team_history(self, guild_id: int, mode: str, player_ids: list[int]):
        return self.connection.execute(
            "SELECT * FROM team_performance WHERE guild_id=? AND mode=? AND team_key=?",
            (guild_id, mode, team_key(player_ids)),
        ).fetchone()

    def replay_gallery(self, guild_id: int, mode: str | None = None, limit: int = 15):
        sql = "SELECT m.id,m.mode,m.map_name,m.created_at,a.replay_url,a.note FROM matches m JOIN match_annotations a ON a.match_id=m.id WHERE m.guild_id=? AND a.replay_url<>''"
        params: list[object] = [guild_id]
        if mode:
            sql += " AND m.mode=?"
            params.append(mode)
        sql += " ORDER BY m.id DESC LIMIT ?"
        params.append(max(1, min(limit, 50)))
        return self.connection.execute(sql, params).fetchall()

    def active_season(self, guild_id: int):
        return self.connection.execute("SELECT * FROM seasons WHERE guild_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (guild_id,)).fetchone()

    def season_standings(self, guild_id: int, season_id: int, mode: str):
        """Return season records with ratings and the stats needed for divisions."""
        return self.connection.execute(
            """SELECT s.user_id, r.rating,
                      COUNT(*) AS games,
                      SUM(CASE WHEN (m.winner=1 AND instr(','||m.team_one||',', ','||s.user_id||',')>0)
                                    OR (m.winner=2 AND instr(','||m.team_two||',', ','||s.user_id||',')>0)
                               THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN (m.winner=1 AND instr(','||m.team_one||',', ','||s.user_id||',')>0)
                                    OR (m.winner=2 AND instr(','||m.team_two||',', ','||s.user_id||',')>0)
                               THEN 0 ELSE 1 END) AS losses,
                      SUM(s.kills) AS kills, SUM(s.damage) AS damage,
                      SUM(s.score) AS score, SUM(s.assists) AS assists
                 FROM match_player_stats s
                 JOIN matches m ON m.id=s.match_id AND m.guild_id=s.guild_id
                 LEFT JOIN ratings r ON r.guild_id=s.guild_id AND r.user_id=s.user_id AND r.mode=m.mode
                WHERE s.guild_id=? AND m.season_id=? AND m.mode=?
                GROUP BY s.user_id, r.rating
                ORDER BY wins DESC, rating DESC, games DESC, s.user_id""",
            (guild_id, season_id, mode),
        ).fetchall()

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
            mu = TRUESKILL_MU + (starting_rating - DEFAULT_RATING) / 40.0
            self.connection.execute("UPDATE ratings SET rating=?, skill_rating=?, gow2_rank=?, mu=?, sigma=?, current_streak=0, best_streak=0, wins=0, losses=0, games=0, provisional_games=(SELECT provisional_games FROM elo_settings WHERE guild_id=? AND mode=?) WHERE guild_id=? AND user_id=? AND mode=?", (starting_rating, starting_rating, gow2_rank(starting_rating)[0], mu, TRUESKILL_SIGMA, guild_id, row["mode"], guild_id, row["user_id"], row["mode"]))
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

    def create_tournament(self, guild_id: int, name: str, mode: str, tournament_format: str, created_by: int):
        cursor = self.connection.execute("INSERT INTO tournaments (guild_id, name, mode, format, created_by) VALUES (?, ?, ?, ?, ?)", (guild_id, name.strip(), mode, tournament_format, created_by))
        self.connection.commit()
        return cursor.lastrowid

    def tournament(self, guild_id: int, tournament_id: int):
        return self.connection.execute("SELECT * FROM tournaments WHERE guild_id=? AND id=?", (guild_id, tournament_id)).fetchone()

    def tournament_join(self, tournament_id: int, user_id: int, team_name: str = ""):
        self.connection.execute("INSERT OR IGNORE INTO tournament_entries (tournament_id, user_id, team_name) VALUES (?, ?, ?)", (tournament_id, user_id, team_name.strip()))
        self.connection.commit()

    def tournament_entries(self, tournament_id: int):
        return self.connection.execute("SELECT user_id, team_name FROM tournament_entries WHERE tournament_id=? ORDER BY user_id", (tournament_id,)).fetchall()

    def set_tournament_bracket(self, tournament_id: int, bracket: list[dict]):
        self.connection.execute("UPDATE tournaments SET bracket=?, status='active' WHERE id=?", (json.dumps(bracket), tournament_id))
        self.connection.commit()

    def create_draft(self, guild_id: int, mode: str, players: list[int], created_by: int):
        cursor = self.connection.execute("INSERT INTO drafts (guild_id, mode, players, created_by) VALUES (?, ?, ?, ?)", (guild_id, mode, json.dumps(players), created_by))
        self.connection.commit()
        return cursor.lastrowid

    def draft(self, guild_id: int, draft_id: int):
        return self.connection.execute("SELECT * FROM drafts WHERE guild_id=? AND id=?", (guild_id, draft_id)).fetchone()

    def update_draft(self, guild_id: int, draft_id: int, team_one: list[int], team_two: list[int], turn: int, status: str):
        self.connection.execute("UPDATE drafts SET team_one=?, team_two=?, turn=?, status=? WHERE guild_id=? AND id=?", (json.dumps(team_one), json.dumps(team_two), turn, status, guild_id, draft_id))
        self.connection.commit()

    def create_lobby(self, guild_id: int, mode: str, team_one: list[int], team_two: list[int], created_by: int):
        cursor = self.connection.execute("INSERT INTO lobby_sessions (guild_id, mode, team_one, team_two, created_by) VALUES (?, ?, ?, ?, ?)", (guild_id, mode, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by))
        self.connection.commit()
        return cursor.lastrowid

    def lobby(self, guild_id: int, lobby_id: int):
        return self.connection.execute("SELECT * FROM lobby_sessions WHERE guild_id=? AND id=?", (guild_id, lobby_id)).fetchone()

    def update_lobby(self, guild_id: int, lobby_id: int, status: str, checked_in: list[int], no_shows: list[int]):
        self.connection.execute("UPDATE lobby_sessions SET status=?, checked_in=?, no_shows=? WHERE guild_id=? AND id=?", (status, json.dumps(checked_in), json.dumps(no_shows), guild_id, lobby_id))
        self.connection.commit()

    def queue_add(self, guild_id: int, mode: str, user_id: int) -> bool:
        result = self.connection.execute("INSERT OR IGNORE INTO matchmaking_queue (guild_id, mode, user_id, joined_at) VALUES (?, ?, ?, ?)", (guild_id, mode, user_id, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()
        return bool(result.rowcount)

    def queue_remove(self, guild_id: int, mode: str, user_id: int) -> bool:
        result = self.connection.execute("DELETE FROM matchmaking_queue WHERE guild_id=? AND mode=? AND user_id=?", (guild_id, mode, user_id))
        self.connection.commit()
        return bool(result.rowcount)

    def queue_players(self, guild_id: int, mode: str, limit: int | None = None) -> list[int]:
        sql = "SELECT user_id FROM matchmaking_queue WHERE guild_id=? AND mode=? ORDER BY joined_at, user_id"
        params: list[object] = [guild_id, mode]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [row["user_id"] for row in self.connection.execute(sql, params).fetchall()]

    def queue_take(self, guild_id: int, mode: str, user_ids: list[int]) -> None:
        self.connection.executemany("DELETE FROM matchmaking_queue WHERE guild_id=? AND mode=? AND user_id=?", [(guild_id, mode, user_id) for user_id in user_ids])
        self.connection.commit()

    def track_channel(self, guild_id: int, channel_id: int, delete_at: str):
        self.connection.execute("INSERT OR REPLACE INTO temporary_channels (guild_id, channel_id, delete_at) VALUES (?, ?, ?)", (guild_id, channel_id, delete_at))
        self.connection.commit()

    def due_channels(self):
        return self.connection.execute("SELECT * FROM temporary_channels WHERE delete_at<=?", (datetime.now(timezone.utc).isoformat(),)).fetchall()

    def untrack_channel(self, channel_id: int):
        self.connection.execute("DELETE FROM temporary_channels WHERE channel_id=?", (channel_id,))
        self.connection.commit()

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

    def set_rotation(self, guild_id: int, maps: list[str]):
        self.connection.execute("INSERT INTO map_rotation (guild_id, maps, position) VALUES (?, ?, 0) ON CONFLICT(guild_id) DO UPDATE SET maps=excluded.maps, position=0", (guild_id, json.dumps(maps)))
        self.connection.commit()

    def next_map(self, guild_id: int):
        row = self.connection.execute("SELECT maps, position FROM map_rotation WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            return None
        maps = json.loads(row["maps"])
        if not maps:
            return None
        position = row["position"] % len(maps)
        selected = maps[position]
        self.connection.execute("UPDATE map_rotation SET position=? WHERE guild_id=?", ((position + 1) % len(maps), guild_id))
        self.connection.commit()
        return selected

    def create_share(self, guild_id: int, user_id: int):
        token = secrets.token_urlsafe(18)
        self.connection.execute("INSERT INTO dashboard_shares (guild_id, token, created_by, created_at) VALUES (?, ?, ?, ?)", (guild_id, token, user_id, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()
        return token

    def share(self, token: str):
        return self.connection.execute("SELECT * FROM dashboard_shares WHERE token=?", (token,)).fetchone()

    def set_webhook(self, guild_id: int, url: str):
        self.connection.execute("INSERT INTO webhook_settings (guild_id, webhook_url) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET webhook_url=excluded.webhook_url, enabled=1", (guild_id, url))
        self.connection.commit()

    def webhook(self, guild_id: int):
        return self.connection.execute("SELECT * FROM webhook_settings WHERE guild_id=? AND enabled=1", (guild_id,)).fetchone()

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

    def create_achievement(self, guild_id: int, name: str, metric: str, threshold: int, created_by: int):
        cursor = self.connection.execute("INSERT INTO custom_achievements (guild_id, name, metric, threshold, created_by) VALUES (?, ?, ?, ?, ?)", (guild_id, name.strip(), metric, threshold, created_by))
        self.connection.commit()
        return cursor.lastrowid

    def custom_achievements(self, guild_id: int):
        return self.connection.execute("SELECT * FROM custom_achievements WHERE guild_id=? ORDER BY id", (guild_id,)).fetchall()

    def custom_progress(self, guild_id: int, user_id: int, metric: str):
        allowed = {"games": "COUNT(*)", "kills": "SUM(kills)", "damage": "SUM(damage)", "score": "SUM(score)", "assists": "SUM(assists)"}
        expression = allowed.get(metric, allowed["games"])
        row = self.connection.execute(f"SELECT {expression} AS value FROM match_player_stats WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return row["value"] or 0

    def elo_history(self, guild_id: int, user_id: int, mode: str, limit: int = 20):
        return self.rating_history(guild_id, user_id, mode, limit)

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
        rated_one = [(user_id, *self.get_trueskill(guild_id, user_id, mode)) for user_id in team_one]
        rated_two = [(user_id, *self.get_trueskill(guild_id, user_id, mode)) for user_id in team_two]
        settings = self.elo_settings(guild_id, mode)
        changes = calculate_trueskill_changes(mode, rated_one, rated_two, winner)
        season = self.active_season(guild_id)
        cursor = self.connection.execute(
            "INSERT INTO matches (guild_id, mode, winner, team_one, team_two, created_by, season_id, map_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by, season["id"] if season else None, (map_name or "Unknown").strip()[:100]),
        )
        match_id = cursor.lastrowid
        for user_id, values in stats.items():
            change = next(change for change in changes if change.user_id == user_id)
            final_rating = max(settings["rating_floor"], change.new_rating)
            self.connection.execute(
                "INSERT INTO match_player_stats (match_id, guild_id, user_id, mode, captures, breaks, kills, deaths, assists, damage, score, rating_before, rating_delta, mu_before, sigma_before, mu_after, sigma_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (match_id, guild_id, user_id, mode, values.get("captures", 0), values.get("breaks", 0), values.get("kills", 0), values.get("deaths", 0), values.get("assists", 0), values.get("damage", 0), values.get("score", 0), change.old_rating, final_rating - change.old_rating, change.old_mu, change.old_sigma, change.new_mu, change.new_sigma),
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
                INSERT INTO ratings (guild_id, user_id, mode, rating, wins, losses, games, current_streak, best_streak, provisional_games, peak_rating, mu, sigma, skill_rating, gow2_rank)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, mode) DO UPDATE SET
                    rating=excluded.rating, mu=excluded.mu, sigma=excluded.sigma, skill_rating=excluded.skill_rating, gow2_rank=excluded.gow2_rank, wins=wins+excluded.wins,
                    losses=losses+excluded.losses, games=games+1,
                    current_streak=CASE WHEN excluded.wins=1 THEN current_streak+1 ELSE 0 END,
                    best_streak=MAX(best_streak, CASE WHEN excluded.wins=1 THEN current_streak+1 ELSE 0 END),
                    provisional_games=MAX(0, provisional_games-1),
                    peak_rating=MAX(peak_rating, excluded.rating)
                """,
                (guild_id, change.user_id, mode, max(settings["rating_floor"], change.new_rating), int(did_win), int(not did_win), int(did_win), int(did_win), max(0, settings["provisional_games"] - 1), max(settings["rating_floor"], change.new_rating), change.new_mu, change.new_sigma, max(settings["rating_floor"], change.new_rating), gow2_rank(max(settings["rating_floor"], change.new_rating))[0]),
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
                if row["mu_before"] is not None:
                    restored_rating = round(trueskill_display(row["mu_before"], row["sigma_before"]))
                    self.connection.execute(
                        "UPDATE ratings SET rating=?, skill_rating=?, gow2_rank=?, mu=?, sigma=?, wins=wins-?, losses=losses-?, games=games-1, current_streak=MAX(0, current_streak-?) WHERE guild_id=? AND user_id=? AND mode=?",
                        (restored_rating, restored_rating, gow2_rank(restored_rating)[0], row["mu_before"], row["sigma_before"], int(won), int(not won), int(won), guild_id, row["user_id"], match["mode"]),
                    )
                else:
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

    def leaderboard(self, guild_id: int, mode: str, metric: str = "rating", limit: int = 10, min_games: int = 0, offset: int = 0):
        allowed = {"rating": "r.rating", "wins": "r.wins", "winrate": "CAST(r.wins AS REAL) / NULLIF(r.games, 0)", "kills": "COALESCE(SUM(s.kills), 0)", "damage": "COALESCE(SUM(s.damage), 0)", "score": "COALESCE(SUM(s.score), 0)", "assists": "COALESCE(SUM(s.assists), 0)"}
        order = allowed.get(metric, allowed["rating"])
        return self.connection.execute(
            f"SELECT r.user_id, r.rating, r.wins, r.losses, r.games, COALESCE(SUM(s.kills), 0) AS kills, COALESCE(SUM(s.damage), 0) AS damage, COALESCE(SUM(s.score), 0) AS score, COALESCE(SUM(s.assists), 0) AS assists FROM ratings r LEFT JOIN match_player_stats s ON s.guild_id=r.guild_id AND s.user_id=r.user_id AND s.mode=r.mode WHERE r.guild_id=? AND r.mode=? AND r.games>=? GROUP BY r.user_id, r.rating, r.wins, r.losses, r.games ORDER BY {order} DESC, r.wins DESC LIMIT ? OFFSET ?",
            (guild_id, mode, max(0, min_games), max(1, limit), max(0, offset)),
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
            # Remove commands registered by older versions before copying the
            # current grouped tree. Discord does not automatically delete
            # stale guild-scoped commands when a command is renamed or moved
            # beneath a group.
            self.tree.clear_commands(guild=guild)
            # A single bulk sync removes stale command IDs and installs the
            # current grouped tree without triggering an extra API rate limit.
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Slash commands synced to server {GUILD_ID} ({len(synced)} top-level commands).")
        else:
            await self.tree.sync()

    async def close(self):
        self.database.close()
        await super().close()


bot = GearsEloBot()

mode_choices = [app_commands.Choice(name=str(info["label"]), value=mode) for mode, info in MODES.items()]
queues: dict[tuple[int, str], list[int]] = {}


async def map_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    rows = bot.database.connection.execute("SELECT DISTINCT map_name FROM matches WHERE guild_id=? AND map_name<>'' AND map_name<>'Unknown' AND map_name LIKE ? ORDER BY map_name LIMIT 25", (interaction.guild_id, f"%{current}%")).fetchall()
    return [app_commands.Choice(name=row["map_name"][:100], value=row["map_name"][:100]) for row in rows]


async def match_id_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    try:
        search = int(current) if current else 0
    except ValueError:
        search = 0
    rows = bot.database.connection.execute("SELECT id, mode, map_name FROM matches WHERE guild_id=? AND (?=0 OR id=?) ORDER BY id DESC LIMIT 25", (interaction.guild_id, search, search)).fetchall()
    return [app_commands.Choice(name=f"#{row['id']} · {mode_label(row['mode'])} · {row['map_name']}"[:100], value=str(row["id"])) for row in rows]

# Keep the Discord command tree compact. Discord treats these as the only
# top-level commands; the existing features live underneath relevant groups.
match_group = app_commands.Group(name="match", description="Record and manage match results")
stats_group = app_commands.Group(name="stats", description="Player, team, and leaderboard statistics")
team_group = app_commands.Group(name="team", description="Build, save, and compare teams")
queue_group = app_commands.Group(name="queue", description="Find players and coordinate lobbies")
season_group = app_commands.Group(name="season", description="Manage competitive seasons")
tournament_group = app_commands.Group(name="tournament", description="Run tournament brackets")
player_group = app_commands.Group(name="player", description="Manage player profiles and records")
admin_group = app_commands.Group(name="admin", description="Server administration")
maps_group = app_commands.Group(name="maps", description="Maps, rotations, and vetoes")
challenge_group = app_commands.Group(name="challenge", description="Player challenges")
series_group = app_commands.Group(name="series", description="Track best-of series")
lobby_group = app_commands.Group(name="lobby", description="Match lobbies and check-ins")
server_group = app_commands.Group(name="server", description="Server tools and bot information")
insights_group = app_commands.Group(name="insights", description="Additional match and player analytics")
ops_group = app_commands.Group(name="ops", description="Server activity and operational summaries")
reports_group = app_commands.Group(name="reports", description="Detailed server reports")
tools_group = app_commands.Group(name="tools", description="Quick bot tools and diagnostics")
analytics_group = app_commands.Group(name="analytics", description="Advanced derived match analytics")
community_group = app_commands.Group(name="community", description="Server activity and coordination views")
matchroom_group = app_commands.Group(name="matchroom", description="Live match-room and coordination views")
career_group = app_commands.Group(name="career", description="Your personal competitive record")
for _group in (match_group, stats_group, team_group, queue_group, season_group, tournament_group, player_group, admin_group, maps_group, challenge_group, series_group, lobby_group, server_group, insights_group, ops_group, reports_group, tools_group, analytics_group, community_group, matchroom_group, career_group):
    bot.tree.add_command(_group)


def has_command_access(interaction: discord.Interaction, command_name: str) -> bool:
    role_id = bot.database.command_role(interaction.guild_id, command_name)
    if not role_id or interaction.user.guild_permissions.manage_guild:
        return True
    return any(role.id == role_id for role in getattr(interaction.user, "roles", []))


async def send_response(interaction: discord.Interaction, *args, **kwargs):
    """Send the initial response or a follow-up after an early defer."""
    if interaction.response.is_done():
        return await interaction.followup.send(*args, **kwargs)
    return await interaction.response.send_message(*args, **kwargs)


def rating_division(rating: int) -> str:
    """Map Elo to a familiar competitive division."""
    if rating >= 1600:
        return "Master"
    if rating >= 1400:
        return "Onyx"
    if rating >= 1200:
        return "Gold"
    if rating >= 1000:
        return "Silver"
    return "Bronze"


class LeaderboardView(discord.ui.View):
    def __init__(self, guild_id: int, mode: str, metric: str, min_games: int = 0, page: int = 1):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.mode = mode
        self.metric = metric
        self.min_games = min_games
        self.page = page

    async def render(self, interaction: discord.Interaction):
        rows = bot.database.leaderboard(self.guild_id, self.mode, self.metric, 10, self.min_games, (self.page - 1) * 10)
        if not rows:
            await interaction.response.send_message("There are no more players on that leaderboard page.", ephemeral=True)
            return
        value_key = "rating" if self.metric == "rating" else self.metric
        embed = discord.Embed(title=f"{mode_label(self.mode)} leaderboard", description=f"Sorted by **{self.metric}** · Page **{self.page}**" + (f" · Minimum games: **{self.min_games}**" if self.min_games else ""), colour=discord.Colour.red())
        values = []
        for index, row in enumerate(rows, (self.page - 1) * 10 + 1):
            value = row[value_key] if value_key != "winrate" else f"{row['wins'] / row['games'] * 100:.1f}%"
            values.append(f"**{index}.** <@{row['user_id']}> — {row['rating']} Elo · {row['wins']}-{row['losses']} · {value}")
        embed.description += "\n\n" + "\n".join(values)
        self.previous.disabled = self.page <= 1
        self.next.disabled = len(rows) < 10
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 1:
            self.page -= 1
        await self.render(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.render(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.render(interaction)


class RatingRollbackView(discord.ui.View):
    def __init__(self, guild_id: int, adjustment_id: int, actor_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.adjustment_id = adjustment_id
        self.actor_id = actor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the administrator who started this rollback can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm rollback", style=discord.ButtonStyle.danger, emoji="↩️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            row = bot.database.rollback_rating_adjustment(self.guild_id, self.adjustment_id)
        except ValueError as error:
            await interaction.response.edit_message(content=f"Rollback refused: {error}", view=None)
            return
        bot.database.audit(self.guild_id, interaction.user.id, "manual_elo_rollback", f"adjustment=#{self.adjustment_id}; player={row['user_id']}; mode={row['mode']}")
        await update_elo_role(interaction.guild, row["user_id"], row["mode"], row["before_rating"])
        await interaction.response.edit_message(content=f"Rolled back adjustment **#{self.adjustment_id}**. <@{row['user_id']}> is now **{row['before_rating']} Elo** in **{mode_label(row['mode'])}**.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Rollback cancelled.", view=None)

def elo_tier(rating: int):
    tier = ELO_TIERS[0]
    for candidate in ELO_TIERS:
        if rating >= candidate[0]:
            tier = candidate
    return tier


def rank_asset_path(rank_number: int) -> Path | None:
    """Find optional user-supplied GoW2 rank art without downloading or committing it."""
    candidates = (
        Path(__file__).with_name("assets") / "ranks" / f"rank-{rank_number}.png",
        Path(__file__).with_name("assets") / "ranks" / f"{rank_number}.png",
        Path(__file__).with_name("ranks") / f"rank-{rank_number}.png",
        Path(__file__).with_name("ranks") / f"{rank_number}.png",
        Path(__file__).with_name(f"rank-{rank_number}.png"),
        Path(__file__).with_name(f"{rank_number}.png"),
    )
    return next((path for path in candidates if path.is_file()), None)


def prepare_rank_badge(plt, asset: Path):
    """Load a rank image, removing a simple opaque background when possible.

    Rank art is deliberately user-supplied. This best-effort cleanup handles the
    common screenshot-style assets with a dark/wooden background while leaving
    unusual or transparent artwork usable. The source file is never modified.
    """
    import numpy as np

    image = plt.imread(asset)
    pixels = image.astype(float)
    if pixels.max() > 1.0:
        pixels /= 255.0
    if pixels.ndim == 2:
        pixels = np.repeat(pixels[:, :, None], 3, axis=2)
    if pixels.shape[2] == 3:
        pixels = np.concatenate((pixels, np.ones((*pixels.shape[:2], 1))), axis=2)
    rgb = pixels[:, :, :3]
    alpha = pixels[:, :, 3]

    # Estimate the background from the perimeter. Foreground symbols generally
    # have a stronger contrast than the textured perimeter around them.
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    reference = np.median(border, axis=0)
    distance = np.linalg.norm(rgb - reference, axis=2)
    border_distance = np.linalg.norm(border - reference, axis=1)
    threshold = max(0.14, float(np.percentile(border_distance, 90)) * 2.0)
    foreground = (distance > threshold) & (alpha > 0.05)

    # Bright metallic symbols are often close in hue to wood backgrounds, so
    # retain unusually bright pixels as a second conservative signal.
    luminance = (rgb * (0.2126, 0.7152, 0.0722)).sum(axis=2)
    foreground |= (luminance > float(np.percentile(border[:, :3] @ (0.2126, 0.7152, 0.0722), 97))) & (alpha > 0.05)

    if foreground.sum() < 8:
        return pixels
    ys, xs = np.where(foreground)
    padding = max(2, int(min(pixels.shape[:2]) * 0.025))
    top, bottom = max(0, ys.min() - padding), min(pixels.shape[0], ys.max() + padding + 1)
    left, right = max(0, xs.min() - padding), min(pixels.shape[1], xs.max() + padding + 1)
    cropped = pixels[top:bottom, left:right].copy()
    local_mask = foreground[top:bottom, left:right]
    cropped[:, :, 3] *= local_mask.astype(float)
    return cropped


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


async def notify_webhook(guild_id: int, content: str):
    setting = bot.database.webhook(guild_id)
    if not setting:
        return
    payload = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(setting["webhook_url"], data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        await asyncio.to_thread(urllib.request.urlopen, request, timeout=10)
    except (OSError, ValueError):
        return


def player_labels(guild: discord.Guild, player_ids: list[int]) -> dict[int, str]:
    """Resolve friendly cached Discord names without delaying the interaction."""
    labels = {}
    for index, player_id in enumerate(player_ids, 1):
        member = guild.get_member(player_id)
        if member is None:
            labels[player_id] = f"Player {index}"
        else:
            labels[player_id] = f"{member.display_name} (@{member.name})"
    return labels


async def fetch_player_labels(guild: discord.Guild, player_ids: list[int]) -> dict[int, str]:
    """Resolve card names from cache, then Discord, without failing the card."""
    labels = player_labels(guild, player_ids)
    for index, player_id in enumerate(player_ids, 1):
        if not labels[player_id].startswith("Player "):
            continue
        try:
            member = await guild.fetch_member(player_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            continue
        labels[player_id] = f"{member.display_name} (@{member.name})"
    return labels


def render_match_card(match: sqlite3.Row, stats: list[sqlite3.Row], labels: dict[int, str]) -> BytesIO:
    """Render a shareable Gears-themed match snapshot from recorded rows."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stat_columns = list(stat_names(match["mode"]))
    display_columns = ["Player", "Team", "Rank"] + [column.title() for column in stat_columns] + ["Rating Δ"]
    team_one = set(map(int, match["team_one"].split(",")))
    table_rows = []
    for row in stats:
        team = "1" if row["user_id"] in team_one else "2"
        values = [labels.get(row["user_id"], str(row["user_id"])), team, ""]
        values.extend(str(row[column]) for column in stat_columns)
        values.append(f"{row['rating_delta']:+d}")
        table_rows.append(values)

    figure = plt.figure(figsize=(14, 8), facecolor="#111318")
    background_axis = figure.add_axes([0, 0, 1, 1], zorder=0)
    background_axis.axis("off")
    background_paths = (
        Path(__file__).with_name("assets") / "gears-background.jpg",
        Path(__file__).with_name("gears-background.jpg"),
    )
    background_path = next((path for path in background_paths if path.exists()), None)
    if background_path is not None:
        background = plt.imread(background_path)
        background_axis.imshow(background, aspect="auto", zorder=0)
        background_axis.add_patch(plt.Rectangle((0, 0), 1, 1, transform=background_axis.transAxes, facecolor="#090b10", alpha=0.42, zorder=1))
    axis = figure.add_axes([0, 0, 1, 1], zorder=1)
    axis.set_facecolor("none")
    axis.axis("off")
    figure.text(0.05, 0.93, "GEARS 5", color="#d7263d", fontsize=28, fontweight="bold", family="sans-serif")
    figure.text(0.05, 0.875, "PRIVATE MATCH REPORT", color="#f2f2f2", fontsize=18, fontweight="bold")
    figure.text(0.95, 0.93, f"MATCH #{match['id']}", color="#aeb4bf", fontsize=16, ha="right", fontweight="bold")
    figure.text(0.05, 0.82, f"{mode_label(match['mode'])}   •   {match['map_name']}", color="#d7dbe2", fontsize=15)
    figure.text(0.95, 0.82, f"TEAM {match['winner']} WINS", color="#d7263d", fontsize=15, ha="right", fontweight="bold")
    name_width = 0.30 if len(display_columns) > 8 else 0.38
    team_width = 0.06
    rank_width = 0.10
    elo_width = 0.10
    stat_width = (1 - name_width - team_width - rank_width - elo_width) / len(stat_columns)
    column_widths = [name_width, team_width, rank_width] + [stat_width] * len(stat_columns) + [elo_width]
    table = axis.table(cellText=table_rows, colLabels=display_columns, cellLoc="center", colLoc="center", colWidths=column_widths, bbox=[0.03, 0.12, 0.94, 0.61])
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.65)
    for (row_index, column_index), cell in table.get_celld().items():
        cell.set_edgecolor("#363b45")
        if row_index == 0:
            cell.set_facecolor("#d7263d")
            cell.set_text_props(color="white", weight="bold")
        else:
            cell.set_facecolor("#20242c" if row_index % 2 else "#171a20")
            cell.set_text_props(color="#eef0f4")
            if column_index == 0:
                cell.set_text_props(color="#eef0f4", ha="left")
            if column_index == 1:
                cell.set_text_props(color="#d7263d", weight="bold")
    # Optional private rank artwork centered in the Rank column. Rank cells are
    # intentionally image-only; missing assets remain blank rather than adding
    # a text fallback that changes the card layout.
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage
    rank_x = 0.03 + 0.94 * (name_width + team_width + rank_width / 2)
    for index, row in enumerate(stats, 1):
        rank_number, _ = gow2_rank(row["rating_before"] + row["rating_delta"])
        asset = rank_asset_path(rank_number)
        if asset:
            try:
                icon = prepare_rank_badge(plt, asset)
                zoom = min(0.12, 22 / max(icon.shape[:2]))
                y = 0.12 + 0.61 * (1 - (index + 0.5) / (len(stats) + 1))
                axis.add_artist(AnnotationBbox(OffsetImage(icon, zoom=zoom), (rank_x, y), xycoords=axis.transAxes, frameon=False, pad=0))
            except (OSError, ValueError):
                pass
    figure.text(0.05, 0.055, "Gears 5 Elo Bot  •  Private matches between friends  •  Artwork: OutNow.ch", color="#aeb4bf", fontsize=9)
    image = BytesIO()
    figure.savefig(image, format="png", dpi=150, facecolor=figure.get_facecolor(), bbox_inches="tight")
    plt.close(figure)
    image.seek(0)
    return image


class PlayerStatsModal(discord.ui.Modal):
    def __init__(self, mode: str, winner: int, team_one: list[int], team_two: list[int], player_ids: list[int], stats: dict[int, dict[str, int]], index: int, map_name: str, labels: dict[int, str]):
        self.mode = mode
        self.winner = winner
        self.team_one = team_one
        self.team_two = team_two
        self.player_ids = player_ids
        self.stats = stats
        self.index = index
        self.map_name = map_name
        self.labels = labels
        player_id = player_ids[index]
        name = labels.get(player_id, f"Player {index + 1}")
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
            next_player = self.labels.get(self.player_ids[next_index], f"Player {next_index + 1}")
            await interaction.response.send_message(
                f"✅ Saved stats for **{self.labels.get(player_id, f'Player {self.index + 1}')}**. "
                f"Click below to enter stats for **{next_player}**.",
                ephemeral=True,
                view=NextPlayerStatsView(self.mode, self.winner, self.team_one, self.team_two, self.player_ids, self.stats, next_index, self.map_name, self.labels),
            )
            return

        try:
            pending_id = bot.database.create_pending_match(interaction.guild_id, self.mode, self.winner, self.team_one, self.team_two, self.stats, interaction.user.id, self.map_name)
        except sqlite3.Error as error:
            await interaction.response.send_message(f"Could not save match for confirmation: {error}", ephemeral=True)
            return
        await interaction.response.send_message(f"📝 Match **#{pending_id}** is ready for confirmation. One player from each team must use `/match confirm match_id:{pending_id}`. Use `/match cancel match_id:{pending_id}` to discard it.")


class NextPlayerStatsView(discord.ui.View):
    """Button bridge between stat modals; Discord forbids modal-to-modal responses."""

    def __init__(self, mode: str, winner: int, team_one: list[int], team_two: list[int], player_ids: list[int], stats: dict[int, dict[str, int]], index: int, map_name: str, labels: dict[int, str]):
        super().__init__(timeout=600)
        self.mode = mode
        self.winner = winner
        self.team_one = team_one
        self.team_two = team_two
        self.player_ids = player_ids
        self.stats = stats
        self.index = index
        self.map_name = map_name
        self.labels = labels

    @discord.ui.button(label="Enter next player's stats", style=discord.ButtonStyle.primary)
    async def open_next_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            PlayerStatsModal(self.mode, self.winner, self.team_one, self.team_two, self.player_ids, self.stats, self.index, self.map_name, self.labels)
        )


@server_group.command(name="modes", description="Show the Gears 5 modes tracked by this bot")
async def modes(interaction: discord.Interaction):
    lines = [f"• {mode_label(mode)} — {team_size(mode)}v{team_size(mode)}" for mode in MODES]
    await interaction.response.send_message("**Tracked modes**\n" + "\n".join(lines))


@admin_group.command(name="settings", description="Show Elo settings for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def settings(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    row = bot.database.elo_settings(interaction.guild_id, mode.value)
    await interaction.response.send_message(f"**{mode_label(mode.value)} TrueSkill settings**\nStarting displayed rating: **{row['starting_rating']}**\nLegacy K-factor: **{row['k_factor']}** (not used by TrueSkill updates)\nRating floor: **{row['rating_floor']}**\nProvisional games: **{row['provisional_games']}**\nTrueSkill uses skill uncertainty to size each result change.")


@admin_group.command(name="setelo", description="Set starting rating and K-factor for a mode")
@app_commands.describe(mode="Game mode", starting_rating="Starting rating for new players", k_factor="How quickly ratings move", rating_floor="Lowest allowed rating", provisional_games="Games before a rating is established")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(manage_guild=True)
async def setelo(interaction: discord.Interaction, mode: app_commands.Choice[str], starting_rating: int, k_factor: int, rating_floor: int = 0, provisional_games: int = 5):
    if not 100 <= starting_rating <= 5000 or not 1 <= k_factor <= 100:
        await interaction.response.send_message("Starting rating must be 100–5000 and K-factor must be 1–100.", ephemeral=True)
        return
    if not 0 <= rating_floor <= starting_rating or not 0 <= provisional_games <= 50:
        await interaction.response.send_message("Rating floor must be between 0 and the starting rating; provisional games must be 0–50.", ephemeral=True)
        return
    bot.database.set_elo_settings(interaction.guild_id, mode.value, starting_rating, k_factor, rating_floor, provisional_games)
    await interaction.response.send_message(f"Updated **{mode_label(mode.value)}**: starting rating **{starting_rating}**, K-factor **{k_factor}**, floor **{rating_floor}**, provisional games **{provisional_games}**.")


async def _manual_elo_adjust(interaction: discord.Interaction, player: discord.Member, mode: app_commands.Choice[str], amount: int, reason: str, sign: int):
    if not 1 <= amount <= 1000:
        await interaction.response.send_message("The adjustment amount must be between 1 and 1,000.", ephemeral=True)
        return
    reason = reason.strip()
    if not reason:
        await interaction.response.send_message("A reason is required for manual rating changes.", ephemeral=True)
        return
    try:
        old_rating, new_rating, rank_number, rank_name = bot.database.adjust_rating(interaction.guild_id, player.id, mode.value, sign * amount, interaction.user.id, reason)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    applied = new_rating - old_rating
    bot.database.audit(interaction.guild_id, interaction.user.id, "manual_elo_adjustment", f"player={player.id}; mode={mode.value}; delta={applied:+d}; {reason[:500]}")
    await update_elo_role(interaction.guild, player.id, mode.value, new_rating)
    floor_note = " (rating floor reached)" if applied != sign * amount else ""
    await interaction.response.send_message(f"Updated {player.mention} in **{mode_label(mode.value)}**: **{old_rating} → {new_rating}** ({applied:+d}) · Rank {rank_number} — **{rank_name}**{floor_note}.", ephemeral=True)


@admin_group.command(name="elo_add", description="Add Elo to a player (administrator only)")
@app_commands.describe(player="Player to adjust", mode="Game mode", amount="Displayed rating points to add", reason="Why the adjustment is being made")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(administrator=True)
async def elo_add(interaction: discord.Interaction, player: discord.Member, mode: app_commands.Choice[str], amount: int, reason: str):
    await _manual_elo_adjust(interaction, player, mode, amount, reason, 1)


@admin_group.command(name="elo_subtract", description="Subtract Elo from a player (administrator only)")
@app_commands.describe(player="Player to adjust", mode="Game mode", amount="Displayed rating points to subtract", reason="Why the adjustment is being made")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(administrator=True)
async def elo_subtract(interaction: discord.Interaction, player: discord.Member, mode: app_commands.Choice[str], amount: int, reason: str):
    await _manual_elo_adjust(interaction, player, mode, amount, reason, -1)


@admin_group.command(name="elo_history", description="Show manual Elo adjustments (administrator only)")
@app_commands.describe(player="Optional player filter", mode="Optional game mode filter", limit="Number of adjustments to show")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(administrator=True)
async def elo_history(interaction: discord.Interaction, player: discord.Member | None = None, mode: app_commands.Choice[str] | None = None, limit: int = 10):
    rows = bot.database.rating_adjustments(interaction.guild_id, player.id if player else None, mode.value if mode else None, limit)
    if not rows:
        await interaction.response.send_message("No manual Elo adjustments were found.", ephemeral=True)
        return
    lines = [f"**#{row['id']}** <@{row['user_id']}> · {mode_label(row['mode'])} · **{row['before_rating']} → {row['after_rating']}** · <@{row['actor_id']}> · {row['reason']}" for row in rows]
    await interaction.response.send_message("**Manual Elo history**\n" + "\n".join(lines), ephemeral=True)


@admin_group.command(name="elo_rollback", description="Roll back a manual Elo adjustment (administrator only)")
@app_commands.describe(adjustment_id="Adjustment number from /admin elo_history")
@app_commands.checks.has_permissions(administrator=True)
async def elo_rollback(interaction: discord.Interaction, adjustment_id: int):
    row = bot.database.rating_adjustment(interaction.guild_id, adjustment_id)
    if not row:
        await interaction.response.send_message("That rating adjustment was not found in this server.", ephemeral=True)
        return
    if row["rolled_back"]:
        await interaction.response.send_message("That rating adjustment has already been rolled back.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Confirm rollback of adjustment **#{adjustment_id}** for <@{row['user_id']}> in **{mode_label(row['mode'])}**?\n"
        f"This will change the rating from **{row['after_rating']}** back to **{row['before_rating']}**.",
        view=RatingRollbackView(interaction.guild_id, adjustment_id, interaction.user.id),
        ephemeral=True,
    )


@admin_group.command(name="roles_setup", description="Create Elo tier roles for a mode")
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


@team_group.command(name="balance", description="Create balanced teams from a player list")
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


@queue_group.command(name="join", description="Join the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_join(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not bot.database.queue_add(interaction.guild_id, mode.value, interaction.user.id):
        await interaction.response.send_message("You are already in that queue.", ephemeral=True)
        return
    needed = team_size(mode.value) * 2
    queue = bot.database.queue_players(interaction.guild_id, mode.value)
    if len(queue) < needed:
        await interaction.response.send_message(f"<@{interaction.user.id}> joined **{mode_label(mode.value)}** queue ({len(queue)}/{needed}).")
        return
    players = queue[:needed]
    bot.database.queue_take(interaction.guild_id, mode.value, players)
    rated = [(player_id, bot.database.get_rating(interaction.guild_id, player_id, mode.value)) for player_id in players]
    team_one, team_two = balance_teams(rated)
    await interaction.response.send_message(f"**{mode_label(mode.value)} lobby ready!**\nTeam 1: {' + '.join(f'<@{player_id}>' for player_id in team_one)}\nTeam 2: {' + '.join(f'<@{player_id}>' for player_id in team_two)}\nUse `/match` to record the result.")


@queue_group.command(name="leave", description="Leave the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_leave(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not bot.database.queue_remove(interaction.guild_id, mode.value, interaction.user.id):
        await interaction.response.send_message("You are not in that queue.", ephemeral=True)
        return
    await interaction.response.send_message(f"<@{interaction.user.id}> left **{mode_label(mode.value)}** queue.")


@queue_group.command(name="status", description="Show the matchmaking queue for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def queue_status(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    queue = bot.database.queue_players(interaction.guild_id, mode.value)
    needed = team_size(mode.value) * 2
    names = ", ".join(f"<@{player_id}>" for player_id in queue) or "Nobody"
    await interaction.response.send_message(f"**{mode_label(mode.value)} queue** ({len(queue)}/{needed})\n{names}")


@team_group.command(name="random_teams", description="Randomize a lobby into two teams")
@app_commands.describe(mode="Game mode", players="Comma-separated players")
@app_commands.choices(mode=mode_choices)
async def random_teams(interaction: discord.Interaction, mode: app_commands.Choice[str], players: str):
    try:
        roster = parse_player_list(players, team_size(mode.value) * 2, team_size(mode.value) * 2)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    random.shuffle(roster)
    size = len(roster) // 2
    await interaction.response.send_message(f"**Random teams — {mode_label(mode.value)}**\nTeam 1: " + " + ".join(f"<@{x}>" for x in roster[:size]) + "\nTeam 2: " + " + ".join(f"<@{x}>" for x in roster[size:]))


@team_group.command(name="draft_start", description="Start a captain-style player draft")
@app_commands.describe(mode="Game mode", players="Comma-separated players")
@app_commands.choices(mode=mode_choices)
async def draft_start(interaction: discord.Interaction, mode: app_commands.Choice[str], players: str):
    try:
        roster = parse_player_list(players, team_size(mode.value) * 2, team_size(mode.value) * 2)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    draft_id = bot.database.create_draft(interaction.guild_id, mode.value, roster, interaction.user.id)
    await interaction.response.send_message(f"🎯 Draft **#{draft_id}** started for {mode_label(mode.value)} with {len(roster)} players. Team 1 picks first using `/draft_pick draft_id:{draft_id} player:@player`.")


@team_group.command(name="draft_pick", description="Make a pick in an active draft")
@app_commands.describe(draft_id="Draft number", player="Player to pick")
async def draft_pick(interaction: discord.Interaction, draft_id: int, player: discord.Member):
    row = bot.database.draft(interaction.guild_id, draft_id)
    if not row or row["status"] != "open":
        await interaction.response.send_message("That draft is not open.", ephemeral=True)
        return
    available = set(json.loads(row["players"]))
    team_one = json.loads(row["team_one"]); team_two = json.loads(row["team_two"])
    if player.id not in available or player.id in team_one or player.id in team_two:
        await interaction.response.send_message("That player is not available in this draft.", ephemeral=True)
        return
    target = team_one if row["turn"] == 1 else team_two
    target.append(player.id)
    completed = len(target) == team_size(row["mode"])
    next_turn = 2 if row["turn"] == 1 else 1
    status = "complete" if len(team_one) + len(team_two) == len(available) else "open"
    bot.database.update_draft(interaction.guild_id, draft_id, team_one, team_two, next_turn, status)
    await interaction.response.send_message(f"Team {row['turn']} picked {player.mention}. " + (f"Teams complete: {' + '.join(f'<@{x}>' for x in team_one)} vs {' + '.join(f'<@{x}>' for x in team_two)}" if status == "complete" else f"Team {next_turn} picks next."))


@team_group.command(name="draft_suggest", description="Suggest captain draft picks by Elo")
@app_commands.describe(mode="Game mode", players="Comma-separated players")
@app_commands.choices(mode=mode_choices)
async def draft_suggest(interaction: discord.Interaction, mode: app_commands.Choice[str], players: str):
    try:
        roster = parse_player_list(players, team_size(mode.value) * 2, team_size(mode.value) * 2)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    rated = sorted(((player_id, bot.database.get_rating(interaction.guild_id, player_id, mode.value)) for player_id in roster), key=lambda item: item[1], reverse=True)
    await interaction.response.send_message("**Suggested snake-draft order**\n" + "\n".join(f"{index}. <@{player_id}> — {rating} Elo" for index, (player_id, rating) in enumerate(rated, 1)))


@season_group.command(name="status", description="Show the active season")
async def season(interaction: discord.Interaction):
    active = bot.database.active_season(interaction.guild_id)
    if not active:
        await interaction.response.send_message("There is no active season. A manager can use `/season_start` to begin one.")
        return
    await interaction.response.send_message(f"**Active season:** {active['name']}\nStarted: {active['started_at'][:10]}\nNew matches are being recorded in this season.")


@season_group.command(name="standings", description="Show season divisions and promotion or relegation positions")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def season_standings(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    active = bot.database.active_season(interaction.guild_id)
    if not active:
        await interaction.response.send_message("There is no active season.", ephemeral=True)
        return
    rows = bot.database.season_standings(interaction.guild_id, active["id"], mode.value)
    if not rows:
        await interaction.response.send_message(f"No **{mode_label(mode.value)}** matches have been recorded in **{active['name']}** yet.")
        return
    promotion_count = max(1, len(rows) // 5) if len(rows) >= 5 else 0
    lines = []
    for index, row in enumerate(rows, 1):
        member = interaction.guild.get_member(row["user_id"])
        name = member.display_name if member else f"<@{row['user_id']}>"
        movement = " · PROMOTE" if promotion_count and index <= promotion_count else " · RELEGATE" if promotion_count and index > len(rows) - promotion_count else ""
        lines.append(f"**{index}.** {name} — {rating_division(row['rating'] or 1000)} **{row['rating'] or 1000}** · {row['wins']}-{row['losses']} · {row['games']} games{movement}")
    note = "Top and bottom 20% move divisions after the season." if promotion_count else "At least five players are needed to show promotion and relegation positions."
    await interaction.response.send_message(f"🏆 **{active['name']} — {mode_label(mode.value)} standings**\n" + "\n".join(lines) + f"\n\n_{note}_")


@season_group.command(name="placements", description="Show provisional placement progress for a player")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def season_placements(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    player = player or interaction.user
    row = bot.database.connection.execute("SELECT games, wins, losses, rating, provisional_games FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (interaction.guild_id, player.id, mode.value)).fetchone()
    required = bot.database.elo_settings(interaction.guild_id, mode.value)["provisional_games"]
    if not row:
        await interaction.response.send_message(f"{player.mention} has not played a **{mode_label(mode.value)}** placement match yet.")
        return
    completed = max(0, required - row["provisional_games"])
    status = "Established" if row["provisional_games"] == 0 else "Provisional"
    await interaction.response.send_message(f"**{player.display_name} placement status — {mode_label(mode.value)}**\nStatus: **{status}**\nProgress: **{completed}/{required}** placement games\nRecord: **{row['wins']}-{row['losses']}**\nCurrent rating: **{row['rating']}** ({rating_division(row['rating'])})")


@season_group.command(name="start", description="Start a named season")
@app_commands.describe(name="Season name, such as Season 1")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_start(interaction: discord.Interaction, name: str):
    try:
        active = bot.database.start_season(interaction.guild_id, name)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(f"Started **{active['name']}**. Future matches will be tagged to this season.")


@season_group.command(name="end", description="End the active season")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_end(interaction: discord.Interaction):
    try:
        ended = bot.database.end_season(interaction.guild_id)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    message = f"Ended **{ended['name']}**. Start another season with `/season_start` when ready."
    await interaction.response.send_message(message)
    setting = bot.database.server_settings(interaction.guild_id)
    channel = bot.get_channel(setting["announcement_channel_id"]) if setting["announcement_channel_id"] else None
    if channel and channel.id != interaction.channel_id:
        await channel.send(f"🏁 Season **{ended['name']}** has ended. Final standings remain available in the dashboard.")


@season_group.command(name="reset", description="Reset current ratings for a fresh season")
@app_commands.checks.has_permissions(manage_guild=True)
async def season_reset(interaction: discord.Interaction):
    bot.database.reset_ratings(interaction.guild_id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "season_reset", "ratings reset; match history preserved")
    await interaction.response.send_message("✅ Current ratings and season records were reset. Historical matches remain available through `/history` and `/myhistory`.")


@tournament_group.command(name="create", description="Create a tournament registration")
@app_commands.describe(name="Tournament name", mode="Game mode", format="Tournament format")
@app_commands.choices(mode=mode_choices, format=[app_commands.Choice(name="Single elimination", value="single"), app_commands.Choice(name="Double elimination", value="double"), app_commands.Choice(name="Round robin", value="round_robin")])
async def tournament_create(interaction: discord.Interaction, name: str, mode: app_commands.Choice[str], format: app_commands.Choice[str]):
    tournament_id = bot.database.create_tournament(interaction.guild_id, name, mode.value, format.value, interaction.user.id)
    await interaction.response.send_message(f"🏆 Created **{name}** tournament **#{tournament_id}** ({format.name}). Players can register with `/tournament_join tournament_id:{tournament_id}`.")


@tournament_group.command(name="join", description="Register for a tournament")
@app_commands.describe(tournament_id="Tournament number", team_name="Optional team name")
async def tournament_join(interaction: discord.Interaction, tournament_id: int, team_name: str = ""):
    tournament = bot.database.tournament(interaction.guild_id, tournament_id)
    if not tournament or tournament["status"] != "registration":
        await interaction.response.send_message("That tournament is not accepting registrations.", ephemeral=True)
        return
    bot.database.tournament_join(tournament_id, interaction.user.id, team_name)
    await interaction.response.send_message(f"Registered {interaction.user.mention} for **{tournament['name']}**.")


@tournament_group.command(name="start", description="Generate a tournament bracket")
@app_commands.describe(tournament_id="Tournament number")
@app_commands.checks.has_permissions(manage_guild=True)
async def tournament_start(interaction: discord.Interaction, tournament_id: int):
    tournament = bot.database.tournament(interaction.guild_id, tournament_id)
    if not tournament or tournament["status"] != "registration":
        await interaction.response.send_message("That tournament is not available to start.", ephemeral=True)
        return
    entries = [row["user_id"] for row in bot.database.tournament_entries(tournament_id)]
    if len(entries) < 2:
        await interaction.response.send_message("At least two players or teams are required.", ephemeral=True)
        return
    bracket = []
    if tournament["format"] == "round_robin":
        for index, first in enumerate(entries):
            for second in entries[index + 1:]:
                bracket.append({"round": 1, "team_one": [first], "team_two": [second], "status": "open"})
    else:
        for index in range(0, len(entries) - 1, 2):
            bracket.append({"round": 1, "team_one": [entries[index]], "team_two": [entries[index + 1]], "status": "open"})
    bot.database.set_tournament_bracket(tournament_id, bracket)
    await interaction.response.send_message(f"✅ Started **{tournament['name']}** with {len(bracket)} opening matchup(s).\n" + "\n".join(f"Game {index}: <@{item['team_one'][0]}> vs <@{item['team_two'][0]}>" for index, item in enumerate(bracket, 1)))


@tournament_group.command(name="bracket", description="Show a tournament bracket")
@app_commands.describe(tournament_id="Tournament number")
async def tournament_bracket(interaction: discord.Interaction, tournament_id: int):
    tournament = bot.database.tournament(interaction.guild_id, tournament_id)
    if not tournament:
        await interaction.response.send_message("That tournament was not found.", ephemeral=True)
        return
    bracket = json.loads(tournament["bracket"])
    if not bracket:
        await interaction.response.send_message(f"**{tournament['name']}** is still accepting registrations.")
        return
    await interaction.response.send_message(f"**{tournament['name']} bracket**\n" + "\n".join(f"Round {item['round']}: <@{item['team_one'][0]}> vs <@{item['team_two'][0]}> — {item['status']}" for item in bracket))


@tournament_group.command(name="report", description="Report a tournament matchup and advance the bracket")
@app_commands.describe(tournament_id="Tournament number", game="Bracket game number", winner="Winning side")
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
@app_commands.checks.has_permissions(manage_guild=True)
async def tournament_report(interaction: discord.Interaction, tournament_id: int, game: int, winner: app_commands.Choice[str]):
    tournament = bot.database.tournament(interaction.guild_id, tournament_id)
    if not tournament or tournament["status"] != "active":
        await interaction.response.send_message("That tournament is not active.", ephemeral=True)
        return
    bracket = json.loads(tournament["bracket"])
    index = game - 1
    if index < 0 or index >= len(bracket):
        await interaction.response.send_message("That bracket game does not exist.", ephemeral=True)
        return
    item = bracket[index]
    if item["status"] != "open":
        await interaction.response.send_message("That bracket game has already been reported.", ephemeral=True)
        return
    winning_team = item["team_one"] if winner.value == "1" else item["team_two"]
    item.update(status="complete", winner=int(winner.value), winning_team=winning_team)
    if tournament["format"] == "round_robin":
        finished = all(match["status"] == "complete" for match in bracket)
        status = "completed" if finished else "active"
    else:
        current_round = item["round"]
        round_matches = [match for match in bracket if match["round"] == current_round]
        if all(match["status"] == "complete" for match in round_matches):
            winners = [match["winning_team"] for match in round_matches]
            if len(winners) == 1:
                status = "completed"
            else:
                next_round = current_round + 1
                for offset in range(0, len(winners) - 1, 2):
                    bracket.append({"round": next_round, "team_one": winners[offset], "team_two": winners[offset + 1], "status": "open"})
                status = "active"
        else:
            status = "active"
    bot.database.connection.execute("UPDATE tournaments SET bracket=?, status=? WHERE guild_id=? AND id=?", (json.dumps(bracket), status, interaction.guild_id, tournament_id))
    bot.database.connection.commit()
    result = "Tournament complete — we have a champion!" if status == "completed" else "Bracket advanced; report the next open game with `/tournament report`."
    await interaction.response.send_message(f"✅ Reported game **{game}**: Team {winner.value} wins. {result}")


@stats_group.command(name="teamleaderboard", description="Rank recurring teams in a mode")
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


@player_group.command(name="search", description="Search server members by name")
@app_commands.describe(query="Name fragment")
async def player_search(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    matches = []
    needle = query.lower()
    for member in interaction.guild.members:
        profile = bot.database.profile(interaction.guild_id, member.id)
        aliases = " ".join(json.loads(profile["aliases"])) if profile else ""
        gamertag = profile["gamertag"] if profile else ""
        if needle in f"{member.display_name} {member.name} {gamertag} {aliases}".lower():
            matches.append(member)
    matches = matches[:15]
    if not matches:
        await interaction.response.send_message("No matching players found.")
        return
    await interaction.response.send_message("**Players found**\n" + "\n".join(f"{member.mention} — `{member.id}`" for member in matches))


@player_group.command(name="opponents", description="Show a player's opponent records")
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


@match_group.command(name="attach", description="Attach notes or a replay link to a match")
@app_commands.describe(match_id="Match number", note="Optional match note", replay_url="Optional clip or replay URL")
@app_commands.autocomplete(match_id=match_id_autocomplete)
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


@challenge_group.command(name="create", description="Challenge another player to a 1v1 match")
@app_commands.describe(mode="1v1 game mode", opponent="Player to challenge")
@app_commands.choices(mode=[choice for choice in mode_choices if team_size(choice.value) == 1])
async def challenge(interaction: discord.Interaction, mode: app_commands.Choice[str], opponent: discord.Member):
    if opponent.id == interaction.user.id or opponent.bot:
        await interaction.response.send_message("Choose another human player.", ephemeral=True)
        return
    challenge_id = bot.database.create_challenge(interaction.guild_id, mode.value, interaction.user.id, opponent.id)
    await interaction.response.send_message(f"⚔️ <@{interaction.user.id}> challenged <@{opponent.id}> to **{mode_label(mode.value)}** (challenge **#{challenge_id}**). Use `/challenge_accept challenge_id:{challenge_id}` to accept.")


@challenge_group.command(name="challenge_accept", description="Accept a pending challenge")
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


@challenge_group.command(name="challenge_decline", description="Decline a pending challenge")
@app_commands.describe(challenge_id="Challenge number")
async def challenge_decline(interaction: discord.Interaction, challenge_id: int):
    row = bot.database.challenge(interaction.guild_id, challenge_id)
    if not row or row["opponent_id"] != interaction.user.id:
        await interaction.response.send_message("That challenge was not found for you.", ephemeral=True)
        return
    bot.database.update_challenge(interaction.guild_id, challenge_id, interaction.user.id, "declined")
    await interaction.response.send_message(f"Challenge **#{challenge_id}** declined.")


@admin_group.command(name="captain_set", description="Set the captain for one side of a mode")
@app_commands.describe(mode="Game mode", team="Team side", captain="Player who can confirm for this side")
@app_commands.choices(mode=mode_choices, team=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
@app_commands.checks.has_permissions(manage_guild=True)
async def captain_set(interaction: discord.Interaction, mode: app_commands.Choice[str], team: app_commands.Choice[str], captain: discord.Member):
    bot.database.set_captain(interaction.guild_id, mode.value, int(team.value), captain.id)
    await interaction.response.send_message(f"Set {captain.mention} as Team {team.value} captain for **{mode_label(mode.value)}**.")


@match_group.command(name="confirm", description="Confirm a pending match result")
@app_commands.describe(match_id="Pending match number")
@app_commands.autocomplete(match_id=match_id_autocomplete)
async def match_confirm(interaction: discord.Interaction, match_id: int):
    await interaction.response.defer()
    if not has_command_access(interaction, "match_confirm"):
        await send_response(interaction, "You do not have the role required to confirm matches.", ephemeral=True)
        return
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row:
        await send_response(interaction, "That pending match was not found.", ephemeral=True)
        return
    team_one = [int(value) for value in row["team_one"].split(",")]
    team_two = [int(value) for value in row["team_two"].split(",")]
    team = 1 if interaction.user.id in team_one else 2 if interaction.user.id in team_two else 0
    if not team:
        await send_response(interaction, "Only players in this match can confirm it.", ephemeral=True)
        return
    assigned_captain = bot.database.captain(interaction.guild_id, row["mode"], team)
    if assigned_captain and assigned_captain != interaction.user.id:
        await send_response(interaction, f"Only the assigned Team {team} captain can confirm this result.", ephemeral=True)
        return
    confirmed = set(json.loads(row["confirmed_by"]))
    if interaction.user.id in confirmed:
        await send_response(interaction, "You already confirmed this match.", ephemeral=True)
        return
    row = bot.database.confirm_pending_match(interaction.guild_id, match_id, interaction.user.id)
    confirmed = set(json.loads(row["confirmed_by"]))
    confirmed_teams = {1 if user_id in team_one else 2 for user_id in confirmed}
    if confirmed_teams != {1, 2}:
        await send_response(interaction, f"Confirmation saved ({len(confirmed_teams)}/2 teams). A player from the other team still needs to confirm.")
        return
    await finalize_pending_match(interaction, row, team_one, team_two)


async def finalize_pending_match(interaction: discord.Interaction, row: sqlite3.Row, team_one: list[int], team_two: list[int]):
    """Record a pending result after normal or administrator approval."""
    match_id = row["id"]
    stats = {int(user_id): values for user_id, values in json.loads(row["stats_json"]).items()}
    changes = bot.database.record_match(interaction.guild_id, row["mode"], row["winner"], team_one, team_two, stats, row["created_by"], row["map_name"])
    bot.database.delete_pending_match(interaction.guild_id, match_id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_recorded", f"pending match #{match_id}; mode={row['mode']}")
    change_text = " · ".join(f"<@{change.user_id}> {change.new_rating} ({change.delta:+d})" for change in changes)
    if interaction.guild:
        for change in changes:
            await update_elo_role(interaction.guild, change.user_id, row["mode"], change.new_rating)
    await notify_webhook(interaction.guild_id, f"{mode_label(row['mode'])} match recorded: Team {row['winner']} won (match #{match_id}).")
    await send_response(interaction, f"✅ **{mode_label(row['mode'])} recorded** — Team {row['winner']} wins\n{change_text}\nStats saved for {len(stats)} players.")


@match_group.command(name="force_confirm", description="Admin: record a pending match without waiting for both confirmations")
@app_commands.describe(match_id="Pending match number")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(match_id=match_id_autocomplete)
async def match_force_confirm(interaction: discord.Interaction, match_id: int):
    await interaction.response.defer()
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row:
        await send_response(interaction, "That pending match was not found.", ephemeral=True)
        return
    team_one = [int(value) for value in row["team_one"].split(",")]
    team_two = [int(value) for value in row["team_two"].split(",")]
    await finalize_pending_match(interaction, row, team_one, team_two)


@match_group.command(name="cancel", description="Discard a pending match result")
@app_commands.describe(match_id="Pending match number")
@app_commands.autocomplete(match_id=match_id_autocomplete)
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


@match_group.command(name="record", description="Record a completed private Gears 5 match")
@app_commands.describe(mode="Game mode", winner="Which team won", team_one="Comma-separated mentions/IDs", team_two="Comma-separated mentions/IDs", map_name="Optional map name")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
@app_commands.autocomplete(map_name=map_autocomplete)
async def match(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: app_commands.Choice[str], team_one: str, team_two: str, map_name: str | None = None):
    if not has_command_access(interaction, "match"):
        await interaction.response.send_message("You do not have the role required to submit matches.", ephemeral=True)
        return
    if bot.database.server_settings(interaction.guild_id)["maintenance"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Match submissions are temporarily paused by an administrator.", ephemeral=True)
        return
    try:
        size = team_size(mode.value)
        first = parse_team(team_one, size)
        second = parse_team(team_two, size)
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
        player_ids = first + second
        labels = player_labels(interaction.guild, player_ids)
        await interaction.response.send_modal(PlayerStatsModal(mode.value, int(winner.value), first, second, player_ids, {}, 0, map_name or "Unknown", labels))
        return
    except (ValueError, sqlite3.Error) as error:
        await interaction.response.send_message(f"Could not record match: {error}", ephemeral=True)
        return


@match_group.command(name="rematch", description="Reuse teams from a prior match and enter a new result")
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
    player_ids = first + second
    labels = player_labels(interaction.guild, player_ids)
    await interaction.response.send_modal(PlayerStatsModal(previous["mode"], int(winner.value), first, second, player_ids, {}, 0, previous["map_name"], labels))


@queue_group.command(name="availability", description="Set your availability for finding matches")
@app_commands.describe(status="Your current availability")
@app_commands.choices(status=[app_commands.Choice(name="Available", value="available"), app_commands.Choice(name="Busy", value="busy"), app_commands.Choice(name="Offline", value="offline")])
async def availability(interaction: discord.Interaction, status: app_commands.Choice[str]):
    bot.database.set_availability(interaction.guild_id, interaction.user.id, status.value)
    bot.database.audit(interaction.guild_id, interaction.user.id, "availability", status.value)
    await interaction.response.send_message(f"Set your status to **{status.value}**.")


@queue_group.command(name="available", description="List players by availability")
async def available(interaction: discord.Interaction):
    rows = bot.database.availability_rows(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("Nobody has set an availability status yet.")
        return
    lines = [f"**{row['status'].title()}**: <@{row['user_id']}>" for row in rows]
    await interaction.response.send_message("**Player availability**\n" + "\n".join(lines))


@player_group.command(name="rivalry", description="Show the head-to-head record between two players")
@app_commands.describe(mode="Game mode", first="First player", second="Second player")
@app_commands.choices(mode=mode_choices)
async def rivalry(interaction: discord.Interaction, mode: app_commands.Choice[str], first: discord.Member, second: discord.Member):
    row = bot.database.rivalry(interaction.guild_id, mode.value, first.id, second.id)
    if not row or not row["games"]:
        await interaction.response.send_message("Those players have not faced each other in that mode.")
        return
    await interaction.response.send_message(f"**{first.display_name} vs {second.display_name} — {mode_label(mode.value)}**\nGames: **{row['games']}**\n{first.mention}: **{row['first_wins']} wins**\n{second.mention}: **{row['second_wins']} wins**")


@match_group.command(name="vote", description="Approve or dispute a pending match result")
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
    await interaction.response.send_message(f"Approval recorded for match **#{match_id}** ({len(confirmed)} confirmation(s)). Use `/match confirm match_id:{match_id}` when both sides have approved.")


@admin_group.command(name="note_add", description="Add an admin note to a player")
@app_commands.describe(player="Player", note="Note text")
@app_commands.checks.has_permissions(manage_guild=True)
async def note_add(interaction: discord.Interaction, player: discord.Member, note: str):
    note_id = bot.database.add_note(interaction.guild_id, player.id, interaction.user.id, note)
    bot.database.audit(interaction.guild_id, interaction.user.id, "note_added", f"note #{note_id} for {player.id}")
    await interaction.response.send_message(f"Added private admin note **#{note_id}** for {player.display_name}.", ephemeral=True)


@admin_group.command(name="notes", description="View admin notes for a player")
@app_commands.describe(player="Player")
@app_commands.checks.has_permissions(manage_guild=True)
async def notes(interaction: discord.Interaction, player: discord.Member):
    rows = bot.database.notes(interaction.guild_id, player.id)
    if not rows:
        await interaction.response.send_message("No notes found.", ephemeral=True)
        return
    await interaction.response.send_message("**Admin notes**\n" + "\n".join(f"#{row['id']} ({row['created_at'][:10]}): {row['note']}" for row in rows), ephemeral=True)


@admin_group.command(name="note_delete", description="Delete an admin note")
@app_commands.describe(note_id="Note number")
@app_commands.checks.has_permissions(manage_guild=True)
async def note_delete(interaction: discord.Interaction, note_id: int):
    if not bot.database.delete_note(interaction.guild_id, note_id):
        await interaction.response.send_message("That note was not found.", ephemeral=True)
        return
    bot.database.audit(interaction.guild_id, interaction.user.id, "note_deleted", f"note #{note_id}")
    await interaction.response.send_message(f"Deleted note **#{note_id}**.", ephemeral=True)


@team_group.command(name="preset_save", description="Save a frequent team roster")
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


@team_group.command(name="presets", description="List saved team presets")
async def presets(interaction: discord.Interaction):
    rows = bot.database.presets(interaction.guild_id)
    await interaction.response.send_message("**Team presets**\n" + ("\n".join(f"**{row['name']}** — {mode_label(row['mode'])}: " + " + ".join(f"<@{x}>" for x in row['players'].split(",")) for row in rows) if rows else "No presets saved."))


@team_group.command(name="preset_delete", description="Delete a saved team preset")
@app_commands.describe(name="Preset name")
async def preset_delete(interaction: discord.Interaction, name: str):
    if not bot.database.delete_preset(interaction.guild_id, name):
        await interaction.response.send_message("That preset was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"Deleted preset **{name}**.")


@series_group.command(name="start", description="Start a best-of-3 or best-of-5 series")
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


@series_group.command(name="update", description="Add a game result to a series")
@app_commands.describe(series_id="Series number", winner="Winning side")
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def series_update(interaction: discord.Interaction, series_id: int, winner: app_commands.Choice[str]):
    row = bot.database.update_series(interaction.guild_id, series_id, int(winner.value))
    if not row:
        await interaction.response.send_message("That open series was not found.", ephemeral=True)
        return
    status = "COMPLETE" if row["status"] == "complete" else "in progress"
    await interaction.response.send_message(f"Series **#{series_id}** is **{status}**: Team 1 **{row['team_one_wins']}** — Team 2 **{row['team_two_wins']}**.")


@series_group.command(name="status", description="Show a series score")
@app_commands.describe(series_id="Series number")
async def series_status(interaction: discord.Interaction, series_id: int):
    row = bot.database.get_series(interaction.guild_id, series_id)
    if not row:
        await interaction.response.send_message("That series was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"**Series #{series_id}** — {mode_label(row['mode'])}\nTeam 1: **{row['team_one_wins']}** · Team 2: **{row['team_two_wins']}** · {row['status'].title()}")


@queue_group.command(name="schedule", description="Schedule a match reminder")
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


@lobby_group.command(name="create", description="Create a check-in lobby for two teams")
@app_commands.describe(mode="Game mode", team_one="Team 1 players", team_two="Team 2 players")
@app_commands.choices(mode=mode_choices)
async def lobby_create(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str):
    try:
        first = parse_team(team_one, team_size(mode.value)); second = parse_team(team_two, team_size(mode.value))
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    lobby_id = bot.database.create_lobby(interaction.guild_id, mode.value, first, second, interaction.user.id)
    mentions = " ".join(f"<@{x}>" for x in first + second)
    await interaction.response.send_message(f"🎮 Lobby **#{lobby_id}** created for {mode_label(mode.value)}. Check in with `/checkin lobby_id:{lobby_id}`.\n{mentions}")


@lobby_group.command(name="checkin", description="Check in for a match lobby")
@app_commands.describe(lobby_id="Lobby number")
async def checkin(interaction: discord.Interaction, lobby_id: int):
    row = bot.database.lobby(interaction.guild_id, lobby_id)
    if not row or row["status"] in ("complete", "cancelled"):
        await interaction.response.send_message("That lobby is not active.", ephemeral=True)
        return
    players = [int(x) for x in row["team_one"].split(",") + row["team_two"].split(",")]
    if interaction.user.id not in players:
        await interaction.response.send_message("You are not in this lobby.", ephemeral=True)
        return
    checked = set(json.loads(row["checked_in"])); checked.add(interaction.user.id)
    status = "ready" if len(checked) == len(players) else "checking_in"
    bot.database.update_lobby(interaction.guild_id, lobby_id, status, sorted(checked), json.loads(row["no_shows"]))
    await interaction.response.send_message(f"✅ {interaction.user.mention} checked in ({len(checked)}/{len(players)})." + (" Lobby is ready." if status == "ready" else ""))


@lobby_group.command(name="status", description="Show match lobby check-ins")
@app_commands.describe(lobby_id="Lobby number")
async def lobby_status(interaction: discord.Interaction, lobby_id: int):
    row = bot.database.lobby(interaction.guild_id, lobby_id)
    if not row:
        await interaction.response.send_message("That lobby was not found.", ephemeral=True)
        return
    players = [int(x) for x in row["team_one"].split(",") + row["team_two"].split(",")]
    checked = set(json.loads(row["checked_in"])); no_shows = set(json.loads(row["no_shows"]))
    await interaction.response.send_message(f"**Lobby #{lobby_id} — {row['status']}**\nChecked in: {len(checked)}/{len(players)}\nMissing: " + (" ".join(f"<@{x}>" for x in players if x not in checked and x not in no_shows) or "none") + "\nNo-shows: " + (" ".join(f"<@{x}>" for x in no_shows) or "none"))


@lobby_group.command(name="start", description="Mark a fully checked-in lobby as an active match")
@app_commands.describe(lobby_id="Lobby number")
async def lobby_start(interaction: discord.Interaction, lobby_id: int):
    row = bot.database.lobby(interaction.guild_id, lobby_id)
    if not row or row["status"] != "ready":
        await interaction.response.send_message("That lobby must be active and fully checked in before it can start.", ephemeral=True)
        return
    bot.database.update_lobby(interaction.guild_id, lobby_id, "active", json.loads(row["checked_in"]), json.loads(row["no_shows"]))
    await interaction.response.send_message(f"🔥 Lobby **#{lobby_id}** is now active. Record the result with `/match record` when the game ends.")


@lobby_group.command(name="cancel", description="Cancel an open match lobby")
@app_commands.describe(lobby_id="Lobby number")
@app_commands.checks.has_permissions(manage_guild=True)
async def lobby_cancel(interaction: discord.Interaction, lobby_id: int):
    row = bot.database.lobby(interaction.guild_id, lobby_id)
    if not row or row["status"] in ("complete", "cancelled"):
        await interaction.response.send_message("That lobby is not open.", ephemeral=True)
        return
    bot.database.update_lobby(interaction.guild_id, lobby_id, "cancelled", json.loads(row["checked_in"]), json.loads(row["no_shows"]))
    await interaction.response.send_message(f"Cancelled lobby **#{lobby_id}**.", ephemeral=True)


@lobby_group.command(name="no_show", description="Mark a player as a no-show")
@app_commands.describe(lobby_id="Lobby number", player="Player who missed check-in")
@app_commands.checks.has_permissions(manage_guild=True)
async def no_show(interaction: discord.Interaction, lobby_id: int, player: discord.Member):
    row = bot.database.lobby(interaction.guild_id, lobby_id)
    if not row:
        await interaction.response.send_message("That lobby was not found.", ephemeral=True)
        return
    no_shows = set(json.loads(row["no_shows"])); no_shows.add(player.id)
    bot.database.update_lobby(interaction.guild_id, lobby_id, "no_show", json.loads(row["checked_in"]), sorted(no_shows))
    bot.database.audit(interaction.guild_id, interaction.user.id, "no_show", f"lobby #{lobby_id}; player={player.id}")
    await interaction.response.send_message(f"Marked {player.mention} as a no-show for lobby **#{lobby_id}**.")


@match_group.command(name="remake", description="Mark a game as remade without changing ratings")
@app_commands.describe(reason="Why the game was remade")
async def remake(interaction: discord.Interaction, reason: str):
    bot.database.audit(interaction.guild_id, interaction.user.id, "remake", reason[:500])
    await interaction.response.send_message(f"🔁 Remake logged by {interaction.user.mention}. No Elo or stats were changed. Reason: {reason}")


@match_group.command(name="forfeit", description="Submit a forfeit result for confirmation")
@app_commands.describe(mode="Game mode", winner="Winning team", team_one="Team 1 players", team_two="Team 2 players")
@app_commands.choices(mode=mode_choices, winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def forfeit(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: app_commands.Choice[str], team_one: str, team_two: str):
    try:
        first = parse_team(team_one, team_size(mode.value)); second = parse_team(team_two, team_size(mode.value))
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    stats = {player_id: {name: 0 for name in stat_names(mode.value)} for player_id in first + second}
    pending_id = bot.database.create_pending_match(interaction.guild_id, mode.value, int(winner.value), first, second, stats, interaction.user.id, "Forfeit")
    await interaction.response.send_message(f"Forfeit match **#{pending_id}** submitted. Both sides must confirm with `/match confirm match_id:{pending_id}`.")


@match_group.command(name="dispute_resolve", description="Resolve a pending match dispute")
@app_commands.describe(match_id="Pending match number", decision="Resolution")
@app_commands.choices(decision=[app_commands.Choice(name="Accept result", value="accept"), app_commands.Choice(name="Reject result", value="reject")])
@app_commands.checks.has_permissions(manage_guild=True)
async def dispute_resolve(interaction: discord.Interaction, match_id: int, decision: app_commands.Choice[str]):
    row = bot.database.pending_match(interaction.guild_id, match_id)
    if not row:
        await interaction.response.send_message("That pending match was not found.", ephemeral=True)
        return
    if decision.value == "reject":
        bot.database.delete_pending_match(interaction.guild_id, match_id)
        bot.database.audit(interaction.guild_id, interaction.user.id, "dispute_rejected", f"pending match #{match_id}")
        await interaction.response.send_message(f"Rejected and discarded pending match **#{match_id}**.")
        return
    stats = {int(user_id): values for user_id, values in json.loads(row["stats_json"]).items()}
    first = [int(x) for x in row["team_one"].split(",")]; second = [int(x) for x in row["team_two"].split(",")]
    bot.database.record_match(interaction.guild_id, row["mode"], row["winner"], first, second, stats, row["created_by"], row["map_name"])
    bot.database.delete_pending_match(interaction.guild_id, match_id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "dispute_accepted", f"pending match #{match_id}")
    await interaction.response.send_message(f"Accepted and recorded pending match **#{match_id}**.")


@queue_group.command(name="lfg", description="Post a looking-for-group request")
@app_commands.describe(mode="Game mode", message="What you are looking for")
@app_commands.choices(mode=mode_choices)
async def lfg(interaction: discord.Interaction, mode: app_commands.Choice[str], message: str = "Need players"):
    await interaction.response.send_message(f"📣 **LFG — {mode_label(mode.value)}**\n{interaction.user.mention} is looking for players: {message}")


@lobby_group.command(name="channels", description="Create temporary text and voice channels for a match")
@app_commands.describe(name="Short match name", players="Players to mention")
@app_commands.checks.has_permissions(manage_channels=True)
async def match_channels(interaction: discord.Interaction, name: str, players: str = ""):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    slug = "".join(character for character in name.lower().replace(" ", "-") if character.isalnum() or character == "-")[:40] or "match"
    text_channel = await interaction.guild.create_text_channel(f"match-{slug}", reason="Temporary Gears match channel")
    voice_channel = await interaction.guild.create_voice_channel(f"Match {name[:80]}", reason="Temporary Gears match channel")
    delete_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=6)).isoformat()
    bot.database.track_channel(interaction.guild.id, text_channel.id, delete_at)
    bot.database.track_channel(interaction.guild.id, voice_channel.id, delete_at)
    await text_channel.send(f"🎮 Match room for {interaction.user.mention}. {players}" if players else f"🎮 Match room for {interaction.user.mention}.")
    await interaction.response.send_message(f"Created {text_channel.mention} and {voice_channel.mention}. They will be deleted after six hours.")


@lobby_group.command(name="channels_close", description="Close temporary match channels")
@app_commands.describe(channel="Channel to close")
@app_commands.checks.has_permissions(manage_channels=True)
async def match_channels_close(interaction: discord.Interaction, channel: discord.TextChannel):
    if channel.guild.id != interaction.guild_id:
        await interaction.response.send_message("That channel is not in this server.", ephemeral=True)
        return
    await channel.delete(reason="Close temporary Gears match channel")
    bot.database.untrack_channel(channel.id)
    await interaction.response.send_message(f"Closed **{channel.name}**.")


@maps_group.command(name="veto_start", description="Start a map ban and pick session")
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


@maps_group.command(name="veto_ban", description="Ban a map from a veto session")
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


@maps_group.command(name="veto_pick", description="Pick the final map in a veto session")
@app_commands.describe(veto_id="Veto session number", map_name="Map to pick")
@app_commands.autocomplete(map_name=map_autocomplete)
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


@stats_group.command(name="leaderboard", description="Show the top ratings for a mode")
@app_commands.describe(mode="Game mode", metric="Ranking metric", min_games="Only include players with at least this many games", page="Leaderboard page")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(metric=[app_commands.Choice(name="Elo", value="rating"), app_commands.Choice(name="Wins", value="wins"), app_commands.Choice(name="Win rate", value="winrate"), app_commands.Choice(name="Kills", value="kills"), app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Score", value="score"), app_commands.Choice(name="Assists", value="assists")])
async def leaderboard(interaction: discord.Interaction, mode: app_commands.Choice[str], metric: app_commands.Choice[str] | None = None, min_games: int = 0, page: int = 1):
    if min_games < 0 or min_games > 100:
        await interaction.response.send_message("Minimum games must be between 0 and 100.", ephemeral=True)
        return
    if page < 1 or page > 100:
        await interaction.response.send_message("Leaderboard page must be between 1 and 100.", ephemeral=True)
        return
    metric_value = metric.value if metric else "rating"
    rows = bot.database.leaderboard(interaction.guild_id, mode.value, metric_value, 10, min_games, (page - 1) * 10)
    if not rows:
        await interaction.response.send_message(f"No matches have been recorded for **{mode_label(mode.value)}** yet.")
        return
    values = {"rating": "Elo", "wins": "wins", "winrate": "win rate", "kills": "kills", "damage": "damage", "score": "score", "assists": "assists"}
    embed = discord.Embed(title=f"{mode_label(mode.value)} leaderboard", description=f"Sorted by **{values[metric_value]}** · Page **{page}**" + (f" · Minimum games: **{min_games}**" if min_games else ""), colour=discord.Colour.red())
    def metric_display(row):
        return f"{row['wins'] / row['games'] * 100:.1f}%" if metric_value == "winrate" else row[metric_value]
    embed.description += "\n\n" + "\n".join(f"**{index}.** <@{row['user_id']}> — {row['rating']} Elo · {row['wins']}-{row['losses']} · {metric_display(row)}" for index, row in enumerate(rows, (page - 1) * 10 + 1))
    view = LeaderboardView(interaction.guild_id, mode.value, metric_value, min_games, page)
    view.previous.disabled = page <= 1
    view.next.disabled = len(rows) < 10
    await interaction.response.send_message(embed=embed, view=view)


@stats_group.command(name="streaks", description="Show the current win-streak leaders for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def streaks(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.streak_leaderboard(interaction.guild_id, mode.value)
    if not rows:
        await interaction.response.send_message(f"No active win streaks in **{mode_label(mode.value)}**.")
        return
    lines = [f"{index}. <@{row['user_id']}> — **{row['current_streak']}** straight wins (best: {row['best_streak']})" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message(f"**{mode_label(mode.value)} streak leaders**\n" + "\n".join(lines))


@stats_group.command(name="rating", description="Show a player's ratings across all modes")
@app_commands.describe(player="Optional player; defaults to you")
async def rating(interaction: discord.Interaction, player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.player_stats(interaction.guild_id, member.id)
    if not rows:
        await interaction.response.send_message(f"<@{member.id}> has no recorded matches yet.")
        return
    lines = [f"{mode_label(row['mode'])}: **{row['rating']}** · Rank {gow2_rank(row['rating'])[0]} ({gow2_rank(row['rating'])[1]}) · {row['wins']}-{row['losses']}" for row in rows]
    await interaction.response.send_message(f"**{member.display_name}'s ratings**\n" + "\n".join(lines))


@stats_group.command(name="trend", description="Show a player's Elo and performance trend")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you", metric="Performance metric to chart")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(metric=[
    app_commands.Choice(name="Damage", value="damage"),
    app_commands.Choice(name="Kills", value="kills"),
    app_commands.Choice(name="Score", value="score"),
])
async def trend(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None, metric: app_commands.Choice[str] | None = None):
    await interaction.response.defer()
    member = player or interaction.user
    rows = bot.database.rating_history(interaction.guild_id, member.id, mode.value)
    if not rows:
        await send_response(interaction, f"<@{member.id}> has no recorded matches for **{mode_label(mode.value)}** yet.", ephemeral=True)
        return

    metric_name = metric.value if metric else "damage"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        await send_response(interaction, "Charts are unavailable because the plotting dependency is not installed. Run `python -m pip install -r requirements.txt` and restart the bot.", ephemeral=True)
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
    await send_response(
        interaction,
        f"**{member.display_name} — {mode_label(mode.value)} trend**\nShowing {len(rows)} matches. Elo: **{ratings[-1]}** · Average {metric_name}: **{sum(performance) / len(performance):.1f}**",
        file=discord.File(image, filename="gears-elo-trend.png"),
    )


@player_group.command(name="profile", description="Show a complete player profile")
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


@stats_group.command(name="achievements", description="Show a player's earned badges")
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
    for achievement in bot.database.custom_achievements(interaction.guild_id):
        progress = bot.database.custom_progress(interaction.guild_id, member.id, achievement["metric"])
        if progress >= achievement["threshold"]:
            badges.append(f"🏆 {achievement['name']}")
    if not badges:
        await interaction.response.send_message(f"<@{member.id}> has no achievements yet. Play a match to get started.")
        return
    await interaction.response.send_message(f"**{member.display_name}'s achievements**\n" + "\n".join(dict.fromkeys(badges)))


@admin_group.command(name="achievement_create", description="Create a server-specific achievement")
@app_commands.describe(name="Achievement name", metric="Progress metric", threshold="Required total")
@app_commands.choices(metric=[app_commands.Choice(name="Games", value="games"), app_commands.Choice(name="Kills", value="kills"), app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Score", value="score"), app_commands.Choice(name="Assists", value="assists")])
@app_commands.checks.has_permissions(manage_guild=True)
async def achievement_create(interaction: discord.Interaction, name: str, metric: app_commands.Choice[str], threshold: int):
    if threshold < 1 or len(name) > 80:
        await interaction.response.send_message("Use a positive threshold and an achievement name of 80 characters or fewer.", ephemeral=True)
        return
    achievement_id = bot.database.create_achievement(interaction.guild_id, name, metric.value, threshold, interaction.user.id)
    await interaction.response.send_message(f"Created achievement **#{achievement_id} {name}**: {threshold} {metric.value}.")


@stats_group.command(name="elo_history", description="Show a player's Elo changes")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you", limit="Number of matches")
@app_commands.choices(mode=mode_choices)
async def elo_history(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None, limit: int = 10):
    member = player or interaction.user
    rows = bot.database.elo_history(interaction.guild_id, member.id, mode.value, limit)
    if not rows:
        await interaction.response.send_message("No Elo history found.")
        return
    await interaction.response.send_message(f"**{member.display_name} Elo history — {mode_label(mode.value)}**\n" + "\n".join(f"Match #{row['id']}: {row['rating_before']} → {row['rating_before'] + row['rating_delta']} ({row['rating_delta']:+d})" for row in rows))


@stats_group.command(name="confidence", description="Show how established a player's rating is")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def confidence(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    row = bot.database.connection.execute("SELECT rating, games FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (interaction.guild_id, member.id, mode.value)).fetchone()
    games = row["games"] if row else 0
    confidence_value = games / (games + 10) * 100 if games else 0
    rating_value = row["rating"] if row else bot.database.get_rating(interaction.guild_id, member.id, mode.value)
    await interaction.response.send_message(f"**{member.display_name} — {mode_label(mode.value)}**\nElo: **{rating_value}**\nRating confidence: **{confidence_value:.0f}%** ({games} games; confidence increases with more results)")


@stats_group.command(name="predict", description="Estimate each team's win probability")
@app_commands.describe(mode="Game mode", team_one="Team 1 players", team_two="Team 2 players")
@app_commands.choices(mode=mode_choices)
async def predict(interaction: discord.Interaction, mode: app_commands.Choice[str], team_one: str, team_two: str):
    try:
        first = parse_team(team_one, team_size(mode.value)); second = parse_team(team_two, team_size(mode.value))
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    first_rating = sum(bot.database.get_rating(interaction.guild_id, player_id, mode.value) for player_id in first) / len(first)
    second_rating = sum(bot.database.get_rating(interaction.guild_id, player_id, mode.value) for player_id in second) / len(second)
    probability = expected_score(first_rating, second_rating) * 100
    await interaction.response.send_message(f"**{mode_label(mode.value)} prediction**\nTeam 1 average Elo: **{first_rating:.0f}** — **{probability:.0f}%** chance\nTeam 2 average Elo: **{second_rating:.0f}** — **{100 - probability:.0f}%** chance")


@stats_group.command(name="awards", description="Show performance leaders for a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def awards(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.connection.execute("SELECT user_id, SUM(kills) AS kills, SUM(damage) AS damage, SUM(assists) AS assists, SUM(score) AS score FROM match_player_stats WHERE guild_id=? AND mode=? GROUP BY user_id", (interaction.guild_id, mode.value)).fetchall()
    if not rows:
        await interaction.response.send_message("No performance data yet.")
        return
    categories = [("💀 Slayer", "kills"), ("💥 Damage Dealer", "damage"), ("🎯 Playmaker", "assists"), ("🏅 Scorer", "score")]
    lines = [f"{label}: <@{max(rows, key=lambda row: row[column])['user_id']}> ({max(rows, key=lambda row: row[column])[column]})" for label, column in categories]
    await interaction.response.send_message(f"**{mode_label(mode.value)} performance awards**\n" + "\n".join(lines))


@stats_group.command(name="hall_of_fame", description="Show the server's all-time career leaders")
async def hall_of_fame(interaction: discord.Interaction):
    rows = bot.database.connection.execute("SELECT user_id, MAX(peak_rating) AS peak, SUM(wins) AS wins, SUM(games) AS games FROM ratings WHERE guild_id=? GROUP BY user_id ORDER BY peak DESC, wins DESC, games DESC LIMIT 10", (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("The Hall of Fame is empty until matches are recorded.")
        return
    lines = [f"**{index}.** <@{row['user_id']}> — peak **{row['peak']}** Elo · **{row['wins']}** wins · **{row['games']}** games" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message("🏛️ **Gears 5 Hall of Fame**\n" + "\n".join(lines))


@stats_group.command(name="quests", description="Show short-term stat quests for a player")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def quests(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    player = player or interaction.user
    summary = bot.database.player_stat_summary(interaction.guild_id, player.id, mode.value)
    if not summary or not summary["matches"]:
        await interaction.response.send_message(f"{player.mention} has no quest progress in **{mode_label(mode.value)}** yet.")
        return
    goals = [("Win 3 matches", summary["matches"] and bot.database.connection.execute("SELECT wins FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (interaction.guild_id, player.id, mode.value)).fetchone()["wins"], 3), ("Deal 1,000 damage", summary["damage"] or 0, 1000), ("Get 30 kills", summary["kills"] or 0, 30)]
    lines = [f"{'✅' if value >= target else '⬜'} {label}: **{min(value, target)}/{target}**" for label, value, target in goals]
    await interaction.response.send_message(f"🎯 **{player.display_name} quests — {mode_label(mode.value)}**\n" + "\n".join(lines))


@stats_group.command(name="player", description="Show a player's match-stat totals and averages")
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


@player_group.command(name="myhistory", description="Show your recent matches with full personal stats")
@app_commands.describe(limit="Number of matches, from 1 to 20")
async def myhistory(interaction: discord.Interaction, limit: int = 10):
    rows = bot.database.player_history(interaction.guild_id, interaction.user.id, limit)
    if not rows:
        await interaction.response.send_message("You have no recorded matches yet.")
        return
    lines = [f"**#{row['id']} {mode_label(row['mode'])}** — Team {row['winner']} won · K/D {row['kills']}/{row['deaths']} · Damage {row['damage']} · Score {row['score']} · Elo {row['rating_delta']:+d}" for row in rows]
    await interaction.response.send_message("**Your recent match history**\n" + "\n".join(lines))


@maps_group.command(name="mapplayer", description="Show a player's performance by map")
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


@admin_group.command(name="announce", description="Post a leaderboard announcement")
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


@match_group.command(name="edit", description="Correct a player's recorded stats in a match")
@app_commands.describe(match_id="Recorded match number", player="Player whose stats need correction", stats_line="Complete stat line, e.g. kills=10 deaths=4 damage=200 score=100")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(match_id=match_id_autocomplete)
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


@stats_group.command(name="teamstats", description="Show the head-to-head record for two exact teams")
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


@team_group.command(name="chemistry", description="Show an exact team's overall chemistry")
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


@maps_group.command(name="mapstats", description="Show match counts and wins by map")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def mapstats(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.map_stats(interaction.guild_id, mode.value)
    if not rows:
        await interaction.response.send_message(f"No map data recorded for **{mode_label(mode.value)}** yet.")
        return
    lines = [f"**{row['map_name']}** — {row['games']} games · Team 1: {row['team_one_wins']} wins · Team 2: {row['team_two_wins']} wins" for row in rows]
    await interaction.response.send_message(f"**{mode_label(mode.value)} map stats**\n" + "\n".join(lines))


@stats_group.command(name="history", description="Show recent recorded matches")
@app_commands.describe(mode="Optional game mode", limit="Number of matches, from 1 to 20")
@app_commands.choices(mode=mode_choices)
async def history(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None, limit: int = 10):
    await interaction.response.defer()
    rows = bot.database.match_history(interaction.guild_id, mode.value if mode else None, limit)
    if not rows:
        await send_response(interaction, "No recorded matches found.")
        return
    lines = []
    for row in rows:
        first = " + ".join(f"<@{user_id}>" for user_id in row["team_one"].split(","))
        second = " + ".join(f"<@{user_id}>" for user_id in row["team_two"].split(","))
        season_text = f" · {row['season_name']}" if row["season_name"] else ""
        lines.append(f"**#{row['id']} {mode_label(row['mode'])}** · {row['map_name']} · Team {row['winner']} won{season_text}\n{first} vs {second}")
    await send_response(interaction, "**Recent match history**\n" + "\n".join(lines))


@stats_group.command(name="match_card", description="Create a Gears-themed image of a recorded match")
@app_commands.describe(match_id="Recorded match number")
@app_commands.autocomplete(match_id=match_id_autocomplete)
async def match_card(interaction: discord.Interaction, match_id: int):
    await interaction.response.defer()
    match = bot.database.connection.execute("SELECT * FROM matches WHERE guild_id=? AND id=?", (interaction.guild_id, match_id)).fetchone()
    if not match:
        await send_response(interaction, f"Match **#{match_id}** was not found in this server.", ephemeral=True)
        return
    stats = bot.database.connection.execute("SELECT * FROM match_player_stats WHERE guild_id=? AND match_id=? ORDER BY user_id", (interaction.guild_id, match_id)).fetchall()
    player_ids = [row["user_id"] for row in stats]
    labels = await fetch_player_labels(interaction.guild, player_ids)
    try:
        image = render_match_card(match, stats, labels)
    except ImportError:
        await send_response(interaction, "Image cards require the plotting dependency. Run `python -m pip install -r requirements.txt` and restart the bot.", ephemeral=True)
        return
    except (OSError, ValueError, RuntimeError) as error:
        logging.getLogger("gears5-elo-bot").exception("Match card generation failed", exc_info=error)
        await send_response(interaction, "I could not generate that match card. Check the background/rank image files and try again.", ephemeral=True)
        return
    await send_response(interaction, f"**Match #{match_id} snapshot**", file=discord.File(image, filename=f"gears5-match-{match_id}.png"))


@match_group.command(name="undo", description="Undo the latest match in this server")
@app_commands.checks.has_permissions(manage_guild=True)
async def undo(interaction: discord.Interaction):
    await interaction.response.defer()
    if not has_command_access(interaction, "undo"):
        await send_response(interaction, "You do not have the role required to undo matches.", ephemeral=True)
        return
    try:
        removed = bot.database.undo_latest_match(interaction.guild_id)
    except (ValueError, sqlite3.Error) as error:
        await send_response(interaction, f"Could not undo match: {error}", ephemeral=True)
        return
    bot.database.audit(interaction.guild_id, interaction.user.id, "match_undone", f"match #{removed['id']}; mode={removed['mode']}")
    await send_response(interaction, f"Undid match **#{removed['id']}** ({mode_label(removed['mode'])}). Re-enter it with `/match record` if needed.")


@admin_group.command(name="audit", description="Show recent administrative bot actions")
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


@admin_group.command(name="permission_set", description="Require a Discord role for a command")
@app_commands.describe(command="Command name without slash", role="Required role")
@app_commands.checks.has_permissions(manage_guild=True)
async def permission_set(interaction: discord.Interaction, command: str, role: discord.Role):
    bot.database.set_command_role(interaction.guild_id, command, role.id)
    bot.database.audit(interaction.guild_id, interaction.user.id, "permission_set", f"/{command.lstrip('/')} requires {role.id}")
    await interaction.response.send_message(f"Configured **/{command.lstrip('/')}** to require {role.mention} (managers can still use it).")


@admin_group.command(name="backup_now", description="Create a database backup")
@app_commands.checks.has_permissions(manage_guild=True)
async def backup_now(interaction: discord.Interaction):
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIRECTORY / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3"
    bot.database.backup(destination)
    bot.database.audit(interaction.guild_id, interaction.user.id, "backup_created", destination.name)
    await interaction.response.send_message(f"Created database backup `{destination.name}`.", ephemeral=True)


@admin_group.command(name="backup_restore", description="Restore a database backup by filename")
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


@tasks.loop(minutes=5)
async def temporary_channel_cleanup():
    for row in bot.database.due_channels():
        channel = bot.get_channel(row["channel_id"])
        if channel:
            try:
                await channel.delete(reason="Expired temporary Gears match channel")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        bot.database.untrack_channel(row["channel_id"])


@player_group.command(name="compare", description="Compare two player profiles")
@app_commands.describe(mode="Game mode", first="First player", second="Second player")
@app_commands.choices(mode=mode_choices)
async def player_compare(interaction: discord.Interaction, mode: app_commands.Choice[str], first: discord.User, second: discord.User):
    first_member = interaction.guild.get_member(first.id)
    second_member = interaction.guild.get_member(second.id)
    if not first_member or not second_member:
        await send_response(interaction, "Both players must be members of this server.", ephemeral=True)
        return
    if first.id == second.id:
        await send_response(interaction, "Choose two different players to compare.", ephemeral=True)
        return
    rows = [bot.database.connection.execute("SELECT rating, wins, losses, games FROM ratings WHERE guild_id=? AND user_id=? AND mode=?", (interaction.guild_id, member.id, mode.value)).fetchone() for member in (first_member, second_member)]
    lines = []
    for member, row in zip((first_member, second_member), rows):
        rating = row["rating"] if row else bot.database.get_rating(interaction.guild_id, member.id, mode.value)
        record = f"{row['wins']}-{row['losses']}" if row else "0-0"
        lines.append(f"{member.mention}: **{rating} Elo** · {record}")
    await interaction.response.send_message(f"**Player comparison — {mode_label(mode.value)}**\n" + "\n".join(lines))


@player_group.command(name="recent_form", description="Show a player's last five results")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def recent_form(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.player_history(interaction.guild_id, member.id, 5)
    rows = [row for row in rows if row["mode"] == mode.value]
    if not rows:
        await interaction.response.send_message("No recent results for that mode.")
        return
    await interaction.response.send_message(f"**{member.display_name} recent form — {mode_label(mode.value)}**\n" + " · ".join(f"#{row['id']} {row['rating_delta']:+d}" for row in rows))


@player_group.command(name="consistency", description="Show a player's performance consistency")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def consistency(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    rows = bot.database.rating_history(interaction.guild_id, member.id, mode.value, 20)
    if not rows:
        await interaction.response.send_message("No matches for that player and mode.")
        return
    values = [row["rating_delta"] for row in rows]
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    await interaction.response.send_message(f"**{member.display_name} consistency — {mode_label(mode.value)}**\nAverage Elo change: **{average:+.1f}** · Volatility: **{variance ** 0.5:.1f}** points")


@player_group.command(name="personal_bests", description="Show a player's best recorded stat lines")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def personal_bests(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    member = player or interaction.user
    row = bot.database.connection.execute("SELECT MAX(kills) AS kills, MAX(damage) AS damage, MAX(score) AS score, MAX(assists) AS assists, MAX(captures) AS captures, MAX(breaks) AS breaks FROM match_player_stats WHERE guild_id=? AND user_id=? AND mode=?", (interaction.guild_id, member.id, mode.value)).fetchone()
    if not row or row["kills"] is None:
        await interaction.response.send_message("No stats for that player and mode.")
        return
    values = " · ".join(f"{name.title()}: **{row[name]}**" for name in stat_names(mode.value))
    await interaction.response.send_message(f"**{member.display_name} personal bests — {mode_label(mode.value)}**\n{values}")


@stats_group.command(name="close_games", description="Show the closest recorded matches")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def close_games(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    history = bot.database.match_history(interaction.guild_id, mode.value, 10)
    if not history:
        await interaction.response.send_message("No matches recorded for that mode.")
        return
    scored = []
    for row in history:
        first_score = bot.database.connection.execute("SELECT COALESCE(SUM(score), 0) FROM match_player_stats WHERE match_id=? AND user_id IN (SELECT value FROM json_each(?))", (row["id"], "[" + row["team_one"].replace(",", ",") + "]")).fetchone()[0]
        all_score = bot.database.connection.execute("SELECT COALESCE(SUM(score), 0) FROM match_player_stats WHERE match_id=?", (row["id"],)).fetchone()[0]
        scored.append((abs(first_score - (all_score - first_score)), row))
    lines = [f"Match #{row['id']} — score margin **{margin}** · Team {row['winner']} won · {row['map_name']}" for margin, row in sorted(scored, key=lambda item: item[0])]
    await interaction.response.send_message(f"**Closest recent games — {mode_label(mode.value)}**\n" + "\n".join(lines))


@stats_group.command(name="comebacks", description="Show upset and comeback wins")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def comebacks(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.connection.execute("SELECT id, winner, team_one, team_two FROM matches WHERE guild_id=? AND mode=? ORDER BY id DESC LIMIT 50", (interaction.guild_id, mode.value)).fetchall()
    lines = []
    for row in rows:
        first = [bot.database.get_rating(interaction.guild_id, int(x), mode.value) for x in row["team_one"].split(",")]
        second = [bot.database.get_rating(interaction.guild_id, int(x), mode.value) for x in row["team_two"].split(",")]
        if (row["winner"] == 1 and sum(first) < sum(second)) or (row["winner"] == 2 and sum(second) < sum(first)):
            lines.append(f"Match #{row['id']} — Team {row['winner']} upset the higher-rated side")
    await interaction.response.send_message(f"**Comeback/upset wins — {mode_label(mode.value)}**\n" + ("\n".join(lines[:10]) or "No rating upsets recorded yet."))


@maps_group.command(name="rotation_set", description="Configure the server's map rotation")
@app_commands.describe(maps="Comma-separated map names")
@app_commands.checks.has_permissions(manage_guild=True)
async def rotation_set(interaction: discord.Interaction, maps: str):
    map_list = [item.strip() for item in maps.split(",") if item.strip()]
    if not map_list:
        await interaction.response.send_message("Enter at least one map.", ephemeral=True)
        return
    bot.database.set_rotation(interaction.guild_id, map_list)
    await interaction.response.send_message(f"Map rotation set: {', '.join(map_list)}")


@maps_group.command(name="next_map", description="Get and advance the next map in rotation")
async def next_map(interaction: discord.Interaction):
    selected = bot.database.next_map(interaction.guild_id)
    await interaction.response.send_message(f"Next map: **{selected}**" if selected else "No map rotation configured. Use `/rotation_set` first.")


@admin_group.command(name="nickname_sync", description="Sync player nicknames with their Elo")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(manage_nicknames=True)
async def nickname_sync(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    rows = bot.database.leaderboard(interaction.guild_id, mode.value, "rating", 100)
    updated = 0
    for row in rows:
        member = interaction.guild.get_member(row["user_id"])
        if member and not member.bot:
            try:
                await member.edit(nick=f"{member.display_name[:24]} [{row['rating']}]", reason="Sync Gears Elo nickname")
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
    await interaction.response.send_message(f"Updated {updated} nickname(s) for {mode_label(mode.value)}.")


@admin_group.command(name="roles_cleanup", description="Remove outdated Elo tier roles")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
@app_commands.checks.has_permissions(manage_roles=True)
async def roles_cleanup(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    prefix = f"Gears Elo • {mode_label(mode.value)} • "
    removed = 0
    for member in interaction.guild.members:
        roles = [role for role in member.roles if role.name.startswith(prefix)]
        if len(roles) > 1:
            try:
                await member.remove_roles(*roles[:-1], reason="Clean up duplicate Elo roles")
                removed += len(roles) - 1
            except (discord.Forbidden, discord.HTTPException):
                continue
    await interaction.response.send_message(f"Removed {removed} outdated role assignment(s).")


@server_group.command(name="health", description="Show bot and database health")
async def health(interaction: discord.Interaction):
    table_count = bot.database.connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    await interaction.response.send_message(f"✅ Bot online\nDatabase: healthy\nRecorded matches: **{table_count}**\nDashboard: `http://<this-PC-IP>:{os.getenv('DASHBOARD_PORT', '5050')}`")


@admin_group.command(name="integrity", description="Check database integrity")
@app_commands.checks.has_permissions(manage_guild=True)
async def integrity(interaction: discord.Interaction):
    result = bot.database.connection.execute("PRAGMA integrity_check").fetchone()[0]
    orphaned = bot.database.connection.execute("SELECT COUNT(*) FROM match_player_stats s LEFT JOIN matches m ON m.id=s.match_id WHERE m.id IS NULL").fetchone()[0]
    await interaction.response.send_message(f"Database integrity: **{result}**\nOrphaned stat rows: **{orphaned}**")


@admin_group.command(name="webhook_set", description="Configure a webhook for future announcements")
@app_commands.describe(url="Discord webhook URL")
@app_commands.checks.has_permissions(manage_webhooks=True)
async def webhook_set(interaction: discord.Interaction, url: str):
    if not url.startswith("https://discord.com/api/webhooks/"):
        await interaction.response.send_message("That does not look like a Discord webhook URL.", ephemeral=True)
        return
    bot.database.set_webhook(interaction.guild_id, url)
    await interaction.response.send_message("Webhook saved for future bot notifications.", ephemeral=True)


@admin_group.command(name="dashboard_share", description="Create a public read-only dashboard link")
@app_commands.checks.has_permissions(manage_guild=True)
async def dashboard_share(interaction: discord.Interaction):
    token = bot.database.create_share(interaction.guild_id, interaction.user.id)
    port = os.getenv("DASHBOARD_PORT", "5050")
    await interaction.response.send_message(f"Read-only dashboard link: `http://<this-PC-IP>:{port}/share/{token}`", ephemeral=True)


@player_group.command(name="profile_set", description="Set your Xbox gamertag and searchable aliases")
@app_commands.describe(gamertag="Your Xbox gamertag", aliases="Optional comma-separated old or alternate names")
async def profile_set(interaction: discord.Interaction, gamertag: str, aliases: str = ""):
    alias_list = [value.strip() for value in aliases.split(",") if value.strip() and value.strip().lower() != gamertag.strip().lower()]
    bot.database.set_profile(interaction.guild_id, interaction.user.id, gamertag, alias_list)
    await interaction.response.send_message(f"Saved your gamertag as **{gamertag.strip()}**" + (f" with aliases: {', '.join(alias_list)}." if alias_list else "."), ephemeral=True)


@team_group.command(name="teamhistory", description="Show the record and totals for an exact recurring team")
@app_commands.describe(mode="Game mode", team="Comma-separated mentions or IDs for the team")
@app_commands.choices(mode=mode_choices)
async def teamhistory(interaction: discord.Interaction, mode: app_commands.Choice[str], team: str):
    try:
        players = parse_team(team, team_size(mode.value))
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    row = bot.database.team_history(interaction.guild_id, mode.value, players)
    if not row:
        await interaction.response.send_message("No history exists for that exact team yet.")
        return
    roster = " + ".join(f"<@{player_id}>" for player_id in players)
    await interaction.response.send_message(f"**{mode_label(mode.value)} team history**\n{roster}\nRecord: **{row['wins']}-{row['losses']}** across **{row['games']}** games\nStats: " + " · ".join(f"{name.title()}: **{row[name]}**" for name in stat_names(mode.value)))


@match_group.command(name="clips", description="Show the latest match replay and clip links")
@app_commands.describe(mode="Optional game mode", limit="Number of clips to show")
@app_commands.choices(mode=mode_choices)
async def clips(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None, limit: int = 10):
    rows = bot.database.replay_gallery(interaction.guild_id, mode.value if mode else None, limit)
    if not rows:
        await interaction.response.send_message("No replay or clip links have been attached yet.")
        return
    lines = [f"**Match #{row['id']} — {mode_label(row['mode'])} — {row['map_name']}**\n{row['replay_url']}" + (f"\n_{row['note']}_" if row['note'] else "") for row in rows]
    await interaction.response.send_message("**Match clip gallery**\n" + "\n\n".join(lines))


@admin_group.command(name="announcement_channel", description="Set the channel for scheduled leaderboard announcements")
@app_commands.describe(channel="Announcement channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def announcement_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.database.set_announcement_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(f"Leaderboard announcements will use {channel.mention}.", ephemeral=True)


@admin_group.command(name="announcement_schedule", description="Schedule recurring leaderboard announcements")
@app_commands.describe(mode="Game mode", interval_minutes="Minutes between announcements", metric="Ranking metric", channel="Optional destination channel")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(metric=[app_commands.Choice(name="Elo", value="rating"), app_commands.Choice(name="Wins", value="wins"), app_commands.Choice(name="Win rate", value="winrate"), app_commands.Choice(name="Kills", value="kills"), app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Score", value="score"), app_commands.Choice(name="Assists", value="assists")])
@app_commands.checks.has_permissions(manage_guild=True)
async def announcement_schedule(interaction: discord.Interaction, mode: app_commands.Choice[str], interval_minutes: int, metric: app_commands.Choice[str] | None = None, channel: discord.TextChannel | None = None):
    if not 15 <= interval_minutes <= 10080:
        await interaction.response.send_message("Interval must be between 15 minutes and 7 days.", ephemeral=True)
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Choose a text channel.", ephemeral=True)
        return
    schedule_id = bot.database.schedule_announcement(interaction.guild_id, target.id, mode.value, metric.value if metric else "rating", interval_minutes, interaction.user.id)
    await interaction.response.send_message(f"Created announcement schedule **#{schedule_id}** in {target.mention} every **{interval_minutes} minutes**.", ephemeral=True)


@admin_group.command(name="announcement_cancel", description="Cancel a scheduled leaderboard announcement")
@app_commands.describe(schedule_id="Announcement schedule number")
@app_commands.checks.has_permissions(manage_guild=True)
async def announcement_cancel(interaction: discord.Interaction, schedule_id: int):
    if not bot.database.delete_announcement(interaction.guild_id, schedule_id):
        await interaction.response.send_message("That announcement schedule was not found.", ephemeral=True)
        return
    await interaction.response.send_message(f"Cancelled announcement schedule **#{schedule_id}**.", ephemeral=True)


@admin_group.command(name="maintenance", description="Pause or resume match submissions")
@app_commands.describe(enabled="Whether to pause non-admin match submissions")
@app_commands.checks.has_permissions(manage_guild=True)
async def maintenance(interaction: discord.Interaction, enabled: bool):
    bot.database.set_maintenance(interaction.guild_id, enabled)
    await interaction.response.send_message(f"Match submission maintenance mode is now **{'ON' if enabled else 'OFF'}**.", ephemeral=True)


class HelpMenuView(discord.ui.View):
    def __init__(self, pages: list[str]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page = 0

    async def refresh(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Gears 5 Elo commands", description=self.pages[self.page], colour=discord.Colour.red())
        embed.set_footer(text=f"Page {self.page + 1}/{len(self.pages)} · Use Discord's command search for options")
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page == len(self.pages) - 1
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(self.pages) - 1, self.page + 1)
        await self.refresh(interaction)


@server_group.command(name="help_menu", description="Show commands grouped by use")
async def help_menu(interaction: discord.Interaction):
    lines = []
    for command in sorted(bot.tree.get_commands(), key=lambda item: item.name):
        children = getattr(command, "commands", [])
        if children:
            lines.extend(f"`/{command.name} {child.name}`" for child in sorted(children, key=lambda item: item.name))
        else:
            lines.append(f"`/{command.name}`")
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 950:
            chunks.append(current)
            current = ""
        current += (" " if current else "") + line
    if current:
        chunks.append(current)
    pages = [f"**Commands {index}/{len(chunks)}**\n{chunk}" for index, chunk in enumerate(chunks, 1)] or ["No commands are currently registered."]
    view = HelpMenuView(pages)
    view.previous.disabled = True
    view.next.disabled = len(pages) <= 1
    embed = discord.Embed(title="Gears 5 Elo commands", description=pages[0], colour=discord.Colour.red())
    embed.set_footer(text=f"Page 1/{len(pages)} · Use Discord's command search for options")
    await interaction.response.send_message(embed=embed, view=view)


@stats_group.command(name="lb", description="Alias for the player leaderboard")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def lb(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await leaderboard(interaction, mode)


@tasks.loop(minutes=1)
async def scheduled_reminders():
    for row in bot.database.due_schedules():
        channel = bot.get_channel(row["channel_id"])
        if channel:
            players = row["team_one"].split(",") + row["team_two"].split(",")
            mentions = " ".join(f"<@{player_id}>" for player_id in players)
            await channel.send(f"⏰ Match reminder — **{mode_label(row['mode'])}** is scheduled now. {mentions}")
        bot.database.mark_schedule_notified(row["id"])


@tasks.loop(minutes=1)
async def scheduled_announcements():
    for row in bot.database.due_announcements():
        channel = bot.get_channel(row["channel_id"])
        if channel:
            rows = bot.database.leaderboard(row["guild_id"], row["mode"], row["metric"], 10)
            if rows:
                metric = row["metric"]
                values = "\n".join(f"**{index}.** <@{item['user_id']}> — {item['rating']} Elo · {item['wins']}-{item['losses']}" for index, item in enumerate(rows, 1))
                await channel.send(f"📊 **{mode_label(row['mode'])} leaderboard** — sorted by **{metric}**\n{values}")
        bot.database.advance_announcement(row["id"], row["interval_minutes"])


async def _send_metric_leaders(interaction: discord.Interaction, mode: app_commands.Choice[str], metric: str, label: str):
    rows = bot.database.connection.execute(
        f"SELECT user_id, SUM({metric}) AS value FROM match_player_stats WHERE guild_id=? AND mode=? GROUP BY user_id ORDER BY value DESC LIMIT 10",
        (interaction.guild_id, mode.value),
    ).fetchall()
    if not rows:
        await send_response(interaction, f"No **{label}** data is recorded for **{mode_label(mode.value)}** yet.")
        return
    lines = [f"**{index}.** <@{row['user_id']}> — **{row['value']}** {label}" for index, row in enumerate(rows, 1)]
    await send_response(interaction, f"**Top {label} — {mode_label(mode.value)}**\n" + "\n".join(lines))


@insights_group.command(name="overview", description="Show a compact overview of a mode")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_overview(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    row = bot.database.connection.execute(
        "SELECT COUNT(*) AS matches, COUNT(DISTINCT s.user_id) AS players, COALESCE(SUM(s.damage), 0) AS damage, COALESCE(SUM(s.kills), 0) AS kills FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE m.guild_id=? AND m.mode=?",
        (interaction.guild_id, mode.value),
    ).fetchone()
    await send_response(interaction, f"**{mode_label(mode.value)} overview**\nMatches: **{row['matches']}** · Players: **{row['players']}**\nTotal kills: **{row['kills']}** · Total damage: **{row['damage']}**")


@insights_group.command(name="clutch", description="Rank players in close matches")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_clutch(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    matches = bot.database.connection.execute("SELECT id, winner, team_one, team_two FROM matches WHERE guild_id=? AND mode=? ORDER BY id DESC", (interaction.guild_id, mode.value)).fetchall()
    records: dict[int, list[int]] = {}
    for match in matches:
        stats = bot.database.connection.execute("SELECT user_id, score FROM match_player_stats WHERE guild_id=? AND match_id=?", (interaction.guild_id, match["id"])).fetchall()
        first = {int(value) for value in match["team_one"].split(",")}
        first_score = sum(row["score"] for row in stats if row["user_id"] in first)
        second_score = sum(row["score"] for row in stats if row["user_id"] not in first)
        if abs(first_score - second_score) > max(25, (first_score + second_score) * 0.10):
            continue
        for row in stats:
            own_team = 1 if row["user_id"] in first else 2
            values = records.setdefault(row["user_id"], [0, 0])
            values[0] += 1
            values[1] += int(own_team == match["winner"])
    ranked = sorted(((player_id, close, wins) for player_id, (close, wins) in records.items() if close >= 2), key=lambda item: (item[2] / item[1], item[1]), reverse=True)[:10]
    if not ranked:
        await send_response(interaction, f"Not enough close **{mode_label(mode.value)}** matches yet. Two close games are needed per player.")
        return
    lines = [f"**{index}.** <@{player_id}> — **{wins}-{close - wins}** in close games ({wins / close:.0%})" for index, (player_id, close, wins) in enumerate(ranked, 1)]
    await send_response(interaction, f"🔥 **Clutch rankings — {mode_label(mode.value)}**\n" + "\n".join(lines))


@insights_group.command(name="improvement", description="Give a player data-based areas to improve")
@app_commands.describe(mode="Game mode", player="Optional player; defaults to you")
@app_commands.choices(mode=mode_choices)
async def insights_improvement(interaction: discord.Interaction, mode: app_commands.Choice[str], player: discord.Member | None = None):
    player = player or interaction.user
    summary = bot.database.player_stat_summary(interaction.guild_id, player.id, mode.value)
    if not summary or not summary["matches"]:
        await send_response(interaction, f"No recorded stats for {player.mention} in **{mode_label(mode.value)}** yet.")
        return
    games = summary["matches"]
    kills = summary["kills"] or 0
    deaths = summary["deaths"] or 0
    damage = summary["damage"] or 0
    assists = summary["assists"] or 0
    kd = kills / max(1, deaths)
    advice = []
    if kd < 1:
        advice.append("survival and winning more engagements")
    if damage / games < 400:
        advice.append("dealing more damage before taking risks")
    if mode.value.startswith("control_") and (summary["captures"] or 0) / games < 1:
        advice.append("playing the objective and contesting hills")
    if mode.value == "gnashers_2v2" and assists / games < 2:
        advice.append("creating more team-shot opportunities")
    if not advice:
        advice.append("maintaining consistency while pushing your strongest metric")
    await send_response(interaction, f"**{player.display_name} improvement report — {mode_label(mode.value)}**\nRecord: **{summary['matches']}** games · K/D **{kd:.2f}** · Damage/game **{damage / games:.0f}**\nFocus next: **" + ", ".join(advice) + "**\n_Data-driven coaching based on your recorded match stats._")


@insights_group.command(name="top_damage", description="Rank players by total damage")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_damage(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "damage", "damage")


@insights_group.command(name="top_kills", description="Rank players by total kills")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_kills(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "kills", "kills")


@insights_group.command(name="top_score", description="Rank players by total score")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_score(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "score", "score")


@insights_group.command(name="top_assists", description="Rank players by total assists")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_assists(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "assists", "assists")


@insights_group.command(name="top_captures", description="Rank Control players by captures")
@app_commands.describe(mode="Control mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_captures(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "captures", "captures")


@insights_group.command(name="top_breaks", description="Rank Control players by hill breaks")
@app_commands.describe(mode="Control mode")
@app_commands.choices(mode=mode_choices)
async def insights_top_breaks(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await _send_metric_leaders(interaction, mode, "breaks", "breaks")


@insights_group.command(name="kd", description="Rank players by kill/death ratio")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_kd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.connection.execute("SELECT user_id, SUM(kills) AS kills, SUM(deaths) AS deaths FROM match_player_stats WHERE guild_id=? AND mode=? GROUP BY user_id ORDER BY CAST(kills AS REAL) / MAX(deaths, 1) DESC LIMIT 10", (interaction.guild_id, mode.value)).fetchall()
    lines = [f"**{index}.** <@{row['user_id']}> — **{row['kills'] / max(row['deaths'], 1):.2f} K/D** ({row['kills']}-{row['deaths']})" for index, row in enumerate(rows, 1)]
    await send_response(interaction, f"**Kill/death leaders — {mode_label(mode.value)}**\n" + ("\n".join(lines) if lines else "No data yet."))


@insights_group.command(name="winrate", description="Rank players by win rate")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_winrate(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.leaderboard(interaction.guild_id, mode.value, "winrate", 10)
    lines = [f"**{index}.** <@{row['user_id']}> — **{row['wins'] / max(row['games'], 1) * 100:.1f}%** ({row['wins']}-{row['losses']})" for index, row in enumerate(rows, 1)]
    await send_response(interaction, f"**Win-rate leaders — {mode_label(mode.value)}**\n" + ("\n".join(lines) if lines else "No ratings yet."))


@insights_group.command(name="peak", description="Show a player's peak Elo")
@app_commands.describe(player="Player")
async def insights_peak(interaction: discord.Interaction, player: discord.Member):
    rows = bot.database.profile_rows(interaction.guild_id, player.id)
    if not rows:
        await send_response(interaction, f"{player.mention} has no recorded ratings yet.")
        return
    best = max(rows, key=lambda row: row["peak_rating"])
    await send_response(interaction, f"**{player.display_name} peak Elo**\n{mode_label(best['mode'])}: **{best['peak_rating']}** (current: {best['rating']})")


@insights_group.command(name="recent", description="Show the latest results in a mode")
@app_commands.describe(mode="Optional game mode", limit="Number of matches")
@app_commands.choices(mode=mode_choices)
async def insights_recent(interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None, limit: int = 5):
    rows = bot.database.match_history(interaction.guild_id, mode.value if mode else None, max(1, min(limit, 10)))
    lines = [f"**#{row['id']}** {mode_label(row['mode'])} — Team {row['winner']} won ({row['map_name']})" for row in rows]
    await send_response(interaction, "**Recent results**\n" + ("\n".join(lines) if lines else "No matches recorded yet."))


@insights_group.command(name="lastmatch", description="Show the latest recorded match")
async def insights_lastmatch(interaction: discord.Interaction):
    rows = bot.database.match_history(interaction.guild_id, limit=1)
    if not rows:
        await send_response(interaction, "No matches recorded yet.")
        return
    row = rows[0]
    await send_response(interaction, f"**Latest match #{row['id']}**\nMode: **{mode_label(row['mode'])}** · Map: **{row['map_name']}** · Team **{row['winner']}** won")


@insights_group.command(name="maps", description="Rank maps by match count")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_maps(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.map_stats(interaction.guild_id, mode.value)
    lines = [f"**{index}.** {row['map_name']} — {row['games']} games ({row['team_one_wins']}-{row['team_two_wins']})" for index, row in enumerate(rows, 1)]
    await send_response(interaction, f"**Map rankings — {mode_label(mode.value)}**\n" + ("\n".join(lines) if lines else "No map data yet."))


@insights_group.command(name="teams", description="Rank recurring teams by wins")
@app_commands.describe(mode="Game mode")
@app_commands.choices(mode=mode_choices)
async def insights_teams(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    rows = bot.database.team_leaderboard(interaction.guild_id, mode.value, 10)
    lines = [f"**{index}.** {' + '.join(f'<@{value}>' for value in row['team_key'].split(','))} — {row['wins']}-{row['losses']} ({row['games']} games)" for index, row in enumerate(rows, 1)]
    await send_response(interaction, f"**Team rankings — {mode_label(mode.value)}**\n" + ("\n".join(lines) if lines else "No recurring teams yet."))


@insights_group.command(name="pending", description="List pending match confirmations")
async def insights_pending(interaction: discord.Interaction):
    rows = bot.database.connection.execute("SELECT id, mode, winner, confirmed_by FROM pending_matches WHERE guild_id=? ORDER BY id DESC LIMIT 15", (interaction.guild_id,)).fetchall()
    lines = [f"**#{row['id']}** {mode_label(row['mode'])} — Team {row['winner']} · {len(json.loads(row['confirmed_by']))}/2 confirmations" for row in rows]
    await send_response(interaction, "**Pending matches**\n" + ("\n".join(lines) if lines else "No pending matches."))


def _ops_count(table: str, guild_id: int) -> int:
    return bot.database.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id=?", (guild_id,)).fetchone()[0]


@ops_group.command(name="summary", description="Show server-wide bot and match totals")
async def ops_summary(interaction: discord.Interaction):
    matches = _ops_count("matches", interaction.guild_id)
    players = bot.database.connection.execute("SELECT COUNT(DISTINCT user_id) FROM ratings WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    await send_response(interaction, f"**Server summary**\nRecorded matches: **{matches}** · Rated players: **{players}** · Modes: **{len(MODES)}**")


@ops_group.command(name="players", description="Count rated and profiled players")
async def ops_players(interaction: discord.Interaction):
    rated = bot.database.connection.execute("SELECT COUNT(DISTINCT user_id) FROM ratings WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    profiled = _ops_count("player_profiles", interaction.guild_id)
    await send_response(interaction, f"**Player counts**\nRated players: **{rated}** · Profiles: **{profiled}**")


@ops_group.command(name="activity", description="Show recent server match activity")
async def ops_activity(interaction: discord.Interaction):
    rows = bot.database.connection.execute("SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY day ORDER BY day DESC LIMIT 7", (interaction.guild_id,)).fetchall()
    await send_response(interaction, "**Recent activity**\n" + ("\n".join(f"{row['day']} — **{row['games']}** matches" for row in rows) if rows else "No match activity yet."))


@ops_group.command(name="database", description="Show database record counts")
async def ops_database(interaction: discord.Interaction):
    counts = [f"{table}: **{_ops_count(table, interaction.guild_id)}**" for table in ("matches", "ratings", "team_performance", "pending_matches", "audit_log")]
    await send_response(interaction, "**Database summary**\n" + " · ".join(counts))


@ops_group.command(name="backups", description="Show available database backups")
async def ops_backups(interaction: discord.Interaction):
    files = list(BACKUP_DIRECTORY.glob("*.sqlite3")) if BACKUP_DIRECTORY.is_dir() else []
    await send_response(interaction, f"Available database backups: **{len(files)}**")


@ops_group.command(name="seasons", description="Show season status and count")
async def ops_seasons(interaction: discord.Interaction):
    active = bot.database.active_season(interaction.guild_id)
    total = _ops_count("seasons", interaction.guild_id)
    await send_response(interaction, f"Seasons created: **{total}**\nActive season: **{active['name'] if active else 'None'}**")


@ops_group.command(name="teams", description="Count saved and recurring teams")
async def ops_teams(interaction: discord.Interaction):
    presets = _ops_count("team_presets", interaction.guild_id)
    recurring = _ops_count("team_performance", interaction.guild_id)
    await send_response(interaction, f"Saved presets: **{presets}** · Recurring teams: **{recurring}**")


@ops_group.command(name="maps", description="Show map rotation status")
async def ops_maps(interaction: discord.Interaction):
    row = bot.database.connection.execute("SELECT maps, position FROM map_rotation WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    if not row:
        await send_response(interaction, "No custom map rotation is configured.")
        return
    maps = json.loads(row["maps"])
    await send_response(interaction, f"Map rotation: **{len(maps)}** maps · Next position: **{row['position'] + 1}**")


@ops_group.command(name="pending", description="Count pending matches and votes")
async def ops_pending(interaction: discord.Interaction):
    await send_response(interaction, f"Pending matches: **{_ops_count('pending_matches', interaction.guild_id)}** · Votes: **{_ops_count('match_votes', interaction.guild_id)}**")


@ops_group.command(name="lobbies", description="Count match lobbies")
async def ops_lobbies(interaction: discord.Interaction):
    await send_response(interaction, f"Match lobbies: **{_ops_count('lobby_sessions', interaction.guild_id)}**")


@ops_group.command(name="tournaments", description="Count tournaments and registrations")
async def ops_tournaments(interaction: discord.Interaction):
    await send_response(interaction, f"Tournaments: **{_ops_count('tournaments', interaction.guild_id)}** · Registrations: **{bot.database.connection.execute('SELECT COUNT(*) FROM tournament_entries e JOIN tournaments t ON t.id=e.tournament_id WHERE t.guild_id=?', (interaction.guild_id,)).fetchone()[0]}**")


@ops_group.command(name="series", description="Count best-of series")
async def ops_series(interaction: discord.Interaction):
    await send_response(interaction, f"Best-of series: **{_ops_count('series', interaction.guild_id)}**")


@ops_group.command(name="challenges", description="Count player challenges")
async def ops_challenges(interaction: discord.Interaction):
    await send_response(interaction, f"Player challenges: **{_ops_count('challenges', interaction.guild_id)}**")


@ops_group.command(name="queue", description="Show persistent matchmaking queues")
async def ops_queue(interaction: discord.Interaction):
    queue_rows = bot.database.connection.execute("SELECT mode, COUNT(*) AS players FROM matchmaking_queue WHERE guild_id=? GROUP BY mode ORDER BY mode", (interaction.guild_id,)).fetchall()
    rows = [f"{mode_label(row['mode'])}: **{row['players']}**" for row in queue_rows]
    await send_response(interaction, "**Matchmaking queues**\n" + ("\n".join(rows) if rows else "All queues are empty."))


@ops_group.command(name="commands", description="Show the bot's top-level command count")
async def ops_commands(interaction: discord.Interaction):
    await send_response(interaction, f"This bot currently exposes **{len(bot.tree.get_commands())}** top-level slash-command groups.")


def _report_count(table: str, guild_id: int) -> int:
    return bot.database.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE guild_id=?", (guild_id,)).fetchone()[0]


def _report_stat(column: str, guild_id: int) -> int:
    row = bot.database.connection.execute(
        f"SELECT COALESCE(SUM({column}), 0) FROM match_player_stats WHERE guild_id=?", (guild_id,)
    ).fetchone()
    return row[0]


def _register_report_command(name: str, description: str, callback):
    async def command(interaction: discord.Interaction):
        await callback(interaction)
    command.__name__ = f"reports_{name}"
    reports_group.add_command(app_commands.Command(name=name, description=description, callback=command))


def _register_tool_command(name: str, description: str, callback):
    async def command(interaction: discord.Interaction):
        await callback(interaction)
    command.__name__ = f"tools_{name}"
    tools_group.add_command(app_commands.Command(name=name, description=description, callback=command))


def _count_report(table: str, label: str):
    async def callback(interaction):
        await send_response(interaction, f"{label}: **{_report_count(table, interaction.guild_id)}**")
    return callback


for _name, _table, _label in (
    ("match_count", "matches", "Recorded matches"), ("player_count", "player_profiles", "Player profiles"),
    ("rating_count", "ratings", "Rating rows"), ("team_count", "team_performance", "Recurring teams"),
    ("pending_count", "pending_matches", "Pending matches"), ("vote_count", "match_votes", "Match votes"),
    ("season_count", "seasons", "Seasons"), ("tournament_count", "tournaments", "Tournaments"),
    ("lobby_count", "lobby_sessions", "Lobbies"), ("series_count", "series", "Series"),
    ("challenge_count", "challenges", "Challenges"),
    ("preset_count", "team_presets", "Team presets"), ("veto_count", "veto_sessions", "Veto sessions"),
    ("audit_count", "audit_log", "Audit entries"),
):
    _register_report_command(_name, f"Count { _label.lower() }", _count_report(_table, _label))


for _name, _column, _label in (
    ("total_kills", "kills", "Total kills"), ("total_deaths", "deaths", "Total deaths"),
    ("total_damage", "damage", "Total damage"), ("total_score", "score", "Total score"),
    ("total_assists", "assists", "Total assists"), ("total_captures", "captures", "Total captures"),
    ("total_breaks", "breaks", "Total hill breaks"),
):
    async def _stat_callback(interaction, column=_column, label=_label):
        await send_response(interaction, f"{label}: **{_report_stat(column, interaction.guild_id):,}**")
    _register_report_command(_name, f"Show { _label.lower() }", _stat_callback)


async def _report_latest(interaction):
    row = bot.database.connection.execute("SELECT id, mode, winner, map_name, created_at FROM matches WHERE guild_id=? ORDER BY id DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No matches recorded yet." if not row else f"Latest match: **#{row['id']}** · {mode_label(row['mode'])} · Team {row['winner']} won · {row['map_name']} · {row['created_at'][:16]}")


async def _report_oldest(interaction):
    row = bot.database.connection.execute("SELECT id, mode, winner, map_name, created_at FROM matches WHERE guild_id=? ORDER BY id LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No matches recorded yet." if not row else f"Oldest match: **#{row['id']}** · {mode_label(row['mode'])} · Team {row['winner']} won · {row['map_name']} · {row['created_at'][:16]}")


async def _report_activity_days(interaction):
    days = bot.database.connection.execute("SELECT COUNT(DISTINCT substr(created_at, 1, 10)) FROM matches WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    await send_response(interaction, f"Active match days: **{days}**")


async def _report_mode_breakdown(interaction):
    rows = bot.database.connection.execute("SELECT mode, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY mode ORDER BY games DESC", (interaction.guild_id,)).fetchall()
    await send_response(interaction, "**Mode breakdown**\n" + ("\n".join(f"{mode_label(row['mode'])}: **{row['games']}**" for row in rows) if rows else "No matches recorded yet."))


for _name, _description, _callback in (
    ("latest", "Show the latest match", _report_latest), ("oldest", "Show the oldest match", _report_oldest),
    ("activity_days", "Count days with recorded matches", _report_activity_days),
    ("mode_breakdown", "Break down matches by mode", _report_mode_breakdown),
):
    _register_report_command(_name, _description, _callback)


async def _tool_identity(interaction):
    await send_response(interaction, f"Logged in as **{bot.user}**")


async def _tool_guild_id(interaction):
    await send_response(interaction, f"Server ID: **{interaction.guild_id}**")


async def _tool_latency(interaction):
    await send_response(interaction, f"Gateway latency: **{bot.latency * 1000:.0f} ms**")


async def _tool_configured_modes(interaction):
    await send_response(interaction, "Configured modes: " + ", ".join(mode_label(mode) for mode in MODES))


async def _tool_current_season(interaction):
    season = bot.database.active_season(interaction.guild_id)
    await send_response(interaction, f"Active season: **{season['name'] if season else 'None'}**")


async def _tool_database(interaction):
    await send_response(interaction, f"Database: **{DATABASE_PATH.resolve()}** · Size: **{DATABASE_PATH.stat().st_size:,} bytes**")


async def _tool_backup_count(interaction):
    files = list(BACKUP_DIRECTORY.glob("*.sqlite3")) if BACKUP_DIRECTORY.is_dir() else []
    await send_response(interaction, f"Database backups: **{len(files)}**")


async def _tool_queue_size(interaction):
    total = bot.database.connection.execute("SELECT COUNT(*) FROM matchmaking_queue WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    await send_response(interaction, f"Queued players: **{total}**")


async def _tool_server_settings(interaction):
    row = bot.database.connection.execute("SELECT * FROM elo_settings WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "Elo settings configured." if row else "Using default Elo settings.")


async def _tool_command_groups(interaction):
    await send_response(interaction, f"Top-level command groups: **{len(bot.tree.get_commands())}**")


async def _tool_sqlite_version(interaction):
    version = bot.database.connection.execute("SELECT sqlite_version()").fetchone()[0]
    await send_response(interaction, f"SQLite version: **{version}**")


async def _tool_uptime(interaction):
    await send_response(interaction, "Bot process is online and ready.")


async def _tool_latest_backup(interaction):
    files = list(BACKUP_DIRECTORY.glob("*.sqlite3")) if BACKUP_DIRECTORY.is_dir() else []
    latest = max(files, key=lambda path: path.stat().st_mtime) if files else None
    await send_response(interaction, f"Latest backup: **{latest.name}**" if latest else "No backups found.")


async def _tool_database_tables(interaction):
    count = bot.database.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    await send_response(interaction, f"Database tables: **{count}**")


for _name, _description, _callback in (
    ("identity", "Show the bot identity", _tool_identity), ("guild_id", "Show this server ID", _tool_guild_id),
    ("latency", "Show gateway latency", _tool_latency), ("configured_modes", "List configured modes", _tool_configured_modes),
    ("current_season", "Show the active season", _tool_current_season), ("database", "Show database path", _tool_database),
    ("database_size", "Show database size", _tool_database), ("backup_count", "Count database backups", _tool_backup_count),
    ("queue_size", "Count queued players", _tool_queue_size), ("scheduled_count", "Count scheduled matches", _count_report("scheduled_matches", "Scheduled matches")),
    ("announcement_count", "Count announcements", _count_report("announcement_schedules", "Announcements")),
    ("availability_count", "Count availability entries", _count_report("availability", "Availability entries")),
    ("open_series", "Count active series", _count_report("series", "Series")), ("open_lobbies", "Count lobbies", _count_report("lobby_sessions", "Lobbies")),
    ("open_tournaments", "Count tournaments", _count_report("tournaments", "Tournaments")), ("open_challenges", "Count challenges", _count_report("challenges", "Challenges")),
    ("open_vetoes", "Count veto sessions", _count_report("veto_sessions", "Veto sessions")), ("audit_count", "Count audit entries", _count_report("audit_log", "Audit entries")),
    ("server_settings", "Check server settings", _tool_server_settings), ("elo_settings", "Check Elo settings", _tool_server_settings),
    ("command_groups", "Count command groups", _tool_command_groups), ("sqlite_version", "Show SQLite version", _tool_sqlite_version),
    ("uptime", "Check bot readiness", _tool_uptime), ("latest_backup", "Show latest backup", _tool_latest_backup),
    ("database_tables", "Count database tables", _tool_database_tables),
):
    _register_tool_command(_name, _description, _callback)


def _register_readonly_feature(group, name: str, description: str, callback):
    """Register compact, read-only commands without adding more top-level groups."""
    async def command(interaction: discord.Interaction):
        await callback(interaction)
    command.__name__ = f"{group.name}_{name}"
    group.add_command(app_commands.Command(name=name, description=description, callback=command))


def _feature_rows(sql: str, guild_id: int, params: tuple = ()):
    return bot.database.connection.execute(sql, (guild_id, *params)).fetchall()


async def _analytics_career(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) AS matches, COUNT(DISTINCT user_id) AS players FROM matches m LEFT JOIN match_player_stats s ON s.match_id=m.id WHERE m.guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"**Career overview**\nMatches: **{row['matches']}** · Players with stats: **{row['players']}**")


async def _analytics_mode_mix(interaction):
    rows = _feature_rows("SELECT mode, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY mode ORDER BY games DESC", interaction.guild_id)
    await send_response(interaction, "**Mode mix**\n" + ("\n".join(f"{mode_label(r['mode'])}: **{r['games']}**" for r in rows) or "No matches recorded yet."))


async def _analytics_map_winrate(interaction):
    rows = _feature_rows("SELECT mode, map_name, COUNT(*) AS games, AVG(winner=1) * 100.0 AS team_one_rate FROM matches WHERE guild_id=? GROUP BY mode, map_name ORDER BY games DESC, mode, map_name LIMIT 20", interaction.guild_id)
    await send_response(interaction, "**Map win balance**\n" + ("\n".join(f"{mode_label(r['mode'])} · {r['map_name']}: **{r['team_one_rate']:.0f}%** Team 1 over {r['games']} games" for r in rows) or "No map data yet."))


def _analytics_metric(column: str, label: str, aggregate: str = "SUM"):
    async def callback(interaction):
        rows = _feature_rows(f"SELECT user_id, COUNT(*) AS games, {aggregate}({column}) AS value FROM match_player_stats WHERE guild_id=? GROUP BY user_id ORDER BY value DESC LIMIT 10", interaction.guild_id)
        await send_response(interaction, f"**{label} leaders**\n" + ("\n".join(f"**{i}.** <@{r['user_id']}> — **{float(r['value']):,.1f}** ({r['games']} games)" for i, r in enumerate(rows, 1)) or "No stat data yet."))
    return callback


async def _analytics_kd(interaction):
    rows = _feature_rows("SELECT user_id, SUM(kills) AS kills, SUM(deaths) AS deaths FROM match_player_stats WHERE guild_id=? GROUP BY user_id ORDER BY CAST(kills AS REAL)/MAX(deaths,1) DESC LIMIT 10", interaction.guild_id)
    await send_response(interaction, "**All-mode K/D leaders**\n" + ("\n".join(f"**{i}.** <@{r['user_id']}> — **{r['kills']/max(r['deaths'], 1):.2f}** ({r['kills']}-{r['deaths']})" for i, r in enumerate(rows, 1)) or "No stat data yet."))


async def _analytics_efficiency(interaction):
    rows = _feature_rows("SELECT user_id, COUNT(*) AS games, SUM(damage) AS damage, SUM(score) AS score FROM match_player_stats WHERE guild_id=? GROUP BY user_id ORDER BY CAST(damage AS REAL)/MAX(score,1) DESC LIMIT 10", interaction.guild_id)
    await send_response(interaction, "**Damage efficiency**\n" + ("\n".join(f"**{i}.** <@{r['user_id']}> — **{r['damage']/max(r['score'], 1):.2f} damage/score**" for i, r in enumerate(rows, 1)) or "No stat data yet."))


async def _analytics_undefeated(interaction):
    rows = _feature_rows("SELECT user_id, wins, losses, rating FROM ratings WHERE guild_id=? AND wins>0 AND losses=0 ORDER BY wins DESC, rating DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Undefeated players**\n" + ("\n".join(f"<@{r['user_id']}> — **{r['wins']}-0**, {r['rating']} Elo" for r in rows) or "No undefeated records yet."))


async def _analytics_deathless(interaction):
    rows = _feature_rows("SELECT user_id, COUNT(*) AS games, SUM(deaths) AS deaths FROM match_player_stats WHERE guild_id=? GROUP BY user_id HAVING deaths=0 ORDER BY games DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Deathless careers**\n" + ("\n".join(f"<@{r['user_id']}> — **{r['games']}** recorded games without a death" for r in rows) or "No deathless careers yet."))


async def _analytics_peak_mode(interaction):
    rows = _feature_rows("SELECT user_id, mode, peak_rating FROM ratings WHERE guild_id=? ORDER BY peak_rating DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Peak Elo by mode**\n" + ("\n".join(f"<@{r['user_id']}> · {mode_label(r['mode'])}: **{r['peak_rating']}**" for r in rows) or "No ratings yet."))


async def _analytics_activity_hours(interaction):
    rows = _feature_rows("SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY hour ORDER BY games DESC LIMIT 10", interaction.guild_id)
    await send_response(interaction, "**Most active UTC hours**\n" + ("\n".join(f"{r['hour']}:00 UTC — **{r['games']}** matches" for r in rows) or "No activity yet."))


async def _analytics_weekdays(interaction):
    rows = _feature_rows("SELECT strftime('%w', created_at) AS day, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY day ORDER BY games DESC", interaction.guild_id)
    names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    await send_response(interaction, "**Matchdays**\n" + ("\n".join(f"{names[int(r['day'])]} — **{r['games']}** matches" for r in rows) or "No activity yet."))


async def _analytics_maps_played(interaction):
    rows = _feature_rows("SELECT map_name, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY map_name ORDER BY games DESC LIMIT 20", interaction.guild_id)
    await send_response(interaction, "**Map pool usage**\n" + ("\n".join(f"{i}. {r['map_name']} — **{r['games']}**" for i, r in enumerate(rows, 1)) or "No maps recorded yet."))


async def _analytics_first_games(interaction):
    rows = _feature_rows("SELECT user_id, games, provisional_games, rating FROM ratings WHERE guild_id=? ORDER BY games ASC, user_id LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Newest competitors**\n" + ("\n".join(f"<@{r['user_id']}> — **{r['games']}** games · {r['rating']} Elo" for r in rows) or "No players yet."))


async def _analytics_grinders(interaction):
    rows = _feature_rows("SELECT user_id, SUM(games) AS games, MAX(rating) AS best_rating FROM ratings WHERE guild_id=? GROUP BY user_id ORDER BY games DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Most active competitors**\n" + ("\n".join(f"**{i}.** <@{r['user_id']}> — **{r['games']}** games · peak current-mode Elo {r['best_rating']}" for i, r in enumerate(rows, 1)) or "No players yet."))


async def _analytics_data_quality(interaction):
    total = bot.database.connection.execute("SELECT COUNT(*) FROM matches WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    stats = bot.database.connection.execute("SELECT COUNT(DISTINCT match_id) FROM match_player_stats WHERE guild_id=?", (interaction.guild_id,)).fetchone()[0]
    unknown = bot.database.connection.execute("SELECT COUNT(*) FROM matches WHERE guild_id=? AND (map_name='' OR map_name='Unknown')", (interaction.guild_id,)).fetchone()[0]
    await send_response(interaction, f"**Data quality**\nMatches: **{total}** · Matches with stat rows: **{stats}** · Unknown maps: **{unknown}**")


async def _analytics_team_depth(interaction):
    rows = _feature_rows("SELECT mode, COUNT(*) AS teams FROM team_performance WHERE guild_id=? GROUP BY mode ORDER BY teams DESC", interaction.guild_id)
    await send_response(interaction, "**Team depth**\n" + ("\n".join(f"{mode_label(r['mode'])}: **{r['teams']}** recurring rosters" for r in rows) or "No recurring teams yet."))


async def _analytics_top_rating_delta(interaction):
    rows = _feature_rows("SELECT user_id, SUM(CASE WHEN rating_delta>0 THEN rating_delta ELSE 0 END) AS gains, SUM(CASE WHEN rating_delta<0 THEN rating_delta ELSE 0 END) AS losses FROM match_player_stats WHERE guild_id=? GROUP BY user_id ORDER BY gains DESC LIMIT 10", interaction.guild_id)
    await send_response(interaction, "**Career Elo gains**\n" + ("\n".join(f"<@{r['user_id']}> — **+{r['gains']}** gained · {r['losses']} lost" for r in rows) or "No Elo history yet."))


async def _analytics_recent_winners(interaction):
    rows = _feature_rows("SELECT winner, COUNT(*) AS wins FROM matches WHERE guild_id=? GROUP BY winner ORDER BY wins DESC", interaction.guild_id)
    await send_response(interaction, "**Team-side results**\n" + ("\n".join(f"Team {r['winner']} wins: **{r['wins']}**" for r in rows) or "No matches yet."))


async def _analytics_season_count(interaction):
    rows = _feature_rows("SELECT name, started_at, ended_at FROM seasons WHERE guild_id=? ORDER BY id DESC LIMIT 10", interaction.guild_id)
    await send_response(interaction, "**Season history**\n" + ("\n".join(f"{r['name']} — {r['started_at'][:10]} to {(r['ended_at'] or 'active')[:10]}" for r in rows) or "No seasons created yet."))


async def _analytics_pending_age(interaction):
    rows = _feature_rows("SELECT id, mode, created_at FROM pending_matches WHERE guild_id=? ORDER BY id", interaction.guild_id)
    await send_response(interaction, "**Pending match queue**\n" + ("\n".join(f"#{r['id']} · {mode_label(r['mode'])} · submitted {r['created_at'][:16]}" for r in rows) or "No pending matches."))


async def _analytics_match_size(interaction):
    rows = _feature_rows("SELECT mode, AVG((length(team_one)-length(replace(team_one, ',', ''))+1)+(length(team_two)-length(replace(team_two, ',', ''))+1)) AS players FROM matches WHERE guild_id=? GROUP BY mode", interaction.guild_id)
    await send_response(interaction, "**Average lobby size**\n" + ("\n".join(f"{mode_label(r['mode'])}: **{r['players']:.1f}** players" for r in rows) or "No matches yet."))


async def _analytics_top_maps(interaction):
    rows = _feature_rows("SELECT mode, map_name, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY mode, map_name ORDER BY games DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Favorite maps by mode**\n" + ("\n".join(f"{mode_label(r['mode'])}: {r['map_name']} (**{r['games']}** games)" for r in rows) or "No maps yet."))


async def _analytics_stat_averages(interaction):
    row = bot.database.connection.execute("SELECT AVG(kills), AVG(deaths), AVG(assists), AVG(damage), AVG(score) FROM match_player_stats WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"**Server stat averages**\nKills **{row[0] or 0:.1f}** · Deaths **{row[1] or 0:.1f}** · Assists **{row[2] or 0:.1f}** · Damage **{row[3] or 0:.1f}** · Score **{row[4] or 0:.1f}**")


async def _analytics_rating_spread(interaction):
    rows = _feature_rows("SELECT mode, MIN(rating) AS low, MAX(rating) AS high, AVG(rating) AS average FROM ratings WHERE guild_id=? GROUP BY mode", interaction.guild_id)
    await send_response(interaction, "**Rating spread**\n" + ("\n".join(f"{mode_label(r['mode'])}: **{r['low']}–{r['high']}** · average **{r['average']:.0f}**" for r in rows) or "No ratings yet."))


async def _community_roster(interaction):
    rows = _feature_rows("SELECT DISTINCT user_id FROM ratings WHERE guild_id=? ORDER BY user_id", interaction.guild_id)
    await send_response(interaction, "**Tracked roster**\n" + (" ".join(f"<@{r['user_id']}>" for r in rows) if rows else "No tracked players yet."))


async def _community_available(interaction):
    rows = _feature_rows("SELECT user_id, status FROM availability WHERE guild_id=? ORDER BY status, user_id", interaction.guild_id)
    await send_response(interaction, "**Availability board**\n" + ("\n".join(f"<@{r['user_id']}> — **{r['status']}**" for r in rows) or "Nobody has set availability yet."))


async def _community_queue_board(interaction):
    rows = _feature_rows("SELECT mode, COUNT(*) AS players FROM matchmaking_queue WHERE guild_id=? GROUP BY mode ORDER BY mode", interaction.guild_id)
    await send_response(interaction, "**Queue board**\n" + ("\n".join(f"{mode_label(r['mode'])}: **{r['players']}** queued" for r in rows) or "All queues are empty."))


async def _community_lfg(interaction):
    rows = _feature_rows("SELECT mode, message, created_at FROM lfg_requests WHERE guild_id=? ORDER BY id DESC LIMIT 10", interaction.guild_id) if bot.database.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lfg_requests'").fetchone() else []
    await send_response(interaction, "**Recent LFG posts**\n" + ("\n".join(f"{mode_label(r['mode'])}: {r['message']}" for r in rows) if rows else "No recent LFG posts."))


async def _community_match_calendar(interaction):
    rows = _feature_rows("SELECT substr(created_at,1,10) AS day, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY day ORDER BY day DESC LIMIT 14", interaction.guild_id)
    await send_response(interaction, "**Match calendar**\n" + ("\n".join(f"{r['day']} — **{r['games']}** matches" for r in rows) or "No matches recorded yet."))


async def _community_winners(interaction):
    rows = _feature_rows("SELECT m.winner, COUNT(*) AS games FROM matches m WHERE m.guild_id=? GROUP BY m.winner ORDER BY games DESC", interaction.guild_id)
    await send_response(interaction, "**Winning side scoreboard**\n" + ("\n".join(f"Team {r['winner']}: **{r['games']}** wins" for r in rows) or "No matches yet."))


async def _community_tournaments(interaction):
    rows = _feature_rows("SELECT name, mode, status FROM tournaments WHERE guild_id=? ORDER BY id DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Tournament board**\n" + ("\n".join(f"{r['name']} · {mode_label(r['mode'])} · **{r['status']}**" for r in rows) or "No tournaments yet."))


async def _community_series(interaction):
    rows = _feature_rows("SELECT id, mode, team_one_wins, team_two_wins, status FROM series WHERE guild_id=? ORDER BY id DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Series board**\n" + ("\n".join(f"Series #{r['id']} · {mode_label(r['mode'])} · **{r['team_one_wins']}-{r['team_two_wins']}** · {r['status']}" for r in rows) or "No series yet."))


async def _community_lobbies(interaction):
    rows = _feature_rows("SELECT id, mode, status, checked_in, no_shows FROM lobby_sessions WHERE guild_id=? ORDER BY id DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Lobby board**\n" + ("\n".join(f"Lobby #{r['id']} · {mode_label(r['mode'])} · **{r['status']}** · {len(json.loads(r['checked_in']))} checked in" for r in rows) or "No lobbies yet."))


async def _community_challenges(interaction):
    rows = _feature_rows("SELECT id, mode, challenger_id, opponent_id, status FROM challenges WHERE guild_id=? ORDER BY id DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Challenge board**\n" + ("\n".join(f"#{r['id']} · {mode_label(r['mode'])} · <@{r['challenger_id']}> vs <@{r['opponent_id']}> · **{r['status']}**" for r in rows) or "No challenges yet."))


async def _community_presets(interaction):
    rows = _feature_rows("SELECT name, mode, players FROM team_presets WHERE guild_id=? ORDER BY name LIMIT 20", interaction.guild_id)
    await send_response(interaction, "**Saved team presets**\n" + ("\n".join(f"{r['name']} · {mode_label(r['mode'])} · {len(r['players'].split(','))} players" for r in rows) or "No saved presets yet."))


async def _community_achievements(interaction):
    rows = _feature_rows("SELECT name, metric, threshold FROM custom_achievements WHERE guild_id=? ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Achievement board**\n" + ("\n".join(f"{r['name']} — {r['metric']} ≥ **{r['threshold']}**" for r in rows) or "No custom achievements yet."))


async def _community_replays(interaction):
    rows = _feature_rows("SELECT m.id, m.mode, m.map_name FROM matches m JOIN match_annotations a ON a.match_id=m.id WHERE m.guild_id=? AND a.replay_url<>'' ORDER BY m.id DESC LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Replay gallery**\n" + ("\n".join(f"Match #{r['id']} · {mode_label(r['mode'])} · {r['map_name']}" for r in rows) or "No replay links saved yet."))


async def _community_announcements(interaction):
    rows = _feature_rows("SELECT id, mode, metric, interval_minutes, enabled FROM announcement_schedules WHERE guild_id=? ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Scheduled announcements**\n" + ("\n".join(f"#{r['id']} · {mode_label(r['mode'])} · {r['metric']} every **{r['interval_minutes']}m** · {'on' if r['enabled'] else 'off'}" for r in rows) or "No scheduled announcements."))


async def _community_rotation(interaction):
    row = bot.database.connection.execute("SELECT maps, position FROM map_rotation WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    if not row:
        await send_response(interaction, "No map rotation configured.")
        return
    maps = json.loads(row['maps'])
    await send_response(interaction, f"**Map rotation**\nCurrent next map: **{maps[row['position'] % len(maps)] if maps else 'None'}** · {len(maps)} maps")


async def _community_modes(interaction):
    await send_response(interaction, "**Tracked modes**\n" + "\n".join(f"• {info['label']} — {team_size(mode)}v{team_size(mode)}" for mode, info in MODES.items()))


async def _community_welcome(interaction):
    await send_response(interaction, "**Welcome to Gears 5 Elo**\nUse `/match record` to submit a game, `/stats leaderboard` to check rankings, `/queue join` to find players, and `/player profile` to see your record.")


async def _community_help(interaction):
    await send_response(interaction, "**Quick help**\nRecord: `/match record` · Rankings: `/stats leaderboard` · Player details: `/player profile` · Team tools: `/team balance` · Matchmaking: `/queue status` · Analytics: `/analytics career`")


_ANALYTICS_FEATURES = (
    ("career", "Show all-time career totals", _analytics_career), ("mode_mix", "Break down matches by mode", _analytics_mode_mix),
    ("map_balance", "Show map-side win balance", _analytics_map_winrate), ("kd", "Rank all-mode kill/death ratio", _analytics_kd),
    ("efficiency", "Rank damage efficiency", _analytics_efficiency), ("undefeated", "Show undefeated records", _analytics_undefeated),
    ("deathless", "Show deathless careers", _analytics_deathless), ("peak_modes", "Show peak Elo by mode", _analytics_peak_mode),
    ("hours", "Show the most active UTC hours", _analytics_activity_hours), ("weekdays", "Show the busiest matchdays", _analytics_weekdays),
    ("map_pool", "Show the server map pool", _analytics_maps_played), ("new_players", "Show players with the fewest games", _analytics_first_games),
    ("grinders", "Rank the most active competitors", _analytics_grinders), ("data_quality", "Check stat and map coverage", _analytics_data_quality),
    ("team_depth", "Count recurring rosters by mode", _analytics_team_depth), ("elo_gains", "Rank career Elo gains", _analytics_top_rating_delta),
    ("side_scoreboard", "Compare Team 1 and Team 2 wins", _analytics_recent_winners), ("season_history", "Show recent season history", _analytics_season_count),
    ("pending_age", "Show pending match submission times", _analytics_pending_age), ("lobby_size", "Show average lobby size", _analytics_match_size),
    ("top_maps", "Show favorite maps by mode", _analytics_top_maps), ("averages", "Show server stat averages", _analytics_stat_averages),
    ("rating_spread", "Show rating ranges by mode", _analytics_rating_spread), ("damage", "Rank total career damage", _analytics_metric("damage", "Damage")),
    ("score", "Rank total career score", _analytics_metric("score", "Score")),
)
for _name, _description, _callback in _ANALYTICS_FEATURES:
    _register_readonly_feature(analytics_group, _name, _description, _callback)

_COMMUNITY_FEATURES = (
    ("roster", "Show the tracked server roster", _community_roster), ("availability", "Show the availability board", _community_available),
    ("queue_board", "Show all matchmaking queues", _community_queue_board), ("lfg_board", "Show recent LFG posts", _community_lfg),
    ("calendar", "Show recent match days", _community_match_calendar), ("winners", "Show the winning-side scoreboard", _community_winners),
    ("tournaments", "Show the tournament board", _community_tournaments), ("series", "Show active and recent series", _community_series),
    ("lobbies", "Show recent match lobbies", _community_lobbies), ("challenges", "Show recent challenges", _community_challenges),
    ("presets", "Show saved team presets", _community_presets), ("achievements", "Show the achievement board", _community_achievements),
    ("replays", "Show saved replay links", _community_replays), ("announcements", "Show scheduled announcements", _community_announcements),
    ("rotation", "Show the current map rotation", _community_rotation), ("modes", "Explain tracked game modes", _community_modes),
    ("welcome", "Show a concise new-player guide", _community_welcome), ("help", "Show the most-used commands", _community_help),
    ("match_count", "Show the server match count", _count_report("matches", "Recorded matches")),
    ("player_count", "Show the tracked player count", _count_report("player_profiles", "Player profiles")),
    ("team_count", "Show the recurring team count", _count_report("team_performance", "Recurring teams")),
    ("map_count", "Show the recorded map count", _analytics_maps_played), ("queue_count", "Show queue totals", _community_queue_board),
    ("season_count", "Show the season count", _count_report("seasons", "Seasons")), ("health", "Show a compact server health snapshot", _analytics_data_quality),
)
for _name, _description, _callback in _COMMUNITY_FEATURES:
    _register_readonly_feature(community_group, _name, _description, _callback)


async def _room_counts(interaction):
    tables = ("matches", "pending_matches", "lobby_sessions", "series", "tournaments", "challenges")
    text = " · ".join(f"{table.replace('_', ' ').title()}: **{_ops_count(table, interaction.guild_id)}**" for table in tables)
    await send_response(interaction, f"**Matchroom counts**\n{text}")


async def _room_scheduled(interaction):
    rows = _feature_rows("SELECT id, mode, scheduled_at FROM scheduled_matches WHERE guild_id=? AND notified=0 ORDER BY scheduled_at LIMIT 15", interaction.guild_id)
    await send_response(interaction, "**Upcoming scheduled matches**\n" + ("\n".join(f"#{r['id']} · {mode_label(r['mode'])} · {r['scheduled_at'][:16]}" for r in rows) or "No upcoming matches."))


async def _room_queues_age(interaction):
    rows = _feature_rows("SELECT mode, user_id, joined_at FROM matchmaking_queue WHERE guild_id=? ORDER BY joined_at LIMIT 20", interaction.guild_id)
    await send_response(interaction, "**Queue order**\n" + ("\n".join(f"{mode_label(r['mode'])} · <@{r['user_id']}> joined {r['joined_at'][:16]}" for r in rows) or "All queues are empty."))


async def _room_vetoes(interaction):
    rows = _feature_rows("SELECT id, mode, status, picked FROM veto_sessions WHERE guild_id=? AND status='open' ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Open map vetoes**\n" + ("\n".join(f"Veto #{r['id']} · {mode_label(r['mode'])} · {r['status']} · pick: {r['picked'] or 'pending'}" for r in rows) or "No open vetoes."))


async def _room_drafts(interaction):
    rows = _feature_rows("SELECT id, mode, turn, status FROM drafts WHERE guild_id=? AND status='open' ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Open drafts**\n" + ("\n".join(f"Draft #{r['id']} · {mode_label(r['mode'])} · turn {r['turn']}" for r in rows) or "No open drafts."))


async def _room_annotations(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) FROM match_annotations WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"Saved match annotations: **{row[0]}**")


async def _room_replay_rate(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN a.replay_url<>'' THEN 1 ELSE 0 END) AS linked FROM matches m LEFT JOIN match_annotations a ON a.match_id=m.id WHERE m.guild_id=?", (interaction.guild_id,)).fetchone()
    rate = (row['linked'] or 0) / max(row['total'], 1) * 100
    await send_response(interaction, f"**Replay coverage**\n{row['linked'] or 0}/{row['total']} matches linked (**{rate:.1f}%**)")


async def _room_match_rate(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) AS games, COUNT(DISTINCT substr(created_at,1,10)) AS days FROM matches WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"**Match cadence**\n{row['games']} matches across {row['days']} active days · average **{row['games']/max(row['days'],1):.1f}** per active day")


async def _room_unique_opponents(interaction):
    row = bot.database.connection.execute("SELECT COUNT(DISTINCT user_id) FROM match_player_stats WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"Tracked competitors appearing in match stat lines: **{row[0]}**")


async def _room_latest_map(interaction):
    row = bot.database.connection.execute("SELECT map_name, mode, created_at FROM matches WHERE guild_id=? ORDER BY id DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No matches recorded yet." if not row else f"Latest map: **{row['map_name']}** · {mode_label(row['mode'])} · {row['created_at'][:16]}")


async def _room_latest_season(interaction):
    row = bot.database.connection.execute("SELECT name, started_at, ended_at FROM seasons WHERE guild_id=? ORDER BY id DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No seasons created yet." if not row else f"Latest season: **{row['name']}** · started {row['started_at'][:10]} · {'active' if not row['ended_at'] else 'ended'}")


async def _room_active_tournaments(interaction):
    rows = _feature_rows("SELECT id, name, mode, status FROM tournaments WHERE guild_id=? AND status<>'complete' ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Active tournaments**\n" + ("\n".join(f"#{r['id']} {r['name']} · {mode_label(r['mode'])} · {r['status']}" for r in rows) or "No active tournaments."))


async def _room_active_series(interaction):
    rows = _feature_rows("SELECT id, mode, team_one_wins, team_two_wins FROM series WHERE guild_id=? AND status='open' ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Active series**\n" + ("\n".join(f"#{r['id']} · {mode_label(r['mode'])} · {r['team_one_wins']}-{r['team_two_wins']}" for r in rows) or "No active series."))


async def _room_open_challenges(interaction):
    rows = _feature_rows("SELECT id, mode, challenger_id, opponent_id FROM challenges WHERE guild_id=? AND status='pending' ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Pending challenges**\n" + ("\n".join(f"#{r['id']} · <@{r['challenger_id']}> vs <@{r['opponent_id']}> · {mode_label(r['mode'])}" for r in rows) or "No pending challenges."))


async def _room_active_lobbies(interaction):
    rows = _feature_rows("SELECT id, mode, status FROM lobby_sessions WHERE guild_id=? AND status IN ('scheduled','active') ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Active lobbies**\n" + ("\n".join(f"Lobby #{r['id']} · {mode_label(r['mode'])} · {r['status']}" for r in rows) or "No active lobbies."))


async def _room_pending_confirmations(interaction):
    rows = _feature_rows("SELECT id, mode, confirmed_by FROM pending_matches WHERE guild_id=? ORDER BY id DESC", interaction.guild_id)
    await send_response(interaction, "**Confirmation status**\n" + ("\n".join(f"Match #{r['id']} · {mode_label(r['mode'])} · **{len(json.loads(r['confirmed_by']))}/2** confirmations" for r in rows) or "No pending confirmations."))


async def _room_top_mode(interaction):
    row = bot.database.connection.execute("SELECT mode, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY mode ORDER BY games DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No matches recorded yet." if not row else f"Most-played mode: **{mode_label(row['mode'])}** with **{row['games']}** matches")


async def _room_top_map(interaction):
    row = bot.database.connection.execute("SELECT map_name, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY map_name ORDER BY games DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No map data yet." if not row else f"Most-played map: **{row['map_name']}** with **{row['games']}** matches")


async def _room_recent_day(interaction):
    row = bot.database.connection.execute("SELECT substr(created_at,1,10) AS day, COUNT(*) AS games FROM matches WHERE guild_id=? GROUP BY day ORDER BY day DESC LIMIT 1", (interaction.guild_id,)).fetchone()
    await send_response(interaction, "No matches recorded yet." if not row else f"Most recent active day: **{row['day']}** · **{row['games']}** matches")


async def _room_stats_coverage(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) AS matches, COUNT(DISTINCT match_id) AS covered FROM match_player_stats WHERE guild_id=?", (interaction.guild_id,)).fetchone()
    await send_response(interaction, f"**Stat-line coverage**\n{row['covered']} matches have player stats across {row['matches']} stat rows")


async def _room_mode_settings(interaction):
    rows = _feature_rows("SELECT mode, starting_rating, k_factor, rating_floor, provisional_games FROM elo_settings WHERE guild_id=? ORDER BY mode", interaction.guild_id)
    await send_response(interaction, "**Mode settings**\n" + ("\n".join(f"{mode_label(r['mode'])}: start {r['starting_rating']} · K {r['k_factor']} · floor {r['rating_floor']}" for r in rows) or "Default settings are in use."))


async def _room_db_indexes(interaction):
    row = bot.database.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'").fetchone()
    await send_response(interaction, f"Database performance indexes: **{row[0]}**")


_MATCHROOM_FEATURES = (
    ("counts", "Show live match-room record counts", _room_counts), ("scheduled", "Show upcoming scheduled matches", _room_scheduled),
    ("queue_order", "Show queue order and join times", _room_queues_age), ("vetoes", "Show open map vetoes", _room_vetoes),
    ("drafts", "Show open player drafts", _room_drafts), ("annotations", "Count saved match annotations", _room_annotations),
    ("replay_rate", "Show replay-link coverage", _room_replay_rate), ("cadence", "Show match cadence", _room_match_rate),
    ("competitors", "Count tracked competitors", _room_unique_opponents), ("latest_map", "Show the latest map played", _room_latest_map),
    ("latest_season", "Show the latest season", _room_latest_season), ("tournaments", "Show active tournaments", _room_active_tournaments),
    ("series", "Show active series", _room_active_series), ("challenges", "Show pending challenges", _room_open_challenges),
    ("lobbies", "Show active lobbies", _room_active_lobbies), ("confirmations", "Show pending confirmations", _room_pending_confirmations),
    ("top_mode", "Show the most-played mode", _room_top_mode), ("top_map", "Show the most-played map", _room_top_map),
    ("recent_day", "Show the latest active match day", _room_recent_day), ("stat_coverage", "Show player-stat coverage", _room_stats_coverage),
    ("settings", "Show configured Elo settings", _room_mode_settings), ("db_indexes", "Count database indexes", _room_db_indexes),
    ("match_total", "Show total recorded matches", _count_report("matches", "Recorded matches")), ("pending_total", "Show total pending matches", _count_report("pending_matches", "Pending matches")),
    ("lobby_total", "Show total lobbies", _count_report("lobby_sessions", "Lobbies")),
)
for _name, _description, _callback in _MATCHROOM_FEATURES:
    _register_readonly_feature(matchroom_group, _name, _description, _callback)


def _career_stat(column: str, label: str, average: bool = False):
    async def callback(interaction):
        aggregate = "AVG" if average else "SUM"
        row = bot.database.connection.execute(f"SELECT COUNT(*) AS games, {aggregate}({column}) AS value FROM match_player_stats WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()
        value = float(row['value'] or 0)
        await send_response(interaction, f"**Your {label}**\n{value:.1f} per game" if average else f"**Your {label}**\n**{value:,.0f}** across {row['games']} stat lines")
    return callback


async def _career_summary(interaction):
    rows = bot.database.profile_rows(interaction.guild_id, interaction.user.id)
    await send_response(interaction, "**Your career summary**\n" + ("\n".join(f"{mode_label(r['mode'])}: {r['rating']} Elo · {r['wins']}-{r['losses']} · {r['games']} games" for r in rows) or "You have no recorded games yet."))


async def _career_best_mode(interaction):
    rows = bot.database.profile_rows(interaction.guild_id, interaction.user.id)
    best = max(rows, key=lambda r: r['rating']) if rows else None
    await send_response(interaction, "You have no ratings yet." if not best else f"Your best current mode is **{mode_label(best['mode'])}** at **{best['rating']} Elo**.")


async def _career_kd(interaction):
    row = bot.database.connection.execute("SELECT SUM(kills) AS kills, SUM(deaths) AS deaths FROM match_player_stats WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()
    await send_response(interaction, f"**Your career K/D:** **{(row['kills'] or 0)/max(row['deaths'] or 0, 1):.2f}** ({row['kills'] or 0}-{row['deaths'] or 0})")


async def _career_winrate(interaction):
    row = bot.database.connection.execute("SELECT SUM(wins) AS wins, SUM(games) AS games FROM ratings WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()
    await send_response(interaction, f"**Your all-mode win rate:** **{(row['wins'] or 0)/max(row['games'] or 0, 1)*100:.1f}%** ({row['wins'] or 0}-{(row['games'] or 0)-(row['wins'] or 0)})")


async def _career_streak(interaction):
    row = bot.database.connection.execute("SELECT mode, current_streak, best_streak FROM ratings WHERE guild_id=? AND user_id=? ORDER BY current_streak DESC, best_streak DESC LIMIT 1", (interaction.guild_id, interaction.user.id)).fetchone()
    await send_response(interaction, "You have no streak data yet." if not row else f"**Your best streak** · {mode_label(row['mode'])}: current **{row['current_streak']}**, best **{row['best_streak']}**")


async def _career_peak(interaction):
    row = bot.database.connection.execute("SELECT mode, peak_rating FROM ratings WHERE guild_id=? AND user_id=? ORDER BY peak_rating DESC LIMIT 1", (interaction.guild_id, interaction.user.id)).fetchone()
    await send_response(interaction, "You have no peak rating yet." if not row else f"Your highest peak is **{row['peak_rating']} Elo** in **{mode_label(row['mode'])}**.")


async def _career_maps(interaction):
    rows = bot.database.connection.execute("SELECT m.map_name, COUNT(*) AS games FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE s.guild_id=? AND s.user_id=? GROUP BY m.map_name ORDER BY games DESC LIMIT 10", (interaction.guild_id, interaction.user.id)).fetchall()
    await send_response(interaction, "**Your map history**\n" + ("\n".join(f"{r['map_name']} — **{r['games']}** games" for r in rows) or "You have no map history yet."))


async def _career_recent(interaction):
    rows = bot.database.connection.execute("SELECT m.id, m.mode, m.winner, m.team_one, m.team_two FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE s.guild_id=? AND s.user_id=? ORDER BY m.id DESC LIMIT 10", (interaction.guild_id, interaction.user.id)).fetchall()
    lines = []
    for r in rows:
        own = str(interaction.user.id) in r['team_one'].split(',')
        won = (r['winner'] == 1 and own) or (r['winner'] == 2 and not own)
        lines.append(f"#{r['id']} · {mode_label(r['mode'])} · {'Win' if won else 'Loss'}")
    await send_response(interaction, "**Your recent results**\n" + ("\n".join(lines) or "No recent results."))


async def _career_opponents(interaction):
    rows = bot.database.opponent_records(interaction.guild_id, "control_3v3", interaction.user.id)
    await send_response(interaction, "**Your frequent opponents**\n" + ("\n".join(f"<@{uid}> — **{vals[0]}-{vals[1]}**" for uid, vals in rows[:10]) or "No opponent history yet."))


async def _career_modes(interaction):
    rows = bot.database.player_stats(interaction.guild_id, interaction.user.id)
    await send_response(interaction, "**Your mode records**\n" + ("\n".join(f"{mode_label(r['mode'])}: {r['rating']} Elo · {r['wins']}-{r['losses']}" for r in rows) or "No mode records yet."))


_CAREER_FEATURES = (
    ("summary", "Show your complete career summary", _career_summary), ("best_mode", "Show your best current mode", _career_best_mode),
    ("kd", "Show your career K/D", _career_kd), ("winrate", "Show your all-mode win rate", _career_winrate),
    ("streak", "Show your current and best streak", _career_streak), ("peak", "Show your highest peak Elo", _career_peak),
    ("maps", "Show your most-played maps", _career_maps), ("recent", "Show your recent results", _career_recent),
    ("opponents", "Show your frequent Control opponents", _career_opponents), ("modes", "Show your mode records", _career_modes),
    ("kills", "Show your total kills", _career_stat("kills", "kills")), ("deaths", "Show your total deaths", _career_stat("deaths", "deaths")),
    ("assists", "Show your total assists", _career_stat("assists", "assists")), ("damage", "Show your total damage", _career_stat("damage", "damage")),
    ("score", "Show your total score", _career_stat("score", "score")), ("captures", "Show your total captures", _career_stat("captures", "captures")),
    ("breaks", "Show your total hill breaks", _career_stat("breaks", "breaks")), ("avg_kills", "Show your average kills", _career_stat("kills", "kills", True)),
    ("avg_deaths", "Show your average deaths", _career_stat("deaths", "deaths", True)), ("avg_assists", "Show your average assists", _career_stat("assists", "assists", True)),
    ("avg_damage", "Show your average damage", _career_stat("damage", "damage", True)), ("avg_score", "Show your average score", _career_stat("score", "score", True)),
    ("avg_captures", "Show your average captures", _career_stat("captures", "captures", True)), ("avg_breaks", "Show your average breaks", _career_stat("breaks", "breaks", True)),
    ("provisional", "Show your provisional rating progress", _career_summary),
)
for _name, _description, _callback in _CAREER_FEATURES:
    _register_readonly_feature(career_group, _name, _description, _callback)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(original, app_commands.errors.MissingPermissions):
        message = "You do not have permission to use that command."
    elif isinstance(original, app_commands.errors.CommandOnCooldown):
        message = f"That command is on cooldown. Try again in {original.retry_after:.1f} seconds."
    elif isinstance(original, app_commands.errors.TransformerError):
        message = "One of the values was not valid. Please choose an option from Discord's suggestions."
    elif isinstance(original, app_commands.errors.CheckFailure):
        message = "You cannot use that command here or with your current permissions."
    elif isinstance(original, app_commands.errors.CommandSignatureMismatch):
        message = "Discord still has an older version of this command. Restart the bot and wait for the slash commands to sync."
    else:
        logging.getLogger("gears5-elo-bot").exception("Unhandled application command error", exc_info=original)
        message = "That command could not be completed. Please check the values and try again."
    try:
        await send_response(interaction, message, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")
    if not scheduled_reminders.is_running():
        scheduled_reminders.start()
    if not scheduled_announcements.is_running():
        scheduled_announcements.start()
    if not automatic_backup.is_running():
        automatic_backup.start()
    if not temporary_channel_cleanup.is_running():
        temporary_channel_cleanup.start()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    bot.run(TOKEN)
