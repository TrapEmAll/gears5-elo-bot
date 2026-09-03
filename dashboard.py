from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "gears5_elo.sqlite3"))
MODES = {"control_1v1": "Control 1v1", "control_3v3": "Control 3v3", "control_4v4": "Control 4v4", "gnashers_1v1": "1v1 Gnashers", "gnashers_2v2": "2v2 Gnashers"}
app = Flask(__name__)


def query(sql: str, params=()):
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def configured_guild_id() -> int | None:
    value = os.getenv("DASHBOARD_GUILD_ID")
    try:
        return int(value) if value else None
    except ValueError:
        return None


def requested_guild_id() -> int | None:
    value = request.args.get("guild_id")
    try:
        return int(value) if value else configured_guild_id()
    except (TypeError, ValueError):
        return configured_guild_id()


def guild_clause(guild_id: int | None, prefix: str = "") -> tuple[str, tuple]:
    return ((f"{prefix}guild_id=?", (guild_id,)) if guild_id is not None else ("1=1", ()))


def dashboard_summary(guild_id: int | None) -> dict:
    clause, params = guild_clause(guild_id)
    matches = query(f"SELECT COUNT(*) AS count FROM matches WHERE {clause}", params)[0]["count"]
    players = query(f"SELECT COUNT(DISTINCT user_id) AS count FROM ratings WHERE {clause}", params)[0]["count"]
    stats = query(f"SELECT COALESCE(SUM(kills),0) AS kills, COALESCE(SUM(deaths),0) AS deaths, COALESCE(SUM(damage),0) AS damage, COALESCE(SUM(score),0) AS score FROM match_player_stats WHERE {clause}", params)[0]
    return {"matches": matches, "players": players, "kills": stats["kills"], "deaths": stats["deaths"], "damage": stats["damage"], "score": stats["score"]}


LEADERBOARD_METRICS = {"rating": "rating", "wins": "wins", "games": "games", "kills": "kills", "damage": "damage", "score": "score"}


REFRESH_SECONDS = max(10, int(os.getenv("DASHBOARD_REFRESH_SECONDS", "30")))
PAGE = """<!doctype html><meta http-equiv="refresh" content="{{ refresh_seconds }}"><title>Gears 5 Elo</title><style>body{font-family:system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#141414;color:#eee}a{color:#ff8a8a}nav{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1.5rem}nav a{background:#333;padding:.5rem .75rem;border-radius:6px;text-decoration:none}table{border-collapse:collapse;width:100%;margin:1rem 0 2rem}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #444}.card{background:#222;padding:1rem;border-radius:8px;margin-bottom:1rem}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem}.metric{background:#333;padding:.8rem;border-radius:6px}.metric strong{display:block;font-size:1.5rem}input,select{background:#333;color:#eee;border:1px solid #555;padding:.45rem;border-radius:4px}</style><h1>Gears 5 Elo Dashboard</h1><nav><a href="/">All modes</a>{% for mode, label in modes.items() %}<a href="/mode/{{ mode }}">{{ label }}</a>{% endfor %}<a href="/search">Player search</a></nav>{% block content %}{% endblock %}"""


@app.route("/")
def home():
    guild_id = requested_guild_id()
    selected_mode = request.args.get("mode")
    selected_modes = {selected_mode: MODES[selected_mode]} if selected_mode in MODES else MODES
    clause, params = guild_clause(guild_id)
    leaderboards = [(mode, label, query(f"SELECT r.user_id, COALESCE(NULLIF(p.gamertag,''), CAST(r.user_id AS TEXT)) AS display_name, r.rating, r.wins, r.losses, r.games FROM ratings r LEFT JOIN player_profiles p ON p.guild_id=r.guild_id AND p.user_id=r.user_id WHERE {clause.replace('guild_id', 'r.guild_id')} AND r.mode=? ORDER BY r.rating DESC LIMIT 10", params + (mode,))) for mode, label in selected_modes.items()]
    matches = query(f"SELECT id, mode, winner, team_one, team_two, map_name FROM matches WHERE {clause} ORDER BY id DESC LIMIT 15", params)
    return render_template_string(PAGE + """<div class=card><div class=metrics>{% for label, value in [("Matches", summary.matches), ("Players", summary.players), ("Kills", summary.kills), ("Damage", summary.damage), ("Score", summary.score)] %}<div class=metric><small>{{ label }}</small><strong>{{ "{:,}".format(value) }}</strong></div>{% endfor %}</div></div><div class=card><form><label>Filter mode: <select name=mode onchange="this.form.submit()"><option value="">All modes</option>{% for mode, label in modes.items() %}<option value="{{ mode }}" {% if mode == selected_mode %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label></form></div>{% for mode, label, rows in leaderboards %}<div class=card><h2><a href="/mode/{{ mode }}">{{ label }}</a></h2>{% if rows %}<table><tr><th>#</th><th>Player</th><th>Discord ID</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ loop.index }}</td><td><a href="/player/{{ row.user_id }}">{{ row.display_name }}</a></td><td>{{ row.user_id }}</td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table>{% else %}<p>No matches recorded for this mode.</p>{% endif %}</div>{% endfor %}<div class=card><h2>Recent matches</h2><table><tr><th>ID</th><th>Mode</th><th>Winner</th><th>Map</th><th>Teams</th></tr>{% for row in matches %}<tr><td><a href="/match/{{ row.id }}">#{{ row.id }}</a></td><td>{{ modes.get(row.mode, row.mode) }}</td><td>Team {{ row.winner }}</td><td>{{ row.map_name }}</td><td>{{ row.team_one }} vs {{ row.team_two }}</td></tr>{% endfor %}</table></div>""", leaderboards=leaderboards, matches=matches, modes=MODES, selected_mode=selected_mode, summary=dashboard_summary(guild_id), refresh_seconds=REFRESH_SECONDS)


