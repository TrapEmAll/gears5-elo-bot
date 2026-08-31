# Gears 5 Private Match Elo Bot

A small Discord slash-command bot for tracking private matches between friends. Ratings are separate for every mode:

- Control 1v1, 3v3, and 4v4
- 1v1 and 2v2 Gnashers

Ratings start at 1000. The bot uses a standard Elo K-factor of 32 and stores everything in a local SQLite database.

## Setup on Windows

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/). During installation, check **Add python.exe to PATH**.
2. Download this repository with **Code → Download ZIP**, then extract it, or clone it with Git.
3. Double-click `setup_windows.ps1`. If Windows asks how to open it, right-click it and choose **Run with PowerShell**.
4. Open the new `.env` file in Notepad and replace the placeholder after `DISCORD_TOKEN=` with your bot token.
5. For instant slash-command registration, add `DISCORD_GUILD_ID=` followed by your server ID. Enable **Developer Mode** in Discord, right-click your server, and choose **Copy Server ID**.
6. Double-click `start_bot.bat` to run the bot.

To update later, double-click `update_bot.bat`. It stops the bot, downloads the latest GitHub version, updates dependencies, and starts the bot again. It uses Git when the project was cloned and automatically uses a ZIP download when the project was downloaded as a ZIP. Your `.env`, database, and virtual environment are preserved.

The first run creates a private `.venv` folder and installs everything automatically. Leave the black bot window open while you want the bot online.

This bot uses slash commands only, so **Message Content Intent** does not need to be enabled in the Discord Developer Portal.

If commands still do not appear, create a fresh invite from **OAuth2 → URL Generator** with both `bot` and `applications.commands` selected, then invite the bot again. The bot must also be online in the server where you are testing.

## Manual setup

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the `bot` and `applications.commands` scopes in the install URL. The bot only needs the `Send Messages` permission.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Install dependencies with `python -m pip install -r requirements.txt`.
5. Start it with `python bot.py`.

## Commands

- `/season` — show the active season.
- `/season_start` — admin-only: start a named season.
- `/season_end` — admin-only: end the active season.
- `/settings` — show Elo settings for a mode.
- `/setelo` — admin-only: set starting rating and K-factor for a mode.
- `/balance` — build balanced teams from a lobby’s player list.
- `/modes` — list supported modes.
- `/match` — record a match. Enter players as comma-separated mentions or IDs; the bot then asks for each player's stats one at a time.
- `/leaderboard` — show the top ten players for a mode with Elo, record, games, and win rate.
- `/rating` — show one player's ratings across modes.
- `/profile` — show a complete player profile with record and performance stats.
- `/achievements` — show earned player badges.
- Match completion — automatically posts an MVP, team score totals, and Elo changes.
- `/streaks` — show current and best win streak leaders for a mode.
- `/mapstats` — show match counts and team wins by map.
- `/history` — show recent recorded matches, optionally filtered by mode.
- `/stats` — show a player's totals and per-match averages for a mode.
- `/teamstats` — show the head-to-head record and combined totals for two exact teams.
- `/chemistry` — show an exact roster’s overall record and combined stats across opponents.
- `/undo` — admin-only: remove the latest match so it can be corrected and re-entered.

Example for a 2v2 Gnashers match:

`/match mode:2v2 Gnashers winner:Team 1 team_one:@Alice, @Bob team_two:@Carol, @Dave map_name:Checkout`

After submitting the match, the bot opens a small form for each player. Enter only that player's stats. Every mode requires `damage`; 1v1 Gnashers requires `kills`, `deaths`, `damage`, and `score`; 2v2 Gnashers also includes `assists`:

```text
@Alice kills=15 deaths=8 damage=500 score=250
@Bob kills=11 deaths=10 damage=450 score=210
@Carol kills=8 deaths=13 damage=400 score=180
@Dave kills=10 deaths=13 damage=350 score=190
```

For 2v2 Gnashers, use for example: `kills=15 deaths=8 assists=4 damage=500 score=250`.

To check a recurring matchup, use `/teamstats`. For example: `/teamstats mode:2v2 Gnashers team_one:@Alice, @Bob team_two:@John, @Jim`. The teams are matched by their player combinations, regardless of which side was entered as Team 1.

Control requires `captures`, `breaks`, `kills`, `deaths`, `assists`, `damage`, and `score` on every line.

The first time the bot is started, global slash-command sync can take a little while. To make commands appear immediately in one server during development, set up guild-scoped sync before deploying broadly.
