# Gears 5 Private Match Elo Bot

A small Discord slash-command bot for tracking private matches between friends. Ratings are separate for every mode:

- Control 1v1, 3v3, and 4v4
- 1v1 and 2v2 Gnashers

Ratings start at 1000. The bot uses a standard Elo K-factor of 32 and stores everything in a local SQLite database.

## Setup

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the `bot` and `applications.commands` scopes in the install URL. The bot only needs the `Send Messages` permission.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Install dependencies with `python -m pip install -r requirements.txt`.
5. Start it with `python bot.py`.

## Commands

- `/modes` — list supported modes.
- `/match` — record a match. Enter players as comma-separated mentions or IDs.
- `/leaderboard` — show the top ten players for a mode.
- `/rating` — show one player's ratings across modes.

Example for a 2v2 Gnashers match:

`/match mode:2v2 Gnashers winner:Team 1 team_one:@Alice, @Bob team_two:@Carol, @Dave`

The first time the bot is started, global slash-command sync can take a little while. To make commands appear immediately in one server during development, set up guild-scoped sync before deploying broadly.
