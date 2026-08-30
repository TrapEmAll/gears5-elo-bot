from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from elo import MODES, calculate_match_changes, mode_label, parse_team, team_size

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

    def record_match(self, guild_id: int, mode: str, winner: int, team_one: list[int], team_two: list[int], created_by: int):
        rated_one = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_one]
        rated_two = [(user_id, self.get_rating(guild_id, user_id, mode)) for user_id in team_two]
        changes = calculate_match_changes(mode, rated_one, rated_two, winner)
        self.connection.execute(
            "INSERT INTO matches (guild_id, mode, winner, team_one, team_two, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, mode, winner, ",".join(map(str, team_one)), ",".join(map(str, team_two)), created_by),
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
        changes = bot.database.record_match(interaction.guild_id, mode.value, int(winner.value), first, second, interaction.user.id)
    except (ValueError, sqlite3.Error) as error:
        await interaction.response.send_message(f"Could not record match: {error}", ephemeral=True)
        return
    change_text = " · ".join(f"<@{c.user_id}> {c.new_rating} ({c.delta:+d})" for c in changes)
    await interaction.response.send_message(f"**{mode_label(mode.value)} recorded** — Team {winner.value} wins\n{change_text}")


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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env before starting the bot.")
    bot.run(TOKEN)
