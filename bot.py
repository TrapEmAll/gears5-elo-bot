from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from elo import MODES, calculate_match_changes, mode_label, parse_player_stats, parse_team, stat_names, team_size

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
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (match_id, user_id),
                FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
            );
            """
        )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def get_rating(self, guild_id: int, user_id: int, mode: str) -> int:
        row = self.connection.execute(
            "SELECT rating FROM ratings WHERE guild_id=? AND user_id=? AND mode=?",
            (guild_id, user_id, mode),
        ).fetchone()
        return row["rating"] if row else DEFAULT_RATING

    def record_match(self, guild_id: int, mode: str, winner: int, team_one: list[int], team_two: list[int], stats: dict[int, dict[str, int]], created_by: int):
        rated_one = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_one]
        rated_two = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_two]
        changes = calculate_match_changes(mode, rated_one, rated_two, winner)
        cursor = self.connection.execute(
            "INSERT INTO matches (guild_id, mode, winner, team_one, team_two, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by),
        )
        match_id = cursor.lastrowid
        for user_id, values in stats.items():
            self.connection.execute(
                "INSERT INTO match_player_stats (match_id, guild_id, user_id, mode, captures, breaks, kills, deaths, assists, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (match_id, guild_id, user_id, mode, values.get("captures", 0), values.get("breaks", 0), values.get("kills", 0), values.get("deaths", 0), values.get("assists", 0), values.get("score", 0)),
            )
        for change in changes:
            did_win = (change.user_id in team_one and winner == 1) or (change.user_id in team_two and winner == 2)
            self.connection.execute(
                """
                INSERT INTO ratings (guild_id, user_id, mode, rating, wins, losses, games)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(guild_id, user_id, mode) DO UPDATE SET
                    rating=excluded.rating, wins=wins+excluded.wins,
                    losses=losses+excluded.losses, games=games+1
                """,
                (guild_id, change.user_id, mode, change.new_rating, int(did_win), int(not did_win)),
            )
        self.connection.commit()
        return changes

    def player_stat_summary(self, guild_id: int, user_id: int, mode: str):
        return self.connection.execute(
            """
            SELECT COUNT(*) AS matches, SUM(captures) AS captures, SUM(breaks) AS breaks,
                   SUM(kills) AS kills, SUM(deaths) AS deaths, SUM(assists) AS assists,
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

    def player_stats(self, guild_id: int, user_id: int):
        return self.connection.execute(
            "SELECT mode, rating, wins, losses, games FROM ratings WHERE guild_id=? AND user_id=? ORDER BY rating DESC",
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
            placeholder=("kills=15 deaths=8 assists=4 score=250" if mode == "gnashers_2v2" else "kills=15 deaths=8 score=250") if mode.startswith("gnashers_") else "captures=3 breaks=5 kills=15 deaths=8 assists=7 score=250",
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
    lines = [f"{index}. <@{row['user_id']}> — **{row['rating']}** ({row['wins']}-{row['losses']})" for index, row in enumerate(rows, 1)]
    await interaction.response.send_message(f"**{mode_label(mode.value)} leaderboard**\n" + "\n".join(lines))


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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    bot.run(TOKEN)