@app.route("/mode/<mode>")
def mode_page(mode: str):
    if mode not in MODES:
        abort(404)
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    rows = query(f"SELECT r.user_id, COALESCE(NULLIF(p.gamertag,''), CAST(r.user_id AS TEXT)) AS display_name, r.rating, r.wins, r.losses, r.games FROM ratings r LEFT JOIN player_profiles p ON p.guild_id=r.guild_id AND p.user_id=r.user_id WHERE {clause.replace('guild_id', 'r.guild_id')} AND r.mode=? ORDER BY r.rating DESC LIMIT 50", params + (mode,))
    totals = query(f"SELECT COALESCE(SUM(kills),0) kills, COALESCE(SUM(deaths),0) deaths, COALESCE(SUM(assists),0) assists, COALESCE(SUM(captures),0) captures, COALESCE(SUM(breaks),0) breaks, COALESCE(SUM(damage),0) damage, COALESCE(SUM(score),0) score FROM match_player_stats WHERE {clause} AND mode=?", params + (mode,))[0]
    return render_template_string(PAGE + """<div class=card><h2>{{ label }} leaderboard</h2><div class=metrics>{% for key in ["kills", "deaths", "assists", "captures", "breaks", "damage", "score"] %}<div class=metric><small>Total {{ key }}</small><strong>{{ "{:,}".format(totals[key]) }}</strong></div>{% endfor %}</div>{% if rows %}<table><tr><th>#</th><th>Player</th><th>Discord ID</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ loop.index }}</td><td><a href="/player/{{ row.user_id }}">{{ row.display_name }}</a></td><td>{{ row.user_id }}</td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table>{% else %}<p>No matches recorded for this mode.</p>{% endif %}</div>""", rows=rows, totals=totals, label=MODES[mode], modes=MODES, refresh_seconds=REFRESH_SECONDS)


@app.route("/api/leaderboard/<mode>")
def leaderboard_api(mode: str):
    if mode not in MODES:
        abort(404)
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    metric = request.args.get("metric", "rating")
    order = LEADERBOARD_METRICS.get(metric, "rating")
    if metric in {"kills", "damage", "score"}:
        rows = query(f"SELECT r.user_id, r.rating, r.wins, r.losses, r.games, COALESCE(SUM(s.{order}),0) AS {order} FROM ratings r LEFT JOIN match_player_stats s ON s.guild_id=r.guild_id AND s.user_id=r.user_id AND s.mode=r.mode WHERE {clause.replace('guild_id', 'r.guild_id')} AND r.mode=? GROUP BY r.user_id, r.rating, r.wins, r.losses, r.games ORDER BY {order} DESC LIMIT 50", params + (mode,))
    else:
        rows = query(f"SELECT user_id, rating, wins, losses, games FROM ratings WHERE {clause} AND mode=? ORDER BY {order} DESC LIMIT 50", params + (mode,))
    return jsonify([dict(row) for row in rows])


@app.route("/api/leaderboards")
def leaderboards_api():
    return jsonify({mode: leaderboard_api(mode).json for mode in MODES})


@app.route("/api/summary")
def summary_api():
    return jsonify(dashboard_summary(requested_guild_id()))


