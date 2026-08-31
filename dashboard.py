from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, abort, render_template_string

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


PAGE = """<!doctype html><title>Gears 5 Elo</title><style>body{font-family:system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#141414;color:#eee}a{color:#ff8a8a}nav{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:1.5rem}nav a{background:#333;padding:.5rem .75rem;border-radius:6px;text-decoration:none}table{border-collapse:collapse;width:100%;margin:1rem 0 2rem}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #444}.card{background:#222;padding:1rem;border-radius:8px;margin-bottom:1rem}</style><h1>Gears 5 Elo Dashboard</h1><nav><a href="/">All modes</a>{% for mode, label in modes.items() %}<a href="/mode/{{ mode }}">{{ label }}</a>{% endfor %}</nav>{% block content %}{% endblock %}"""


@app.route("/")
def home():
    leaderboards = [(mode, label, query("SELECT user_id, rating, wins, losses, games FROM ratings WHERE mode=? ORDER BY rating DESC LIMIT 10", (mode,))) for mode, label in MODES.items()]
    matches = query("SELECT id, mode, winner, team_one, team_two, map_name FROM matches ORDER BY id DESC LIMIT 15")
    return render_template_string(PAGE + """{% for mode, label, rows in leaderboards %}<div class=card><h2><a href="/mode/{{ mode }}">{{ label }}</a></h2>{% if rows %}<table><tr><th>#</th><th>Player ID</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ loop.index }}</td><td><a href="/player/{{ row.user_id }}">{{ row.user_id }}</a></td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table>{% else %}<p>No matches recorded for this mode.</p>{% endif %}</div>{% endfor %}<div class=card><h2>Recent matches</h2><table><tr><th>ID</th><th>Mode</th><th>Winner</th><th>Map</th><th>Teams</th></tr>{% for row in matches %}<tr><td>{{ row.id }}</td><td>{{ modes.get(row.mode, row.mode) }}</td><td>Team {{ row.winner }}</td><td>{{ row.map_name }}</td><td>{{ row.team_one }} vs {{ row.team_two }}</td></tr>{% endfor %}</table></div>""", leaderboards=leaderboards, matches=matches, modes=MODES)


@app.route("/mode/<mode>")
def mode_page(mode: str):
    if mode not in MODES:
        abort(404)
    rows = query("SELECT user_id, rating, wins, losses, games FROM ratings WHERE mode=? ORDER BY rating DESC LIMIT 50", (mode,))
    return render_template_string(PAGE + """<div class=card><h2>{{ label }} leaderboard</h2>{% if rows %}<table><tr><th>#</th><th>Player ID</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ loop.index }}</td><td><a href="/player/{{ row.user_id }}">{{ row.user_id }}</a></td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table>{% else %}<p>No matches recorded for this mode.</p>{% endif %}</div>""", rows=rows, label=MODES[mode], modes=MODES)


@app.route("/player/<int:user_id>")
def player_page(user_id: int):
    rows = query("SELECT mode, rating, wins, losses, games FROM ratings WHERE user_id=? ORDER BY rating DESC", (user_id,))
    history = query("SELECT m.id, m.mode, m.map_name, s.kills, s.deaths, s.damage, s.score, s.rating_delta FROM matches m JOIN match_player_stats s ON s.match_id=m.id WHERE s.user_id=? ORDER BY m.id DESC LIMIT 25", (user_id,))
    return render_template_string(PAGE + """<div class=card><h2>Player {{ user_id }}</h2><table><tr><th>Mode</th><th>Elo</th><th>Record</th><th>Games</th></tr>{% for row in rows %}<tr><td>{{ modes.get(row.mode, row.mode) }}</td><td>{{ row.rating }}</td><td>{{ row.wins }}-{{ row.losses }}</td><td>{{ row.games }}</td></tr>{% endfor %}</table></div><div class=card><h2>Recent performance</h2><table><tr><th>Match</th><th>Mode</th><th>Map</th><th>K/D</th><th>Damage</th><th>Score</th><th>Elo</th></tr>{% for row in history %}<tr><td>#{{ row.id }}</td><td>{{ modes.get(row.mode, row.mode) }}</td><td>{{ row.map_name }}</td><td>{{ row.kills }}/{{ row.deaths }}</td><td>{{ row.damage }}</td><td>{{ row.score }}</td><td>{{ "%+d"|format(row.rating_delta) }}</td></tr>{% endfor %}</table></div>""", user_id=user_id, rows=rows, history=history, modes=MODES)


if __name__ == "__main__":
    app.run(host=os.getenv("DASHBOARD_HOST", "0.0.0.0"), port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
