import hmac
import os
import re
import random
import secrets
from datetime import datetime, timedelta, date
from flask import Flask, request, render_template, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

# Local dev only: run with FLASK_DEBUG=1. Anything else is treated as production.
IS_DEBUG = os.environ.get("FLASK_DEBUG") == "1"

app = Flask(__name__)

_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    if not IS_DEBUG:
        raise RuntimeError(
            "SECRET_KEY is not set. Set it to a long random string "
            "(or run with FLASK_DEBUG=1 for local development)."
        )
    _secret_key = "dev-only-change-me"
app.secret_key = _secret_key

app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies need HTTPS. Defaults on in production, off in debug;
    # override with COOKIE_SECURE=0 or 1 if needed.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0" if IS_DEBUG else "1") == "1",
)

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


class DailyReward(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    reward_date = db.Column(db.String(10), nullable=False)
    __table_args__ = (db.UniqueConstraint("name", "reward_date", name="uq_daily_reward"),)


class Challenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator = db.Column(db.String(20), nullable=False, index=True)
    opponent = db.Column(db.String(20), nullable=False, index=True)
    level = db.Column(db.String(10), nullable=False)
    creator_attempts = db.Column(db.Integer)
    opponent_attempts = db.Column(db.Integer)
    creator_won = db.Column(db.Boolean)
    opponent_won = db.Column(db.Boolean)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())


class DailyMission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    mission_date = db.Column(db.String(10), nullable=False)
    mission_key = db.Column(db.String(30), nullable=False)
    progress = db.Column(db.Integer, default=0, nullable=False)
    completed = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (db.UniqueConstraint("name", "mission_date", "mission_key", name="uq_daily_mission"),)


class ChallengeMatch(db.Model):
    # New table keeps existing player/challenge data compatible without altering old tables.
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False, unique=True)
    secret = db.Column(db.Integer, nullable=False)


class ChallengePlayerState(db.Model):
    # Per-player state lets each person play at their own pace without a speed advantage.
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    name = db.Column(db.String(20), nullable=False)
    ready = db.Column(db.Boolean, default=False, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    guesses = db.Column(db.Text, default="", nullable=False)
    solved = db.Column(db.Boolean, default=False, nullable=False)
    finished = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (db.UniqueConstraint("challenge_id", "name", name="uq_challenge_player_state"),)


class ChallengeMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False, index=True)
    sender = db.Column(db.String(20), nullable=False)
    body = db.Column(db.String(300), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    level = db.Column(db.String(10), nullable=False)
    won = db.Column(db.Boolean, nullable=False)
    attempts = db.Column(db.Integer, nullable=False)
    secret = db.Column(db.Integer, nullable=False)
    created = db.Column(db.DateTime, server_default=func.now())


class ActiveGame(db.Model):
    # In-progress solo game. Lives server-side so the secret never reaches the browser
    # and attempts cannot be reset by replaying an old cookie. One row per player.
    name = db.Column(db.String(20), primary_key=True)
    level = db.Column(db.String(10), nullable=False)
    secret = db.Column(db.Integer, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)


class DailyWinBonus(db.Model):
    # Tracks the once-per-day +20 XP win bonus. Kept apart from DailyReward, which
    # belongs to the /claim-daily login reward.
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    bonus_date = db.Column(db.String(10), nullable=False)
    __table_args__ = (db.UniqueConstraint("name", "bonus_date", name="uq_daily_win_bonus"),)


with app.app_context():
    db.create_all()


def bump(name):
    row = db.session.get(Counter, name)
    if row is None:
        row = Counter(name=name, value=0)
        db.session.add(row)
    row.value += 1
    db.session.commit()


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_field():
    return f'<input type="hidden" name="csrf_token" value="{csrf_token()}">'


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        expected = session.get("_csrf", "")
        sent = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent, expected):
            return "Your session expired or the form was invalid. Go back, refresh the page and try again.", 400


def get_active_game(name, level, lock=False):
    """Return the player's in-progress game for `level`, creating or resetting it as needed.

    Only flushes; the caller commits. With lock=True the row is locked (Postgres) so two
    simultaneous guesses from the same player cannot both read the same attempt count.
    """
    hi, _ = LEVELS[level]
    query = ActiveGame.query.filter_by(name=name)
    if lock:
        query = query.with_for_update()
    game = query.first()
    if game is None:
        game = ActiveGame(name=name, level=level, secret=random.randint(1, hi), attempts=0)
        db.session.add(game)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            game = ActiveGame.query.filter_by(name=name).with_for_update().first()
    elif game.level != level:
        game.level = level
        game.secret = random.randint(1, hi)
        game.attempts = 0
    return game