@app.route("/api/matches")
def matches_api():
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    mode = request.args.get("mode")
    try:
        limit = min(max(int(request.args.get("limit", "25")), 1), 100)
    except ValueError:
        limit = 25
    mode_clause = " AND mode=?" if mode in MODES else ""
    rows = query(f"SELECT id, mode, winner, team_one, team_two, map_name, created_at FROM matches WHERE {clause}{mode_clause} ORDER BY id DESC LIMIT ?", params + ((mode,) if mode in MODES else ()) + (limit,))
    return jsonify([dict(row) for row in rows])


@app.route("/api/modes")
def modes_api():
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    rows = query(f"SELECT mode, COUNT(*) AS matches FROM matches WHERE {clause} GROUP BY mode ORDER BY matches DESC", params)
    return jsonify([{**dict(row), "label": MODES.get(row["mode"], row["mode"])} for row in rows])


@app.route("/api/match/<int:match_id>")
def match_api(match_id: int):
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    match = query(f"SELECT id, mode, winner, team_one, team_two, map_name, created_at FROM matches WHERE {clause} AND id=?", params + (match_id,))
    if not match:
        abort(404)
    stats = query("SELECT user_id, kills, deaths, assists, captures, breaks, damage, score, rating_delta FROM match_player_stats WHERE match_id=? ORDER BY user_id", (match_id,))
    return jsonify({"match": dict(match[0]), "stats": [dict(row) for row in stats]})


@app.route("/api/health")
def health_api():
    try:
        query("SELECT 1")
        return jsonify({"status": "ok", "database": str(DATABASE_PATH), "refresh_seconds": REFRESH_SECONDS})
    except sqlite3.Error:
        return jsonify({"status": "error"}), 503


@app.route("/api/player/<int:user_id>")
def player_api(user_id: int):
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    ratings = query(f"SELECT mode, rating, wins, losses, games FROM ratings WHERE {clause} AND user_id=? ORDER BY rating DESC", params + (user_id,))
    totals = query(f"SELECT COALESCE(SUM(kills),0) kills, COALESCE(SUM(deaths),0) deaths, COALESCE(SUM(assists),0) assists, COALESCE(SUM(captures),0) captures, COALESCE(SUM(breaks),0) breaks, COALESCE(SUM(damage),0) damage, COALESCE(SUM(score),0) score FROM match_player_stats WHERE {clause} AND user_id=?", params + (user_id,))[0]
    profile = query(f"SELECT gamertag, aliases FROM player_profiles WHERE {clause} AND user_id=?", params + (user_id,))
    if not ratings and not profile:
        abort(404)
    return jsonify({"user_id": user_id, "profile": dict(profile[0]) if profile else {}, "ratings": [dict(row) for row in ratings], "totals": dict(totals)})


@app.route("/api/players")
def players_api():
    term = request.args.get("q", "").strip()
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    rows = query(f"SELECT user_id, gamertag, aliases FROM player_profiles WHERE {clause} AND (gamertag LIKE ? OR aliases LIKE ?) ORDER BY gamertag LIMIT 50", params + (f"%{term}%", f"%{term}%")) if term else []
    return jsonify([dict(row) for row in rows])


@app.route("/search")
def search_players():
    term = request.args.get("q", "").strip()
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    rows = query(f"SELECT user_id, gamertag, aliases FROM player_profiles WHERE {clause} AND (gamertag LIKE ? OR aliases LIKE ?) ORDER BY gamertag LIMIT 50", params + (f"%{term}%", f"%{term}%")) if term else []
    return render_template_string(PAGE + """<div class=card><h2>Player search</h2><form><input name=q value="{{ term }}" placeholder="Gamertag or alias"><button>Search</button></form></div>{% if term %}<div class=card><table><tr><th>Gamertag</th><th>Aliases</th></tr>{% for row in rows %}<tr><td><a href="/player/{{ row.user_id }}">{{ row.gamertag or row.user_id }}</a></td><td>{{ row.aliases }}</td></tr>{% else %}<tr><td colspan=2>No players found.</td></tr>{% endfor %}</table></div>{% endif %}""", rows=rows, term=term, modes=MODES, refresh_seconds=REFRESH_SECONDS)


