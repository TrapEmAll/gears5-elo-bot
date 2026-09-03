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
2. Enable the `bot` and `applications.commands` scopes in the install URL. The bot needs `Send Messages`; add `Manage Roles` if you want automatic Elo tier roles.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Install dependencies with `python -m pip install -r requirements.txt`.
5. Start it with `python bot.py`.

## Commands

- `/match record` — record a match and enter each player's stats one at a time; `/match confirm` confirms a pending match, and `/match force_confirm` lets a server manager record one without waiting for both teams.
- `/match confirm`, `/match cancel`, `/match vote`, `/match edit`, `/match attach`, `/match rematch`, `/match forfeit`, `/match remake` — review and manage results.
- `/stats leaderboard`, `/stats rating`, `/stats trend`, `/stats player`, `/stats history`, `/stats streaks`, `/stats predict`, `/stats awards`, `/stats elo_history`, `/stats confidence`, `/stats teamleaderboard`, `/stats teamstats`, `/stats close_games`, `/stats comebacks` — view rankings and analytics.
- `/player profile`, `/player profile_set`, `/player search`, `/player opponents`, `/player rivalry`, `/player recent_form`, `/player consistency`, `/player personal_bests`, `/player compare`, `/player myhistory` — view player records and profiles.
- `/team balance`, `/team random_teams`, `/team draft_start`, `/team draft_pick`, `/team draft_suggest`, `/team preset_save`, `/team presets`, `/team preset_delete`, `/team chemistry`, `/team teamhistory` — manage teams.
- `/queue join`, `/queue leave`, `/queue status`, `/queue availability`, `/queue available`, `/queue schedule`, `/queue lfg` — find players and coordinate games.
- `/season status`, `/season start`, `/season end`, `/season reset` — manage seasons.
- `/series start`, `/series update`, `/series status` — track BO3/BO5 series.
- `/tournament create`, `/tournament join`, `/tournament start`, `/tournament bracket` — run tournaments.
- `/lobby create`, `/lobby checkin`, `/lobby status`, `/lobby no_show`, `/lobby match_channels`, `/lobby match_channels_close` — coordinate match lobbies.
- `/maps veto_start`, `/maps veto_ban`, `/maps veto_pick`, `/maps mapstats`, `/maps mapplayer`, `/maps rotation_set`, `/maps next_map` — manage maps.
- `/challenge create`, `/challenge accept`, `/challenge decline` — manage challenges.
- `/admin settings`, `/admin setelo`, `/admin roles_setup`, `/admin roles_cleanup`, `/admin nickname_sync`, `/admin backup_now`, `/admin backup_restore`, `/admin integrity`, `/admin permission_set`, `/admin webhook_set`, `/admin dashboard_share`, `/admin announcement_channel`, `/admin announcement_schedule`, `/admin announcement_cancel`, `/admin maintenance`, `/admin note_add`, `/admin notes`, `/admin note_delete`, `/admin achievement_create`, `/admin captain_set`, `/admin audit` — administrator tools.
- `/server modes`, `/server health`, `/server help_menu` — bot and server information.
- `/insights overview`, `/insights top_damage`, `/insights top_kills`, `/insights top_score`, `/insights top_assists`, `/insights top_captures`, `/insights top_breaks`, `/insights kd`, `/insights winrate`, `/insights peak`, `/insights recent`, `/insights lastmatch`, `/insights maps`, `/insights teams`, `/insights pending` — additional performance and match analytics.
- `/ops summary`, `/ops players`, `/ops activity`, `/ops database`, `/ops backups`, `/ops seasons`, `/ops teams`, `/ops maps`, `/ops pending`, `/ops lobbies`, `/ops tournaments`, `/ops series`, `/ops challenges`, `/ops queue`, `/ops commands` — server operational summaries.
- `/reports ...` — 25 compact match, player, stat, and activity totals, including latest/oldest match and mode breakdown.
- `/tools ...` — 25 quick diagnostics for identity, latency, settings, queues, schedules, backups, SQLite, and database health.

The bot now exposes 17 top-level command groups instead of registering every feature globally. Discord will show the available subcommands after you type the group name.
- `start_dashboard.bat` — launch the dashboard on all LAN interfaces at port `5050`; from another device browse to `http://<the-PC's-LAN-IP>:5050`. Set `DASHBOARD_PORT` or `DASHBOARD_HOST` in `.env` to customize it. If Windows Firewall prompts, allow Python on Private networks.
- Match completion automatically posts an MVP, team score totals, and Elo changes. Leaderboards use embeds with a refresh button.

Example for a 2v2 Gnashers match:

`/match record mode:2v2 Gnashers winner:Team 1 team_one:@Alice, @Bob team_two:@Carol, @Dave map_name:Checkout`

Administrators can use `/setelo` to configure a rating floor and the number of provisional games in addition to the starting rating and K-factor. The dashboard refreshes automatically and supports an all-mode filter plus JSON leaderboard endpoints at `/api/leaderboard/<mode>`.

After submitting the match, the bot opens a small form for each player, labeled with that player's Discord display name and username. Enter only that player's stats in the form currently shown, then click **Enter next player's stats** to continue; you do not need to look up Discord user IDs. Every mode requires `damage`; 1v1 Gnashers requires `kills`, `deaths`, `damage`, and `score`; 2v2 Gnashers also includes `assists`:

```text
@Alice kills=15 deaths=8 damage=500 score=250
@Bob kills=11 deaths=10 damage=450 score=210
@Carol kills=8 deaths=13 damage=400 score=180
@Dave kills=10 deaths=13 damage=350 score=190
```

For 2v2 Gnashers, use for example: `kills=15 deaths=8 assists=4 damage=500 score=250`.

To check a recurring matchup, use `/stats teamstats`. For example: `/stats teamstats mode:2v2 Gnashers team_one:@Alice, @Bob team_two:@John, @Jim`. The teams are matched by their player combinations, regardless of which side was entered as Team 1.

Control requires `captures`, `breaks`, `kills`, `deaths`, `assists`, `damage`, and `score` on every line.

After updating, restart the bot so Discord can sync the grouped command tree. The first guild-scoped sync is immediate when `DISCORD_GUILD_ID` is set; global sync can take longer.
