import os
import random
from flask import Flask, request, render_template, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import func

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

db_url = os.environ.get("DATABASE_URL", "sqlite:///game.db")
if db_url.startswith("postgres://"):
   db_url = db_url.replace("postgres://","postresql+psycopg://", 1)
elif db_url.startwith("postgresql://"):
    db_url = db_url.replace("postgresq1://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)

LEVELS = {"easy": (10, 7), "medium": (20, 5), "hard": (50, 6)}


class Score(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False)
    level = db.Column(db.String(10), nullable=False)
    attempts = db.Column(db.Integer, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())


class Counter(db.Model):
    name = db.Column(db.String(30), primary_key=True)
    value = db.Column(db.Integer, default=0, nullable=False)


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
    body { margin: 0; min-height: 100vh; display: flex; align-items: center;
      justify-content: center; font-family: "Segoe UI", Arial, sans-serif; color: white;
      background: linear-gradient(135deg, #1e1b4b, #4c1d95, #be185d); }
    .card { width: 90%; max-width: 380px; padding: 32px; text-align: center; margin: 20px 0;
      background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2);
      border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.3); }
    h1 { margin: 0 0 8px; }
    .sub { opacity: 0.8; margin: 0 0 16px; }
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
    .small a { color: #facc15; }
    table { width: 100%; border-collapse: collapse; margin: 12px 0; }
    td, th { padding: 8px 4px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.15); }
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
<p class="sub">I'm thinking of a number between 1 and {{ hi }}.</p>
<div class="levels">
  {% for name in levels %}
    <a href="/?level={{ name }}" class="{{ 'on' if name == level else '' }}">{{ name.title() }}</a>
  {% endfor %}
</div>

{% if over %}
  {% if kind == 'win' %}
    <form method="post" action="/score">
      <input name="name" maxlength="20" placeholder="Your name" required>
      <button type="submit">Save score 🏆</button>
    </form>
  {% endif %}
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
<div class="small"><a href="/leaderboard?level={{ level }}">View leaderboard</a></div>
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
  <tr><th>#</th><th>Name</th><th>Tries</th></tr>
  {% for r in rows %}<tr><td>{{ loop.index }}</td><td>{{ r.name }}</td><td>{{ r.attempts }}</td></tr>{% endfor %}
</table>
{% else %}
<p class="sub">No scores yet. Be the first 👀</p>
{% endif %}
<a class="btn" href="/">Back to game</a>
{% endblock %}
"""

app.jinja_loader = DictLoader({"base.html": BASE, "game.html": GAME, "board.html": BOARD})


@app.context_processor
def inject_analytics():
    return {"analytics": os.environ.get("ANALYTICS_SCRIPT", "")}


@app.route("/", methods=["GET", "POST"])
def home():
    if not session.get("seen"):
        session["seen"] = True
        bump("visitors")

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

    if request.method == "POST":
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
                message = f"You got it in {tries} attempts! 🎉"
                kind, over = "win", True
                session["last_win"] = {"level": level, "attempts": tries}
                bump("games_played")
                bump("wins")
                bests = session.get("bests", {})
                if level not in bests or tries < bests[level]:
                    bests[level] = tries
                    session["bests"] = bests
                    message += " New best!"
            elif tries >= max_tries:
                message = f"Out of attempts. It was {secret} 💀"
                kind, over = "lose", True
                bump("games_played")
            elif guess < secret:
                message, kind = "Too low 📉", "low"
            else:
                message, kind = "Too high 📈", "high"

    attempts = session["attempts"]
    if over:
        session.pop("secret")
        session.pop("attempts")

    return render_template(
        "game.html", message=message, kind=kind, over=over, attempts=attempts,
        level=level, levels=list(LEVELS), hi=hi, max_tries=max_tries,
        best=session.get("bests", {}).get(level),
    )


@app.route("/score", methods=["POST"])
def save_score():
    win = session.pop("last_win", None)
    if not win:
        return redirect(url_for("home"))
    name = request.form.get("name", "").strip()[:20] or "Anon"
    db.session.add(Score(name=name, level=win["level"], attempts=win["attempts"]))
    db.session.commit()
    return redirect(url_for("leaderboard", level=win["level"]))


@app.route("/leaderboard")
def leaderboard():
    level = request.args.get("level", "medium")
    if level not in LEVELS:
        level = "medium"
    rows = (Score.query.filter_by(level=level)
            .order_by(Score.attempts, Score.created).limit(10).all())
    return render_template("board.html", rows=rows, level=level, levels=list(LEVELS))


@app.route("/stats")
def stats():
    key = os.environ.get("ADMIN_KEY")
    if not key or request.args.get("key") != key:
        return "Not found", 404
    counts = {c.name: c.value for c in Counter.query.all()}
    return (f"<h2>Stats</h2>Visitors: {counts.get('visitors', 0)}<br>"
            f"Games finished: {counts.get('games_played', 0)}<br>"
            f"Wins: {counts.get('wins', 0)}<br>"
            f"Leaderboard entries: {Score.query.count()}")


if __name__ == "__main__":
    app.run(debug=True)