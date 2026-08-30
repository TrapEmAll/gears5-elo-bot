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
5. Double-click `start_bot.bat` to run the bot.

The first run creates a private `.venv` folder and installs everything automatically. Leave the black bot window open while you want the bot online.

## Manual setup

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