def claim_daily_win_bonus(name):
    """Return 20 the first time a player wins on a given day, else 0."""
    today = date.today().isoformat()
    if DailyWinBonus.query.filter_by(name=name, bonus_date=today).first():
        return 0
    db.session.add(DailyWinBonus(name=name, bonus_date=today))
    return 20


BASE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Guessing Game Plus</title>
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
    .nav { display:flex; flex-wrap:wrap; justify-content:center; gap:12px; margin:14px 0; }
    .nav a { color:#facc15; }
    .profile-box { padding:12px; margin:12px 0; border-radius:12px; background:rgba(0,0,0,.15); }
    body.theme-ocean { background:linear-gradient(135deg,#082f49,#0369a1,#0f766e); }
    body.theme-sunset { background:linear-gradient(135deg,#7c2d12,#c2410c,#9d174d); }
    body.theme-dark { background:linear-gradient(135deg,#09090b,#27272a,#18181b); }
    .muted { opacity:.8; font-size:14px; }
    a { transition: .18s ease; }
    .links a, .nav a { display:inline-block; padding:9px 13px; border-radius:10px; background:rgba(250,204,21,.10); text-decoration:none; }
    .links a:hover, .nav a:hover { background:#facc15; color:#1e1b4b; transform:translateY(-1px); }
    button, .btn { transition:transform .18s ease, filter .18s ease; }
    button:hover, .btn:hover { filter:brightness(1.06); transform:translateY(-1px); }
    select { width:100%; padding:13px; margin:8px 0; border:0; border-radius:10px; font-size:16px; }
    button.linkbtn { display:inline; width:auto; margin:0; padding:0; background:none; color:#facc15;
      font-size:inherit; font-weight:normal; text-decoration:underline; border:none; cursor:pointer; }
    button.linkbtn:hover { filter:none; transform:none; }
  </style>
</head>
<body class="theme-{{ theme|default('purple') }}">
  <div class="card">{% block content %}{% endblock %}</div>
<script>
  // Tiny optional browser beep when a win/loss message is displayed.
  const msg = document.querySelector('.msg');
  if (msg && (msg.classList.contains('win') || msg.classList.contains('lose'))) {
    try { const ctx = new (window.AudioContext || window.webkitAudioContext)(); const osc = ctx.createOscillator(); const gain = ctx.createGain(); osc.connect(gain); gain.connect(ctx.destination); osc.frequency.value = msg.classList.contains('win') ? 880 : 220; gain.gain.value = 0.035; osc.start(); osc.stop(ctx.currentTime + 0.12); } catch(e) {}
  }
</script>
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
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input name="name" maxlength="20" placeholder="Player name" required autofocus>
    <input name="pin" type="password" inputmode="numeric" pattern="[0-9]{4}"
           maxlength="4" placeholder="4-digit PIN" required>
    <button type="submit">Start playing</button>
  </form>
  <div class="msg {{ kind }}">{{ message }}</div>
  <div class="links"><a href="/leaderboard">Leaderboard</a></div>
  <p class="muted">Create an account to save XP, badges and progress.</p>
{% else %}
  <div class="sub">Playing as <b>{{ player }}</b> · <form method="post" action="/logout" style="display:inline"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button type="submit" class="linkbtn">log out</button></form></div>
  <p class="sub">⭐ Total XP: <b>{{ total_xp }}</b> · Level <b>{{ player_level }}</b></p>
  <p class="sub">🎁 Daily reward: {{ daily_status }}</p>
  <div class="nav"><a href="/profile">Profile</a><a href="/missions">Daily Missions</a><a href="/challenges">Challenges</a><a href="/progress">Progress</a><a href="/settings">Theme</a></div>
  <p class="muted">🏅 Badges: {{ badges|join(', ') if badges else 'Play games to earn badges' }}</p>
  <p class="muted">🎯 Daily challenge: win a game today for bonus XP.</p>
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
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
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
    return {"analytics": os.environ.get("ANALYTICS_SCRIPT", ""), "theme": session.get("theme", "purple"),
            "csrf_token": csrf_token}


def get_xp(name):
    row = db.session.get(PlayerXP, name)
    return row.xp if row else 0


def get_level(xp):
    return max(1, xp // 100 + 1)


def get_badges(name):
    games = Game.query.filter_by(name=name).all()
    xp = get_xp(name)
    badges = []
    if games: badges.append("🎮 First Game")
    if any(g.won for g in games): badges.append("🏆 First Win")
    if sum(1 for g in games if g.won) >= 10: badges.append("🔥 10 Wins")
    if xp >= 100: badges.append("⭐ 100 XP")
    if xp >= 500: badges.append("💎 500 XP")
    return badges


def award_xp(name, amount):
    row = db.session.get(PlayerXP, name)
    if row is None:
        row = PlayerXP(name=name, xp=0)
        db.session.add(row)
    row.xp += amount


MISSION_DEFINITIONS = {
    "win_two": {"title": "Win 2 games", "target": 2, "reward": 50, "description": "Win any two games today."},
    "hard_win": {"title": "Hard mode victory", "target": 1, "reward": 75, "description": "Win one game on Hard difficulty."},
    "quick_win": {"title": "Quick thinker", "target": 1, "reward": 40, "description": "Win a game in 3 attempts or fewer."},
}


def update_daily_missions(name, won, level, attempts):
    today = date.today().isoformat()
    for key, definition in MISSION_DEFINITIONS.items():
        mission = DailyMission.query.filter_by(name=name, mission_date=today, mission_key=key).first()
        if mission is None:
            mission = DailyMission(name=name, mission_date=today, mission_key=key, progress=0)
            db.session.add(mission)
            db.session.flush()
        if mission.completed:
            continue
        should_progress = (
            (key == "win_two" and won) or
            (key == "hard_win" and won and level == "hard") or
            (key == "quick_win" and won and attempts <= 3)
        )
        if should_progress:
            mission.progress = min(definition["target"], mission.progress + 1)
            if mission.progress >= definition["target"]:
                mission.completed = True
                award_xp(name, definition["reward"])


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("player", None)
    return redirect(url_for("home"))


@app.route("/name", methods=["GET", "POST"])
def set_name():
    if request.method == "GET":
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()[:20]
    if name and not re.fullmatch(r"[A-Za-z0-9_ -]{1,20}", name):
        session["login_error"] = "Use only letters, numbers, spaces, _ or -."
        return redirect(url_for("home"))
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
    # The secret number and attempt count live in the database, never in the cookie.
    # Drop anything an older version of this app left in the session.
    session.pop("secret", None)
    session.pop("attempts", None)

    if request.method == "GET" and new_level in LEVELS:
        session["level"] = new_level
        if player:
            ActiveGame.query.filter_by(name=player).delete()
            db.session.commit()

    level = session.get("level", "medium")
    hi, max_tries = LEVELS[level]

    game = get_active_game(player, level, lock=request.method == "POST") if player else None
    attempts = game.attempts if game else 0

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
            game.attempts += 1
            tries = attempts = game.attempts
            secret = game.secret

            if guess == secret:
                message = f"You got it in {tries} attempts! 🎉 Saved to the leaderboard."
                kind, over = "win", True
                db.session.add(Score(name=player, level=level, attempts=tries))
                db.session.add(Game(name=player, level=level, won=True,
                                    attempts=tries, secret=secret))
                xp_reward = max(5, 35 - (tries * 5))
                daily_bonus = claim_daily_win_bonus(player)
                award_xp(player, xp_reward + daily_bonus)
                update_daily_missions(player, True, level, tries)
                db.session.delete(game)
                db.session.commit()
                message += f" ⭐ +{xp_reward} XP!"
                if daily_bonus:
                    message += f" 🎯 +{daily_bonus} daily challenge bonus!"
                bump("games_played")
                bump("wins")

            elif tries >= max_tries:
                message = f"Out of attempts. It was {secret} 💀"
                kind, over = "lose", True
                db.session.add(Game(name=player, level=level, won=False,
                                    attempts=tries, secret=secret))
                award_xp(player, 5)
                update_daily_missions(player, False, level, tries)
                db.session.delete(game)
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

    if player:
        db.session.commit()  # saves the new attempt count after a wrong guess

    best = None
    total_xp = 0
    if player:
        best = (db.session.query(func.min(Score.attempts))
                .filter_by(name=player, level=level).scalar())
        xp_row = db.session.get(PlayerXP, player)
        total_xp = xp_row.xp if xp_row else 0

    daily_status = "Claimed today" if player and DailyReward.query.filter_by(name=player, reward_date=date.today().isoformat()).first() else "Available on your profile"
    xp_value = get_xp(player) if player else 0
    return render_template(
        "game.html", message=message, kind=kind, over=over, attempts=attempts,
        level=level, levels=list(LEVELS), hi=hi, max_tries=max_tries,
        player=player, best=best, total_xp=total_xp,
        player_level=get_level(xp_value), badges=get_badges(player) if player else [], daily_status=daily_status,
    )


@app.route("/leaderboard")
def leaderboard():
    level = request.args.get("level") or session.get("level", "medium")
    if level not in LEVELS:
        level = "medium"
    # Rank every player by total XP, then use best attempts as a tie-breaker.
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


@app.route("/missions")
def missions():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    today = date.today().isoformat()
    rows = []
    for key, definition in MISSION_DEFINITIONS.items():
        row = DailyMission.query.filter_by(name=name, mission_date=today, mission_key=key).first()
        progress = row.progress if row else 0
        completed = row.completed if row else False
        bar = min(100, int(progress * 100 / definition["target"]))
        status = "✅ Completed" if completed else f"{progress}/{definition['target']} progress"
        rows.append(f'''<section class="mission"><div class="row"><h3>{definition['title']}</h3><b>+{definition['reward']} XP</b></div><p>{definition['description']}</p><div class="track"><span style="width:{bar}%"></span></div><p class="status">{status}</p></section>''')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Daily Missions</title><style>*{{box-sizing:border-box}}body{{margin:0;padding:22px 14px;background:linear-gradient(135deg,#1e1b4b,#4c1d95,#be185d);color:#fff;font-family:Arial,sans-serif}}main{{max-width:680px;margin:auto}}.panel{{padding:24px;border-radius:18px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18)}}.mission{{padding:16px;margin:14px 0;border-radius:13px;background:rgba(0,0,0,.15)}}.row{{display:flex;justify-content:space-between;gap:12px;align-items:center}}h1,h3{{margin:0}}p{{line-height:1.5}}.track{{height:9px;background:rgba(255,255,255,.18);border-radius:9px;overflow:hidden}}.track span{{display:block;height:100%;background:#facc15}}.status{{color:#fde68a;font-size:14px}}a{{color:#fde047}}.back{{display:inline-block;margin-top:12px}}</style></head><body><main><section class="panel"><h1>🎯 Daily Missions</h1><p>Complete today's goals to earn bonus XP. Progress resets daily; your total XP stays.</p>{''.join(rows)}<a class="back" href="/">← Back to game</a></section></main></body></html>'''


@app.route("/profile")
def profile():
    name = session.get("player")
    if not name: return redirect(url_for("home"))
    games = Game.query.filter_by(name=name).all()
    wins = sum(1 for g in games if g.won)
    xp = get_xp(name)
    claimed = DailyReward.query.filter_by(name=name, reward_date=date.today().isoformat()).first() is not None
    return f"""<html><meta name='viewport' content='width=device-width, initial-scale=1'><body style='font-family:Arial;background:#1e1b4b;color:white;padding:24px'><h1>👤 {name}'s Profile</h1><p>⭐ XP: {xp}</p><p>Level: {get_level(xp)}</p><p>Games played: {len(games)}</p><p>Wins: {wins}</p><p>Badges: {', '.join(get_badges(name)) or 'None yet'}</p><p>Daily reward: {'Already claimed today' if claimed else 'Ready to claim'}</p><form method='post' action='/claim-daily'><input type='hidden' name='csrf_token' value='{csrf_token()}'><button style='padding:10px 14px;border:0;border-radius:10px;background:#facc15;color:#1e1b4b;font-weight:bold;cursor:pointer'>Claim daily login reward</button></form><p><a style='color:#facc15' href='/missions'>Daily missions</a> · <a style='color:#facc15' href='/challenges'>Challenges</a></p><p><a style='color:#facc15' href='/change-pin'>Change PIN</a></p><p><a style='color:#facc15' href='/'>Back to game</a></p></body></html>"""


@app.route("/claim-daily", methods=["POST"])
def claim_daily():
    name = session.get("player")
    if not name: return redirect(url_for("home"))
    today = date.today().isoformat()
    if DailyReward.query.filter_by(name=name, reward_date=today).first():
        return "Daily reward already claimed today. <a href='/profile'>Back</a>"
    db.session.add(DailyReward(name=name, reward_date=today))
    award_xp(name, 10)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # double-click / parallel request: the other one already paid out
        return "Daily reward already claimed today. <a href='/profile'>Back</a>"
    return "🎁 You claimed 10 XP! <a href='/profile'>Back to profile</a>"


@app.route("/change-pin", methods=["GET", "POST"])
def change_pin():
    name = session.get("player")
    if not name: return redirect(url_for("home"))
    if request.method == "POST":
        old_pin = request.form.get("old_pin", "")
        new_pin = request.form.get("new_pin", "")
        player = Player.query.filter_by(name=name).first()
        if not player or not check_password_hash(player.pin_hash, old_pin):
            return "Current PIN is incorrect. <a href='/change-pin'>Try again</a>"
        if not re.fullmatch(r"\d{4}", new_pin):
            return "New PIN must be exactly 4 digits. <a href='/change-pin'>Try again</a>"
        player.pin_hash = generate_password_hash(new_pin)
        db.session.commit()
        return "PIN updated successfully. <a href='/profile'>Back to profile</a>"
    return """<html><meta name='viewport' content='width=device-width, initial-scale=1'><body style='font-family:Arial;padding:24px'><h2>Change PIN</h2><form method='post'><input type='hidden' name='csrf_token' value='__CSRF__'><input name='old_pin' type='password' inputmode='numeric' placeholder='Current PIN' required><br><input name='new_pin' type='password' inputmode='numeric' pattern='[0-9]{4}' maxlength='4' placeholder='New 4-digit PIN' required><br><button>Change PIN</button></form></body></html>""".replace("__CSRF__", csrf_token())


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if not session.get("player"): return redirect(url_for("home"))
    if request.method == "POST":
        theme = request.form.get("theme", "purple")
        if theme in {"purple", "ocean", "sunset", "dark"}: session["theme"] = theme
        return redirect(url_for("settings"))
    return """<html><meta name='viewport' content='width=device-width, initial-scale=1'><body style='font-family:Arial;background:#1e1b4b;color:white;padding:24px'><h2>🎨 Choose a theme</h2><form method='post'><input type='hidden' name='csrf_token' value='__CSRF__'><select name='theme'><option value='purple'>Purple</option><option value='ocean'>Ocean</option><option value='sunset'>Sunset</option><option value='dark'>Dark</option></select><button>Save theme</button></form><p><a style='color:#facc15' href='/'>Back to game</a></p></body></html>""".replace("__CSRF__", csrf_token())


@app.route("/progress")
def progress():
    name = session.get("player")
    if not name: return redirect(url_for("home"))
    games = Game.query.filter_by(name=name).order_by(Game.created.asc(), Game.id.asc()).all()
    wins = sum(1 for g in games if g.won)
    bars = "".join(f"<div style='margin:8px 0'>Game {i}: {'🏆 Win' if g.won else '❌ Loss'} ({g.attempts} tries)</div>" for i, g in enumerate(games[-20:], start=max(1, len(games)-19)))
    return f"<html><meta name='viewport' content='width=device-width, initial-scale=1'><body style='font-family:Arial;background:#1e1b4b;color:white;padding:24px'><h1>📈 Progress</h1><p>Total games: {len(games)}</p><p>Wins: {wins}</p><p>Win rate: {round(wins*100/len(games)) if games else 0}%</p><h3>Last 20 games</h3>{bars or 'No games yet'}<p><a style='color:#facc15' href='/'>Back to game</a></p></body></html>"


@app.route("/challenges", methods=["GET", "POST"])
def challenges():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))

    message = ""
    message_kind = "info"
    if request.method == "POST":
        action = request.form.get("action", "send")
        if action in {"accept", "decline"}:
            try:
                challenge_id = int(request.form.get("challenge_id", ""))
            except ValueError:
                challenge_id = 0
            challenge = db.session.get(Challenge, challenge_id)
            if not challenge:
                message, message_kind = "Challenge not found.", "error"
            elif challenge.opponent != name:
                message, message_kind = "Only the invited player can respond to this challenge.", "error"
            elif challenge.status != "pending":
                message, message_kind = "This challenge has already been answered.", "error"
            elif action == "accept":
                challenge.status = "accepted"
                if not ChallengeMatch.query.filter_by(challenge_id=challenge.id).first():
                    hi, _ = LEVELS[challenge.level]
                    db.session.add(ChallengeMatch(challenge_id=challenge.id, secret=random.randint(1, hi)))
                db.session.commit()
                message, message_kind = f"You accepted {challenge.creator}'s challenge! Open the match below to play.", "success"
            else:
                challenge.status = "declined"
                db.session.commit()
                message, message_kind = "Challenge declined.", "success"
        else:
            opponent = request.form.get("opponent", "").strip()
            level = request.form.get("level", "medium")
            target = Player.query.filter(func.lower(Player.name) == opponent.lower()).first() if opponent else None
            if not target:
                message, message_kind = "That player account was not found.", "error"
            elif target.name == name:
                message, message_kind = "You cannot challenge yourself.", "error"
            elif level not in LEVELS:
                message, message_kind = "Choose a valid difficulty.", "error"
            elif Challenge.query.filter_by(creator=name, opponent=target.name, status="pending").first():
                message, message_kind = f"You already have a pending challenge for {target.name}.", "error"
            else:
                db.session.add(Challenge(creator=name, opponent=target.name, level=level, status="pending"))
                db.session.commit()
                message, message_kind = f"Challenge sent to {target.name}!", "success"

    items = (Challenge.query.filter((Challenge.creator == name) | (Challenge.opponent == name))
             .order_by(Challenge.id.desc()).limit(50).all())
    cards = []
    for c in items:
        incoming = c.opponent == name and c.creator != name
        direction = "Incoming challenge" if incoming else "Challenge you sent"
        actions = ""
        if incoming and c.status == "pending":
            actions = f'''<div class="actions">
              <form method="post">{csrf_field()}<input type="hidden" name="challenge_id" value="{c.id}"><input type="hidden" name="action" value="accept"><button type="submit">✓ Accept</button></form>
              <form method="post">{csrf_field()}<input type="hidden" name="challenge_id" value="{c.id}"><input type="hidden" name="action" value="decline"><button class="decline" type="submit">✕ Decline</button></form>
            </div>'''
        match_link = f'<p><a href="/challenge/{c.id}">▶ Open match</a></p>' if c.status == "accepted" else ""
        result_line = ""
        if c.status == "completed":
            if c.creator_won is True:
                result_line = f"<p>🏆 {c.creator} won!</p>"
            elif c.opponent_won is True:
                result_line = f"<p>🏆 {c.opponent} won!</p>"
            else:
                result_line = "<p>🤝 Match ended in a draw.</p>"
        cards.append(f'''<section class="challenge-card">
          <div class="card-head"><strong>{c.creator} vs {c.opponent}</strong><span class="status">{c.status}</span></div>
          <p>{direction} · {c.level.title()} difficulty</p>{actions}{match_link}{result_line}
        </section>''')

    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    message_html = f'<p class="notice {message_kind}">{safe_message}</p>' if message else ""
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Player Challenges</title><style>
*{{box-sizing:border-box}} body{{margin:0;padding:22px 14px;min-height:100vh;font-family:Arial,sans-serif;color:#fff;background:linear-gradient(135deg,#1e1b4b,#4c1d95,#be185d)}}
.wrap{{max-width:680px;margin:0 auto}} .panel{{background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.18);border-radius:20px;padding:24px;margin-bottom:16px;box-shadow:0 16px 38px rgba(0,0,0,.2)}}
h1{{margin:0 0 8px}} .muted{{opacity:.78;font-size:14px}} input,select{{width:100%;padding:13px;margin:8px 0;border:0;border-radius:10px;font-size:16px}} button{{display:inline-block;width:100%;padding:13px;border:0;border-radius:10px;background:#facc15;color:#1e1b4b;font-size:15px;font-weight:bold;text-align:center;cursor:pointer}}
.challenge-card{{padding:16px;margin:12px 0;border-radius:14px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14)}} .card-head{{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}} .challenge-card p{{margin:8px 0 4px}}
.status{{display:inline-block;padding:5px 10px;border-radius:999px;background:rgba(250,204,21,.18);color:#fde68a;font-size:12px;text-transform:capitalize}} .notice{{padding:12px;border-radius:10px;background:rgba(255,255,255,.1)}} .success{{color:#86efac}} .error{{color:#fda4af}}
.actions{{display:flex;gap:8px;margin-top:12px}} .actions form{{flex:1}} .actions button{{margin:0}} .actions .decline{{background:#fda4af;color:#4c0519}} a{{color:#fde047}} .back{{display:block;margin-top:14px;text-align:center}} @media(max-width:480px){{.panel{{padding:18px}}.actions{{flex-direction:column}}}}
</style></head><body><main class="wrap">
<section class="panel"><h1>🤝 Player Challenges</h1><p class="muted">Challenge another player or respond to requests sent to you.</p>{message_html}
<form method="post">{csrf_field()}<input type="hidden" name="action" value="send"><label for="opponent">Opponent username</label><input id="opponent" name="opponent" maxlength="20" placeholder="Enter exact player name" required>
<label for="level">Difficulty</label><select id="level" name="level"><option value="easy">Easy</option><option value="medium" selected>Medium</option><option value="hard">Hard</option></select><button type="submit">Send challenge →</button></form>
<a class="back" href="/">← Back to game</a></section>
<section class="panel"><h2>📬 Your challenges</h2>{''.join(cards) if cards else '<p class="muted">No challenges yet. Send your first one!</p>'}
<p class="muted">Both players get the same number and attempt limit. Each plays at their own pace; fewer attempts wins. Chat is available inside each match.</p></section>
</main></body></html>'''

@app.route("/challenge/<int:challenge_id>", methods=["GET", "POST"])
def challenge_match(challenge_id):
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    challenge = db.session.get(Challenge, challenge_id)
    if not challenge or name not in {challenge.creator, challenge.opponent}:
        return "Challenge not found.", 404
    if challenge.status != "accepted" and challenge.status != "completed":
        return redirect(url_for("challenges"))

    match = ChallengeMatch.query.filter_by(challenge_id=challenge.id).first()
    if match is None:
        hi, _ = LEVELS[challenge.level]
        match = ChallengeMatch(challenge_id=challenge.id, secret=random.randint(1, hi))
        db.session.add(match)

    # Create state rows lazily so existing accepted challenges and databases keep working.
    states = {}
    for player_name in (challenge.creator, challenge.opponent):
        state = ChallengePlayerState.query.filter_by(challenge_id=challenge.id, name=player_name).first()
        if state is None:
            state = ChallengePlayerState(challenge_id=challenge.id, name=player_name)
            # Preserve attempt counts from matches created by the previous version.
            old_attempts = challenge.creator_attempts if player_name == challenge.creator else challenge.opponent_attempts
            state.attempts = old_attempts or 0
            old_won = challenge.creator_won if player_name == challenge.creator else challenge.opponent_won
            state.solved = bool(old_won)
            state.finished = bool(old_won) or state.attempts >= LEVELS[challenge.level][1]
            db.session.add(state)
        states[player_name] = state
    db.session.commit()
    mine = states[name]
    other_name = challenge.opponent if name == challenge.creator else challenge.creator
    other = states[other_name]
    hi, max_tries = LEVELS[challenge.level]
    message, kind = "Get ready! Both players must mark themselves ready before the match begins.", "info"

    if request.method == "POST":
        action = request.form.get("action", "guess")
        if action == "ready":
            mine.ready = True
            db.session.commit()
            if states[challenge.creator].ready and states[challenge.opponent].ready:
                message, kind = "Both players are ready! The match is live. Play at your own pace; speed does not decide the winner.", "success"
            else:
                message, kind = "You're ready. Waiting for the other player to get ready.", "success"
        elif action == "chat":
            body = request.form.get("message", "").strip()
            if not body:
                message, kind = "Type a message first.", "error"
            elif len(body) > 300:
                message, kind = "Messages must be 300 characters or fewer.", "error"
            else:
                db.session.add(ChallengeMessage(challenge_id=challenge.id, sender=name, body=body))
                db.session.commit()
                message, kind = "Message sent.", "success"
        elif action == "guess":
            if not (states[challenge.creator].ready and states[challenge.opponent].ready):
                message, kind = "The match starts only after both players are ready.", "error"
            elif challenge.status == "completed":
                message, kind = "This match has already ended.", "error"
            elif mine.finished:
                message, kind = "You've finished your guesses. You can wait and chat while your opponent finishes.", "error"
            else:
                try:
                    guess = int(request.form.get("guess", ""))
                except (TypeError, ValueError):
                    guess = None
                if guess is None or not 1 <= guess <= hi:
                    message, kind = f"Enter a number from 1 to {hi}.", "error"
                else:
                    mine.attempts += 1
                    previous = [x for x in mine.guesses.split(",") if x]
                    previous.append(str(guess))
                    mine.guesses = ",".join(previous)
                    if guess == match.secret:
                        mine.solved = True
                        mine.finished = True
                        message, kind = "Correct! You've solved it. Your opponent can finish their own guesses; the winner is decided fairly after both finish. 🎯", "success"
                    elif mine.attempts >= max_tries:
                        mine.finished = True
                        message, kind = "No attempts left. You can chat while waiting for your opponent.", "error"
                    else:
                        message = "Too low! 🔥" if guess < match.secret else "Too high! 📈"
                        kind = "info"
                    if name == challenge.creator:
                        challenge.creator_attempts = mine.attempts
                        challenge.creator_won = mine.solved
                    else:
                        challenge.opponent_attempts = mine.attempts
                        challenge.opponent_won = mine.solved
                    # Finish only when BOTH players are done. Attempt count beats speed; equal scores draw.
                    if states[challenge.creator].finished and states[challenge.opponent].finished:
                        cstate, ostate = states[challenge.creator], states[challenge.opponent]
                        if cstate.solved and not ostate.solved:
                            winner = challenge.creator
                        elif ostate.solved and not cstate.solved:
                            winner = challenge.opponent
                        elif cstate.solved and ostate.solved and cstate.attempts < ostate.attempts:
                            winner = challenge.creator
                        elif cstate.solved and ostate.solved and ostate.attempts < cstate.attempts:
                            winner = challenge.opponent
                        else:
                            winner = None
                        challenge.status = "completed"
                        challenge.creator_won = winner == challenge.creator
                        challenge.opponent_won = winner == challenge.opponent
                        if winner:
                            award_xp(winner, 50)
                            db.session.add(Game(name=winner, level=challenge.level, won=True,
                                                attempts=states[winner].attempts, secret=match.secret))
                        message = (f"🏆 {winner} wins and earns 50 XP!" if winner else
                                   "🤝 It's a draw! Both players finished with the same result or neither solved it.")
                        kind = "success"
                    db.session.commit()
        else:
            message, kind = "Unknown action.", "error"

        # Refresh state after POST so the page shows the latest status.
        db.session.expire_all()
        challenge = db.session.get(Challenge, challenge_id)
        mine = ChallengePlayerState.query.filter_by(challenge_id=challenge.id, name=name).first()
        other = ChallengePlayerState.query.filter_by(challenge_id=challenge.id, name=other_name).first()
        states = {name: mine, other_name: other}

    both_ready = states[challenge.creator].ready and states[challenge.opponent].ready
    if challenge.status == "completed":
        if challenge.creator_won:
            result = f"🏆 {challenge.creator} won the match!"
        elif challenge.opponent_won:
            result = f"🏆 {challenge.opponent} won the match!"
        else:
            result = "🤝 The match ended in a draw."
    elif not both_ready:
        result = f"Your status: {'Ready ✅' if mine.ready else 'Not ready'} · {other_name}: {'Ready ✅' if other.ready else 'Waiting ⏳'}"
    else:
        result = f"Your attempts: {mine.attempts}/{max_tries} · Opponent attempts: {other.attempts}/{max_tries} · Your status: {'Finished' if mine.finished else 'Playing'}"

    if challenge.status != "completed" and not mine.ready:
        game_form = f'<form method="post">{csrf_field()}<input type="hidden" name="action" value="ready"><button type="submit">I\'m ready</button></form>'
    elif not both_ready and challenge.status != "completed":
        game_form = '<p>Waiting for the other player to press “I\'m ready”.</p>'
    elif challenge.status != "completed" and not mine.finished:
        game_form = f'''<form method="post">{csrf_field()}<input type="hidden" name="action" value="guess"><label for="guess">Your guess (1–{hi})</label><input id="guess" type="number" name="guess" min="1" max="{hi}" required><button type="submit">Submit guess</button></form>'''
    else:
        game_form = '<p>You have finished. Chat with your opponent while waiting for the match to conclude.</p>'

    chat_rows = ChallengeMessage.query.filter_by(challenge_id=challenge.id).order_by(ChallengeMessage.id.desc()).limit(30).all()
    chat_rows.reverse()
    chat_html = ''.join(f'<p class="chatline"><strong>{m.sender}</strong>: {m.body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</p>' for m in chat_rows) or '<p class="muted">No messages yet. Be civil-ish. 😅</p>'
    chat_form = f'''<form method="post">{csrf_field()}<input type="hidden" name="action" value="chat"><label for="message">Message</label><input id="message" name="message" maxlength="300" placeholder="Send a message…" required><button type="submit">Send message</button></form>'''
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Challenge Match</title><style>*{{box-sizing:border-box}}body{{margin:0;padding:24px 14px;background:linear-gradient(135deg,#1e1b4b,#4c1d95,#be185d);color:white;font-family:Arial,sans-serif}}main{{max-width:600px;margin:auto}}section{{padding:22px;margin-bottom:16px;border-radius:18px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18)}}input,button{{width:100%;padding:13px;margin:8px 0;border:0;border-radius:10px;font-size:16px}}button{{background:#facc15;color:#1e1b4b;font-weight:bold;cursor:pointer}}a{{color:#fde047}}.msg{{padding:12px;border-radius:10px;background:rgba(0,0,0,.18)}}.error{{color:#fda4af}}.success{{color:#86efac}}.muted{{opacity:.75}}.chatline{{padding:9px;background:rgba(0,0,0,.15);border-radius:8px;overflow-wrap:anywhere}}</style></head><body><main><section><h1>⚔️ {challenge.creator} vs {challenge.opponent}</h1><p>{challenge.level.title()} · Number from 1 to {hi}</p><p class="msg {kind}">{message}</p><p>{result}</p>{game_form}<p class="muted">Fair play: both players finish independently. The faster player does not automatically win. Fewer attempts wins; ties are draws. Your progress is saved.</p><p><a href="/challenges">← Back to challenges</a></p></section><section><h2>💬 Match chat</h2>{chat_html}{chat_form}</section></main></body></html>'''


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
    app.run(debug=IS_DEBUG)
