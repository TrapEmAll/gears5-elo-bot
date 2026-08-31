from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from elo import MODES, calculate_match_changes, canonical_matchup, mode_label, parse_player_stats, parse_team, stat_names, team_size

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "gears5_elo.sqlite3"))
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
DEFAULT_RATING = 1000


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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
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
        return row["rating"] if row else DEFAULT_RATING

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

    def record_match(self, guild_id: int, mode: str, winner: int, team_one: list[int], team_two: list[int], stats: dict[int, dict[str, int]], created_by: int):
        rated_one = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_one]
        rated_two = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_two]
        changes = calculate_match_changes(mode, rated_one, rated_two, winner)
        season = self.active_season(guild_id)
        cursor = self.connection.execute(
            "INSERT INTO matches (guild_id, mode, winner, team_one, team_two, created_by, season_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by, season["id"] if season else None),
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
            self.connection.execute("DELETE FROM matches WHERE id=?", (match["id"],))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return match

    @staticmethod
    def _sum_team_stats(player_ids: list[int], stats: dict[int, dict[str, int]]) -> dict[str, int]:
        return {column: sum(stats[player_id].get(column, 0) for player_id in player_ids) for column in ("captures", "breaks", "kills", "deaths", "assists", "damage", "score")}

    def matchup_stats(self, guild_id: int, mode: str, team_one: list[int], team_two: list[int]):
        team_a, team_b, first_is_a = canonical_matchup(team_one, team_two)
        row = self.connection.execute(
            "SELECT * FROM team_matchups WHERE guild_id=? AND mode=? AND team_a=? AND team_b=?",
            (guild_id, mode, team_a, team_b),
        ).fetchone()
        return row, first_is_a

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

    def player_stats(self, guild_id: int, user_id: int):
        return self.connection.execute(
            "SELECT mode, rating, wins, losses, games FROM ratings WHERE guild_id=? AND user_id=? ORDER BY rating DESC",
            (guild_id, user_id),
        ).fetchall()

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


class PlayerStatsModal(discord.ui.Modal):
    def __init__(self, mode: str, winner: int, team_one: list[int], team_two: list[int], player_ids: list[int], stats: dict[int, dict[str, int]], index: int):
        self.mode = mode
        self.winner = winner
        self.team_one = team_one
        self.team_two = team_two
        self.player_ids = player_ids
        self.stats = stats
        self.index = index
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
            await interaction.response.send_modal(PlayerStatsModal(self.mode, self.winner, self.team_one, self.team_two, self.player_ids, self.stats, next_index))
            return

        try:
            changes = bot.database.record_match(interaction.guild_id, self.mode, self.winner, self.team_one, self.team_two, self.stats, interaction.user.id)
        except sqlite3.Error as error:
            await interaction.response.send_message(f"Could not record match: {error}", ephemeral=True)
            return
        change_text = " · ".join(f"<@{change.user_id}> {change.new_rating} ({change.delta:+d})" for change in changes)
        await interaction.response.send_message(f"**{mode_label(self.mode)} recorded** — Team {self.winner} wins\n{change_text}\nStats saved for {len(self.stats)} players.")


@bot.tree.command(name="modes", description="Show the Gears 5 modes tracked by this bot")
async def modes(interaction: discord.Interaction):
    lines = [f"• {mode_label(mode)} — {team_size(mode)}v{team_size(mode)}" for mode in MODES]
    await interaction.response.send_message("**Tracked modes**\n" + "\n".join(lines))


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


@bot.tree.command(name="match", description="Record a completed private Gears 5 match")
@app_commands.describe(mode="Game mode", winner="Which team won", team_one="Comma-separated mentions/IDs", team_two="Comma-separated mentions/IDs")
@app_commands.choices(mode=mode_choices)
@app_commands.choices(winner=[app_commands.Choice(name="Team 1", value="1"), app_commands.Choice(name="Team 2", value="2")])
async def match(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: app_commands.Choice[str], team_one: str, team_two: str):
    try:
        size = team_size(mode.value)
        first = parse_team(team_one, size)
        second = parse_team(team_two, size)
        if set(first) & set(second):
            raise ValueError("A player cannot be on both teams")
        player_ids = first + second
        await interaction.response.send_modal(PlayerStatsModal(mode.value, int(winner.value), first, second, player_ids, {}, 0))
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
