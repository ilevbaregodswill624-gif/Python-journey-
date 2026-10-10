import os
import re
import random
from datetime import datetime, timedelta
from flask import Flask, request, render_template, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.permanent_session_lifetime = timedelta(days=365)

db_url = os.environ.get("DATABASE_URL", "sqlite:///game.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)

LEVELS = {"easy": (10, 7), "medium": (20, 5), "hard": (50, 6)}
MAX_FAILS = 5
LOCK_MINUTES = 5


class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, unique=True)
    pin_hash = db.Column(db.String(255), nullable=False)
    fails = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)


class Score(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False)
    level = db.Column(db.String(10), nullable=False)
    attempts = db.Column(db.Integer, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())


class Counter(db.Model):
    name = db.Column(db.String(30), primary_key=True)
    value = db.Column(db.Integer, default=0, nullable=False)


class PlayerXP(db.Model):
    # Separate table so existing Player records do not need a schema migration.
    name = db.Column(db.String(20), primary_key=True)
    xp = db.Column(db.Integer, default=0, nullable=False)


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    level = db.Column(db.String(10), nullable=False)
    won = db.Column(db.Boolean, nullable=False)
    attempts = db.Column(db.Integer, nullable=False)
    secret = db.Column(db.Integer, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())


with app.app_context():
    db.create_all()


def bump(name):
    row = db.session.get(Counter, name)
    if row is None:
        row = Counter(name=name, value=0)
        db.session.add(row)
    row.value += 1
    db.session.commit()