@app.route("/match/<int:match_id>")
def match_page(match_id: int):
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id, "m.")
    match = query(f"SELECT m.id, m.mode, m.winner, m.team_one, m.team_two, m.map_name, m.created_at FROM matches m WHERE {clause} AND m.id=?", params + (match_id,))
    if not match:
        abort(404)
    stats = query("SELECT user_id, kills, deaths, assists, captures, breaks, damage, score, rating_delta FROM match_player_stats WHERE match_id=? ORDER BY user_id", (match_id,))
    return render_template_string(PAGE + """<div class=card><h2>Match #{{ match.id }} — {{ modes.get(match.mode, match.mode) }}</h2><p>Team {{ match.winner }} won · {{ match.map_name }} · {{ match.created_at[:16] }}</p><p>{{ match.team_one }} vs {{ match.team_two }}</p></div><div class=card><h2>Player stats</h2><table><tr><th>Player ID</th><th>K/D</th><th>Assists</th><th>Captures</th><th>Breaks</th><th>Damage</th><th>Score</th><th>Elo</th></tr>{% for row in stats %}<tr><td><a href="/player/{{ row.user_id }}">{{ row.user_id }}</a></td><td>{{ row.kills }}/{{ row.deaths }}</td><td>{{ row.assists }}</td><td>{{ row.captures }}</td><td>{{ row.breaks }}</td><td>{{ row.damage }}</td><td>{{ row.score }}</td><td>{{ "%+d"|format(row.rating_delta) }}</td></tr>{% endfor %}</table></div>""", match=match[0], stats=stats, modes=MODES, refresh_seconds=REFRESH_SECONDS)


@app.route("/share/<token>")
def shared_page(token: str):
    share = query("SELECT guild_id FROM dashboard_shares WHERE token=?", (token,))
    if not share:
        abort(404)
    guild_id = share[0]["guild_id"]
    leaderboards = [(mode, label, query("SELECT user_id, rating, wins, losses, games FROM ratings WHERE guild_id=? AND mode=? ORDER BY rating DESC LIMIT 10", (guild_id, mode))) for mode, label in MODES.items()]
    return render_template_string(PAGE + """<p>Public read-only view</p>{% for mode, label, rows in leaderboards %}<div class=card><h2>{{ label }}</h2>{% if rows %}<table><tr><th>#</th><th>Player ID</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ loop.index }}</td><td>{{ row.user_id }}</td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table>{% else %}<p>No matches recorded.</p>{% endif %}</div>{% endfor %}""", leaderboards=leaderboards, modes=MODES, refresh_seconds=REFRESH_SECONDS)


@app.route("/player/<int:user_id>")
def player_page(user_id: int):
    guild_id = requested_guild_id()
    clause, params = guild_clause(guild_id)
    rows = query(f"SELECT mode, rating, wins, losses, games FROM ratings WHERE {clause} AND user_id=? ORDER BY rating DESC", params + (user_id,))
    history = query(f"SELECT m.id, m.mode, m.map_name, s.kills, s.deaths, s.assists, s.captures, s.breaks, s.damage, s.score, s.rating_delta FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE {clause.replace('guild_id', 'm.guild_id')} AND s.user_id=? ORDER BY m.id DESC LIMIT 25", params + (user_id,))
    profile = query(f"SELECT gamertag FROM player_profiles WHERE {clause} AND user_id=?", params + (user_id,))
    totals = query(f"SELECT COALESCE(SUM(kills),0) kills, COALESCE(SUM(deaths),0) deaths, COALESCE(SUM(assists),0) assists, COALESCE(SUM(captures),0) captures, COALESCE(SUM(breaks),0) breaks, COALESCE(SUM(damage),0) damage, COALESCE(SUM(score),0) score FROM match_player_stats WHERE {clause} AND user_id=?", params + (user_id,))[0]
    display_name = profile[0]["gamertag"] if profile and profile[0]["gamertag"] else str(user_id)
    return render_template_string(PAGE + """<div class=card><h2>Player {{ display_name }}</h2><div class=metrics>{% for key in ["kills", "deaths", "assists", "captures", "breaks", "damage", "score"] %}<div class=metric><small>Total {{ key }}</small><strong>{{ "{:,}".format(totals[key]) }}</strong></div>{% endfor %}</div><table><tr><th>Mode</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ modes.get(row.mode, row.mode) }}</td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table></div><div class=card><h2>Recent performance</h2><table><tr><th>Match</th><th>Mode</th><th>Map</th><th>K/D</th><th>Assists</th><th>Captures</th><th>Breaks</th><th>Damage</th><th>Score</th><th>Elo</th></tr>{% for row in history %}<tr><td>#{{ row.id }}</td><td>{{ modes.get(row.mode, row.mode) }}</td><td>{{ row.map_name }}</td><td>{{ row.kills }}/{{ row.deaths }}</td><td>{{ row.assists }}</td><td>{{ row.captures }}</td><td>{{ row.breaks }}</td><td>{{ row.damage }}</td><td>{{ row.score }}</td><td>{{ "%+d"|format(row.rating_delta) }}</td></tr>{% endfor %}</table></div>""", display_name=display_name, totals=totals, rows=rows, history=history, modes=MODES, refresh_seconds=REFRESH_SECONDS)


if __name__ == "__main__":
    app.run(host=os.getenv("DASHBOARD_HOST", "0.0.0.0"), port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