BASE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Guessing Game</title>
  {{ analytics|safe }}
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: flex; align-items: flex-start;
      justify-content: center; font-family: "Segoe UI", Arial, sans-serif; color: white;
      background: linear-gradient(135deg, #1e1b4b, #4c1d95, #be185d);
      overflow-y: auto; padding: 20px 0; box-sizing: border-box; }
    .card { width: 95%; max-width: 500px; padding: 32px; text-align: center; margin: 20px 0;
      background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2);
      border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.3); }
    h1 { margin: 0 0 8px; }
    .sub { opacity: 0.85; margin: 0 0 16px; }
    .levels { display: flex; gap: 8px; margin-bottom: 20px; }
    .levels a { flex: 1; padding: 8px; border-radius: 10px; text-decoration: none;
      color: white; background: rgba(255,255,255,0.15); font-size: 14px; }
    .levels a.on { background: #facc15; color: #1e1b4b; font-weight: bold; }
    input { width: 100%; padding: 14px; margin-bottom: 12px; font-size: 22px;
      text-align: center; border: none; border-radius: 12px; outline: none; }
    button, .btn { display: block; width: 100%; padding: 14px; font-size: 18px;
      font-weight: bold; text-decoration: none; background: #facc15; color: #1e1b4b;
      border: none; border-radius: 12px; cursor: pointer; margin-top: 10px; }
    .msg { min-height: 28px; margin: 20px 0 12px; font-size: 20px; font-weight: bold; }
    .low { color: #93c5fd; } .high { color: #fca5a5; }
    .win { color: #86efac; } .lose { color: #fca5a5; }
    .dots span { display: inline-block; width: 14px; height: 14px; margin: 0 3px;
      border-radius: 50%; background: rgba(255,255,255,0.25); }
    .dots span.used { background: #facc15; }
    .small { margin-top: 16px; opacity: 0.9; }
    .small a, .sub a { color: #facc15; }
    .links { display: flex; justify-content: center; gap: 18px; margin-top: 16px; }
    .links a { color: #facc15; }
    table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }
    td, th { padding: 8px 4px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.15); }
    tr.me td { color: #facc15; font-weight: bold; }
  </style>
</head>
<body>
  <div class="card">{% block content %}{% endblock %}</div>
</body>
</html>
"""

GAME = """
{% extends "base.html" %}
{% block content %}
<h1>🎯 Guessing Game</h1>

{% if not player %}
  <p class="sub">Pick a name and a 4-digit PIN. New name? It's yours. Returning? Enter your PIN.</p>
  <form method="post" action="/name">
    <input name="name" maxlength="20" placeholder="Player name" required autofocus>
    <input name="pin" type="password" inputmode="numeric" pattern="[0-9]{4}"
           maxlength="4" placeholder="4-digit PIN" required>
    <button type="submit">Start playing</button>
  </form>
  <div class="msg {{ kind }}">{{ message }}</div>
  <div class="links"><a href="/leaderboard">Leaderboard</a></div>
{% else %}
  <p class="sub">Playing as <b>{{ player }}</b> · <a href="/name">log out</a></p>
  <p class="sub">⭐ Total XP: <b>{{ total_xp }}</b></p>
  <p class="sub">I'm thinking of a number between 1 and {{ hi }}.</p>
  <div class="levels">
    {% for name in levels %}
      <a href="/?level={{ name }}" class="{{ 'on' if name == level else '' }}">{{ name.title() }}</a>
    {% endfor %}
  </div>

  {% if over %}
    <a class="btn" href="/">Play again 🔁</a>
  {% else %}
    <form method="post">
      <input type="number" name="guess" min="1" max="{{ hi }}" placeholder="?" required autofocus>
      <button type="submit">Guess</button>
    </form>
  {% endif %}

  <div class="msg {{ kind }}">{{ message }}</div>
  <div class="dots">
    {% for i in range(max_tries) %}<span class="{{ 'used' if i < attempts else '' }}"></span>{% endfor %}
  </div>
  {% if best %}<div class="small">🏆 Your best on {{ level }}: {{ best }}</div>{% endif %}
  <div class="links">
    <a href="/leaderboard?level={{ level }}">Leaderboard</a>
    <a href="/history">My history</a>
  </div>
{% endif %}
{% endblock %}
"""

BOARD = """
{% extends "base.html" %}
{% block content %}
<h1>🏆 Leaderboard</h1>
<div class="levels">
  {% for name in levels %}
    <a href="/leaderboard?level={{ name }}" class="{{ 'on' if name == level else '' }}">{{ name.title() }}</a>
  {% endfor %}
</div>
{% if rows %}
<table>
  <tr><th>#</th><th>Name</th><th>XP</th><th>Best</th></tr>
  {% for r in rows %}
    <tr class="{{ 'me' if r.name == player else '' }}">
      <td>{{ loop.index }}</td>
      <td>{{ r.name }}</td>
      <td>⭐ {{ r.xp }}</td>
      <td>{{ r.best if r.best is not none else '-' }}</td>
    </tr>
  {% endfor %}
</table>
{% else %}
<p class="sub">No players yet. Be the first 👀</p>
{% endif %}
<a class="btn" href="/">Back to game</a>
{% endblock %}
"""

HIST = """
{% extends "base.html" %}
{% block content %}
<h1>📜 {{ player }}'s History</h1>
<table>
  <tr><th>Played</th><th>Wins</th><th>Win rate</th><th>Streak</th></tr>
  <tr><td>{{ played }}</td><td>{{ wins }}</td><td>{{ rate }}%</td><td>🔥 {{ streak }}</td></tr>
</table>
{% if games %}
<table>
  <tr><th>Date</th><th>Level</th><th>Result</th><th>Tries</th><th>Number</th></tr>
  {% for g in games %}
  <tr>
    <td>{{ g.created.strftime('%b %d') if g.created else '' }}</td>
    <td>{{ g.level.title() }}</td>
    <td>{{ '✅' if g.won else '❌' }}</td>
    <td>{{ g.attempts }}</td>
    <td>{{ g.secret }}</td>
  </tr>
  {% endfor %}
</table>
<p class="sub">Showing your last 20 games.</p>
{% else %}
<p class="sub">No games yet. Go play one 👀</p>
{% endif %}
<a class="btn" href="/">Back to game</a>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "game.html": GAME,
    "board.html": BOARD, "history.html": HIST,
})


@app.context_processor
def inject_analytics():
    return {"analytics": os.environ.get("ANALYTICS_SCRIPT", "")}


@app.route("/name", methods=["GET", "POST"])
def set_name():
    if request.method == "GET":
        session.pop("player", None)
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()[:20]
    pin = request.form.get("pin", "").strip()

    if not name or not re.fullmatch(r"\d{4}", pin):
        session["login_error"] = "Enter a name and a 4-digit PIN."
        return redirect(url_for("home"))

    player = Player.query.filter(func.lower(Player.name) == name.lower()).first()

    if player is None:
        player = Player(name=name, pin_hash=generate_password_hash(pin))
        db.session.add(player)
        db.session.commit()
    else:
        now = datetime.utcnow()
        if player.locked_until and player.locked_until > now:
            mins = int((player.locked_until - now).total_seconds() // 60) + 1
            session["login_error"] = f"Too many tries. Wait {mins} min."
            return redirect(url_for("home"))
        if not check_password_hash(player.pin_hash, pin):
            player.fails += 1
            if player.fails >= MAX_FAILS:
                player.fails = 0
                player.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            db.session.commit()
            session["login_error"] = "Wrong PIN for that name."
            return redirect(url_for("home"))
        player.fails = 0
        player.locked_until = None
        db.session.commit()

    session.permanent = True
    session["player"] = player.name
    return redirect(url_for("home"))


@app.route("/", methods=["GET", "POST"])
def home():
    session.permanent = True

    if not session.get("seen"):
        session["seen"] = True
        bump("visitors")

    player = session.get("player")
    login_error = session.pop("login_error", None)

    new_level = request.args.get("level")
    if request.method == "GET" and new_level in LEVELS:
        session["level"] = new_level
        session.pop("secret", None)
        session.pop("attempts", None)

    level = session.get("level", "medium")
    hi, max_tries = LEVELS[level]

    if "secret" not in session:
        session["secret"] = random.randint(1, hi)
        session["attempts"] = 0

    message, kind, over = "", "", False
    if login_error:
        message, kind = login_error, "high"

    if request.method == "POST" and player:
        try:
            guess = int(request.form["guess"])
        except (KeyError, ValueError):
            guess = None

        if guess is None or not 1 <= guess <= hi:
            message, kind = f"Pick a number from 1 to {hi}", "high"
        else:
            session["attempts"] += 1
            tries = session["attempts"]
            secret = session["secret"]

            if guess == secret:
                message = f"You got it in {tries} attempts! 🎉 Saved to the leaderboard."
                kind, over = "win", True
                db.session.add(Score(name=player, level=level, attempts=tries))
                db.session.add(Game(name=player, level=level, won=True,
                                    attempts=tries, secret=secret))
                xp_reward = max(5, 35 - (tries * 5))
                xp_row = db.session.get(PlayerXP, player)
                if xp_row is None:
                    xp_row = PlayerXP(name=player, xp=0)
                    db.session.add(xp_row)
                xp_row.xp += xp_reward
                db.session.commit()
                message += f" ⭐ +{xp_reward} XP!"
                bump("games_played")
                bump("wins")

            elif tries >= max_tries:
                message = f"Out of attempts. It was {secret} 💀"
                kind, over = "lose", True
                db.session.add(Game(name=player, level=level, won=False,
                                    attempts=tries, secret=secret))
                xp_row = db.session.get(PlayerXP, player)
                if xp_row is None:
                    xp_row = PlayerXP(name=player, xp=0)
                    db.session.add(xp_row)
                xp_row.xp += 5
                db.session.commit()
                message += " ⭐ +5 XP for playing!"
                bump("games_played")

            elif guess < secret:
                distance = secret - guess

                if distance <= 3:
                    message, kind = "Too low! 🔥 You're very hot!", "low"
                elif distance <= 7:
                    message, kind = "Too low! 🌡️ You're getting warm!", "low"
                else:
                    message, kind = "Too low 📉", "low"

            else:
                distance = guess - secret

                if distance <= 3:
                    message, kind = "Too high! 🔥 You're very hot!", "high"
                elif distance <= 7:
                    message, kind = "Too high! 🌡️ You're getting warm!", "high"
                else:
                    message, kind = "Too high 📈", "high"

    attempts = session["attempts"]
    if over:
        session.pop("secret")
        session.pop("attempts")

    best = None
    total_xp = 0
    if player:
        best = (db.session.query(func.min(Score.attempts))
                .filter_by(name=player, level=level).scalar())
        xp_row = db.session.get(PlayerXP, player)
        total_xp = xp_row.xp if xp_row else 0

    return render_template(
        "game.html", message=message, kind=kind, over=over, attempts=attempts,
        level=level, levels=list(LEVELS), hi=hi, max_tries=max_tries,
        player=player, best=best, total_xp=total_xp,
    )


@app.route("/leaderboard")
def leaderboard():
    level = request.args.get("level") or session.get("level", "medium")
    if level not in LEVELS:
        level = "medium"
    # Rank the top 10 players by total XP, then use their best score as a tie-breaker.
    xp_subquery = (db.session.query(
                       PlayerXP.name.label("name"),
                       PlayerXP.xp.label("xp"))
                   .subquery())

    best_subquery = (db.session.query(
                         Score.name.label("name"),
                         func.min(Score.attempts).label("best"))
                     .filter_by(level=level)
                     .group_by(Score.name)
                     .subquery())

    rows = (db.session.query(
                xp_subquery.c.name,
                xp_subquery.c.xp,
                best_subquery.c.best)
            .outerjoin(best_subquery, best_subquery.c.name == xp_subquery.c.name)
            .order_by(xp_subquery.c.xp.desc(),
                      best_subquery.c.best.asc(),
                      xp_subquery.c.name.asc())
            .all())

    return render_template("board.html", rows=rows, level=level,
                           levels=list(LEVELS), player=session.get("player"))


@app.route("/history")
def history():
    player = session.get("player")
    if not player:
        return redirect(url_for("home"))
    games = (Game.query.filter_by(name=player)
             .order_by(Game.created.desc(), Game.id.desc()).all())
    played = len(games)
    wins = sum(1 for g in games if g.won)
    streak = 0
    for g in games:
        if not g.won:
            break
        streak += 1
    rate = round(100 * wins / played) if played else 0
    return render_template("history.html", games=games[:20], played=played,
                           wins=wins, rate=rate, streak=streak, player=player)


@app.route("/stats")
def stats():
    key = os.environ.get("ADMIN_KEY")
    if not key or request.args.get("key") != key:
        return "Not found", 404
    counts = {c.name: c.value for c in Counter.query.all()}
    return (f"<h2>Stats</h2>Visitors: {counts.get('visitors', 0)}<br>"
            f"Games finished: {counts.get('games_played', 0)}<br>"
            f"Wins: {counts.get('wins', 0)}<br>"
            f"Registered players: {Player.query.count()}")


if __name__ == "__main__":
    app.run(debug=True)