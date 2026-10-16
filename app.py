import os
import re
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from flask import Flask, request, render_template, session, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.permanent_session_lifetime = timedelta(days=365)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

db_url = os.environ.get("DATABASE_URL", "sqlite:///game.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)

# ---------------------------------------------------------------- config
LEVELS = {"easy": (10, 7), "medium": (20, 5), "hard": (50, 6), "impossible": (100, 6)}
UNLOCK_WINS = 5                 # wins needed before Impossible mode opens
IMPOSSIBLE_START_CLUES = 2      # clues shown before the first guess (fewer = harder)
MAX_FAILS = 5
LOCK_MINUTES = 5

CHALLENGE_SECONDS = {"easy": 90, "medium": 120, "hard": 180, "impossible": 240}  # match clock per level
INVITE_HOURS = 24        # pending invites expire after this long
READY_MINUTES = 60       # accepted matches expire if nobody starts in this window
ONLINE_SECONDS = 25      # seen within this window = online
WIN_XP, LOSS_XP, DRAW_XP = 50, 10, 20

THEMES = {"purple": "Purple", "ocean": "Ocean", "sunset": "Sunset",
          "dark": "Dark", "forest": "Forest"}


# ---------------------------------------------------------------- models
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
    name = db.Column(db.String(20), primary_key=True)
    xp = db.Column(db.Integer, default=0, nullable=False)


class DailyReward(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    reward_date = db.Column(db.String(10), nullable=False)
    __table_args__ = (db.UniqueConstraint("name", "reward_date", name="uq_daily_reward"),)


class DailyWinBonus(db.Model):
    # Separate from DailyReward so the "first win of the day" bonus is tracked on its own.
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False, index=True)
    bonus_date = db.Column(db.String(10), nullable=False)
    __table_args__ = (db.UniqueConstraint("name", "bonus_date", name="uq_daily_win_bonus"),)


class Challenge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator = db.Column(db.String(20), nullable=False, index=True)
    opponent = db.Column(db.String(20), nullable=False, index=True)
    level = db.Column(db.String(10), nullable=False)
    creator_attempts = db.Column(db.Integer)
    opponent_attempts = db.Column(db.Integer)
    creator_won = db.Column(db.Boolean)
    opponent_won = db.Column(db.Boolean)
    # pending, accepted, declined, completed, expired
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
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False, unique=True)
    secret = db.Column(db.Integer, nullable=False)


class ChallengePlayerState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    name = db.Column(db.String(20), nullable=False)
    ready = db.Column(db.Boolean, default=False, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    guesses = db.Column(db.Text, default="", nullable=False)
    solved = db.Column(db.Boolean, default=False, nullable=False)
    finished = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (db.UniqueConstraint("challenge_id", "name", name="uq_challenge_player_state"),)


class ChallengeClock(db.Model):
    # The match clock. started_at stays NULL until both players are ready.
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), primary_key=True)
    accepted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    limit_seconds = db.Column(db.Integer, nullable=False)


class ChallengeReward(db.Model):
    # Unique per (match, player): a match can never pay the same player twice.
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    name = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint("challenge_id", "name", name="uq_challenge_reward"),)


class ChallengeMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False, index=True)
    sender = db.Column(db.String(20), nullable=False)
    body = db.Column(db.String(300), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PlayerPresence(db.Model):
    name = db.Column(db.String(20), primary_key=True)
    last_seen = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    activity = db.Column(db.String(20), nullable=False, default="online")


class PlayerTheme(db.Model):
    name = db.Column(db.String(20), primary_key=True)
    theme = db.Column(db.String(20), nullable=False, default="purple")


class ChallengeNotice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(20), nullable=False, index=True)
    challenge_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)
    text = db.Column(db.String(200), nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    seen = db.Column(db.Boolean, nullable=False, default=False)


class SoloState(db.Model):
    # The solo game's secret lives on the server, never in the cookie.
    sid = db.Column(db.String(32), primary_key=True)
    level = db.Column(db.String(10), nullable=False)
    secret = db.Column(db.Integer, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)


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


# ---------------------------------------------------------------- helpers
def utcnow():
    return datetime.utcnow()


def naive(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt):
    dt = naive(dt)
    return dt.isoformat(timespec="seconds") + "Z" if dt else ""


def today_str():
    return utcnow().date().isoformat()


def bump(name):
    row = db.session.get(Counter, name)
    if row is None:
        row = Counter(name=name, value=0)
        db.session.add(row)
    row.value += 1
    db.session.commit()


def claim_once(model, **keys):
    """Insert a unique row and commit. False if it already existed (race-safe)."""
    db.session.add(model(**keys))
    try:
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False


def get_xp(name):
    if not name:
        return 0
    return db.session.query(PlayerXP.xp).filter_by(name=name).scalar() or 0


def get_level(xp):
    return max(1, xp // 100 + 1)


def award_xp(name, amount):
    # Atomic increment so two requests can never overwrite each other's XP.
    n = (PlayerXP.query.filter_by(name=name)
         .update({PlayerXP.xp: PlayerXP.xp + amount}, synchronize_session=False))
    if n == 0:
        db.session.add(PlayerXP(name=name, xp=amount))


def get_badges(name):
    games = Game.query.filter_by(name=name).all()
    xp = get_xp(name)
    duels = Challenge.query.filter(Challenge.status == "completed",
                                   (Challenge.creator == name) | (Challenge.opponent == name)).all()
    duel_wins = sum(1 for c in duels if (c.creator == name and c.creator_won) or
                    (c.opponent == name and c.opponent_won))
    badges = []
    if games: badges.append("🎮 First Game")
    if any(g.won for g in games): badges.append("🏆 First Win")
    if sum(1 for g in games if g.won) >= 10: badges.append("🔥 10 Wins")
    if xp >= 100: badges.append("⭐ 100 XP")
    if xp >= 500: badges.append("💎 500 XP")
    if any(g.won and g.level == "impossible" for g in games): badges.append("💀 Impossible Winner")
    if duels: badges.append("⚔️ First Duel")
    if duel_wins: badges.append("👑 Duel Winner")
    return badges


def hint_for(guess, secret, level=None):
    if guess == secret:
        return "Correct! 🎯", "win"
    d = abs(guess - secret)
    way = "low" if guess < secret else "high"
    if level == "impossible":      # no hot/warm help in Impossible mode
        return f"Too {way} " + ("📉" if way == "low" else "📈"), way
    if d <= 3:
        return f"Too {way}! 🔥 Very hot!", way
    if d <= 7:
        return f"Too {way}! 🌡️ Getting warm!", way
    return f"Too {way} " + ("📉" if way == "low" else "📈"), way


# ---- Impossible mode
def win_count(name):
    if not name:
        return 0
    return Game.query.filter_by(name=name, won=True).count()


def impossible_unlocked(name):
    return win_count(name) >= UNLOCK_WINS


def impossible_clues(secret, seed):
    """Ordered list of TRUE clues about the secret. Same secret + seed = same clues."""
    rng = random.Random(f"{seed}:{secret}")
    lo = rng.randint(max(1, secret - 59), min(secret, 41))
    pool = (3, 4, 5, 6, 7, 9, 11)
    divisors = [k for k in pool if secret % k == 0]
    if divisors:
        div_text = f"➗ It divides evenly by {rng.choice(divisors)}."
    else:
        div_text = f"➗ It does NOT divide evenly by {rng.choice(pool)}."
    m, c = rng.randint(2, 5), rng.randint(1, 20)
    root = round(secret ** 0.5)
    return [
        "🔢 It's an even number." if secret % 2 == 0 else "🔢 It's an odd number.",
        f"📏 It's somewhere between {lo} and {lo + 59}.",
        div_text,
        f"🧮 Its digits add up to {sum(int(d) for d in str(secret))}.",
        "🟦 It IS a perfect square." if root * root == secret else "🟦 It is NOT a perfect square.",
        f"🧠 Multiply it by {m}, then add {c}. You get {secret * m + c}.",
        f"🔟 Its tens digit is {secret // 10 % 10}.",
        f"🔚 Its last digit is {secret % 10}.",
    ]


def clues_shown(clues, wrong_guesses):
    return clues[:min(len(clues), IMPOSSIBLE_START_CLUES + wrong_guesses)]


# ---- deleted accounts
def scrub_name(text, name):
    """Replace a player's name inside text, without touching longer names that contain it."""
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", "a deleted player", text)


def pname(name):
    return "a deleted player" if name and str(name).startswith("[deleted-") else name


def erase_player(name):
    """Delete a player and their data. Finished duels stay in the opponent's history with the name anonymized.
    Does not commit: the caller commits so the whole delete is one transaction."""
    tag = f"[deleted-{secrets.token_hex(3)}]"
    chs = Challenge.query.filter((Challenge.creator == name) | (Challenge.opponent == name)).all()
    ids = [c.id for c in chs]
    for c in chs:
        other = c.opponent if c.creator == name else c.creator
        if c.status in ("pending", "accepted"):
            c.status = "expired"
            if not other.startswith("[deleted-"):
                notify_player(other, c.id, "expired",
                              "⌛ A challenge was cancelled because the other player deleted their account.")
        if c.creator == name:
            c.creator = tag
        else:
            c.opponent = tag
    db.session.flush()
    if ids:
        for model in (ChallengePlayerState, ChallengeReward):
            (model.query.filter(model.challenge_id.in_(ids), model.name == name)
             .update({"name": tag}, synchronize_session=False))
        others = ChallengeNotice.query.filter(ChallengeNotice.challenge_id.in_(ids),
                                              ChallengeNotice.recipient != name).all()
        for n in others:
            n.text = scrub_name(n.text, name)
    ChallengeMessage.query.filter_by(sender=name).delete(synchronize_session=False)
    ChallengeNotice.query.filter_by(recipient=name).delete(synchronize_session=False)
    for model in (Score, DailyReward, DailyWinBonus, DailyMission, Game):
        model.query.filter_by(name=name).delete(synchronize_session=False)
    for model in (PlayerXP, PlayerPresence, PlayerTheme):
        model.query.filter_by(name=name).delete(synchronize_session=False)
    Player.query.filter_by(name=name).delete(synchronize_session=False)


# ---- presence & notifications
def touch_presence(name, activity="online"):
    if not name:
        return
    row = db.session.get(PlayerPresence, name)
    if row is None:
        db.session.add(PlayerPresence(name=name, last_seen=utcnow(), activity=activity))
        return
    row.last_seen = utcnow()
    row.activity = activity


def presence_of(name):
    row = db.session.get(PlayerPresence, name)
    if not row:
        return {"state": "offline", "last_seen": ""}
    online = utcnow() - naive(row.last_seen) <= timedelta(seconds=ONLINE_SECONDS)
    state = ("in-game" if row.activity == "in-game" else "online") if online else "offline"
    return {"state": state, "last_seen": iso(row.last_seen)}


def notify_player(recipient, challenge_id, kind, text):
    db.session.add(ChallengeNotice(recipient=recipient, challenge_id=challenge_id,
                                   kind=kind, text=text[:200]))


# ---- solo game state (server side)
def solo_get(level):
    sid = session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        session["sid"] = sid
    hi, _ = LEVELS[level]
    st = db.session.get(SoloState, sid)
    if st is None:
        st = SoloState(sid=sid, level=level, secret=random.randint(1, hi), attempts=0)
        db.session.add(st)
        db.session.commit()
    elif st.level != level:
        st.level, st.secret, st.attempts = level, random.randint(1, hi), 0
        db.session.commit()
    return st


def solo_reset(level):
    st = solo_get(level)
    st.secret, st.attempts = random.randint(1, LEVELS[level][0]), 0
    db.session.commit()


# ---- missions
MISSION_DEFINITIONS = {
    "win_two": {"title": "Win 2 games", "target": 2, "reward": 50, "description": "Win any two games today."},
    "hard_win": {"title": "Hard mode victory", "target": 1, "reward": 75, "description": "Win one game on Hard or Impossible difficulty."},
    "quick_win": {"title": "Quick thinker", "target": 1, "reward": 40, "description": "Win a game in 3 attempts or fewer."},
}


def update_daily_missions(name, won, level, attempts):
    today = today_str()
    for key, definition in MISSION_DEFINITIONS.items():
        mission = DailyMission.query.filter_by(name=name, mission_date=today, mission_key=key).first()
        if mission is None:
            mission = DailyMission(name=name, mission_date=today, mission_key=key, progress=0)
            db.session.add(mission)
            db.session.flush()
        if mission.completed:
            continue
        should = ((key == "win_two" and won) or
                  (key == "hard_win" and won and level in ("hard", "impossible")) or
                  (key == "quick_win" and won and attempts <= 3))
        if should:
            mission.progress = min(definition["target"], mission.progress + 1)
            if mission.progress >= definition["target"]:
                mission.completed = True
                award_xp(name, definition["reward"])


# ---------------------------------------------------------------- duel engine
def ensure_match(ch):
    """Make sure the secret, both player states and the clock exist. Returns (match, states, clock)."""
    for attempt in range(2):
        try:
            hi, _ = LEVELS[ch.level]
            match = ChallengeMatch.query.filter_by(challenge_id=ch.id).first()
            if match is None:
                match = ChallengeMatch(challenge_id=ch.id, secret=random.randint(1, hi))
                db.session.add(match)
            states = {}
            for n in (ch.creator, ch.opponent):
                s = ChallengePlayerState.query.filter_by(challenge_id=ch.id, name=n).first()
                if s is None:
                    s = ChallengePlayerState(challenge_id=ch.id, name=n)
                    old_att = ch.creator_attempts if n == ch.creator else ch.opponent_attempts
                    old_won = ch.creator_won if n == ch.creator else ch.opponent_won
                    s.attempts = old_att or 0          # keep progress from older versions
                    s.solved = bool(old_won)
                    s.finished = bool(old_won) or s.attempts >= LEVELS[ch.level][1]
                    db.session.add(s)
                states[n] = s
            clock = db.session.get(ChallengeClock, ch.id)
            if clock is None:
                clock = ChallengeClock(challenge_id=ch.id, accepted_at=utcnow(),
                                       limit_seconds=CHALLENGE_SECONDS[ch.level])
                if all(s.ready for s in states.values()):
                    clock.started_at = utcnow()
                db.session.add(clock)
            db.session.commit()
            return match, states, clock
        except IntegrityError:
            db.session.rollback()
    raise RuntimeError("Could not set up match")


def expire_challenge(ch, text):
    n = (Challenge.query.filter(Challenge.id == ch.id, Challenge.status.in_(("pending", "accepted")))
         .update({"status": "expired"}, synchronize_session=False))
    if n:
        for who in (ch.creator, ch.opponent):
            notify_player(who, ch.id, "expired", text)
    db.session.commit()


def finalize_if_done(ch, states, match):
    """Settle the match once both players are finished. Only one request can win the status flip."""
    c, o = states[ch.creator], states[ch.opponent]
    if not (c.finished and o.finished):
        return False
    claimed = (Challenge.query.filter_by(id=ch.id, status="accepted")
               .update({"status": "completed"}, synchronize_session=False))
    if not claimed:
        db.session.commit()
        return False

    if c.solved and not o.solved:
        winner = ch.creator
    elif o.solved and not c.solved:
        winner = ch.opponent
    elif c.solved and o.solved and c.attempts != o.attempts:
        winner = ch.creator if c.attempts < o.attempts else ch.opponent
    else:
        winner = None

    Challenge.query.filter_by(id=ch.id).update({
        "creator_won": winner == ch.creator, "opponent_won": winner == ch.opponent,
        "creator_attempts": c.attempts, "opponent_attempts": o.attempts,
    }, synchronize_session=False)

    for s, other in ((c, ch.opponent), (o, ch.creator)):
        is_win = winner == s.name
        amount = WIN_XP if is_win else (DRAW_XP if winner is None else LOSS_XP)
        db.session.add(ChallengeReward(challenge_id=ch.id, name=s.name, amount=amount))
        award_xp(s.name, amount)
        db.session.add(Game(name=s.name, level=ch.level, won=is_win, attempts=s.attempts, secret=match.secret))
        if is_win:
            update_daily_missions(s.name, True, ch.level, s.attempts)
            text = f"🏆 You beat {other} in a {ch.level.title()} duel! +{amount} XP"
        elif winner is None:
            text = f"🤝 Your duel with {other} ended in a draw. +{amount} XP"
        else:
            text = f"💀 {winner} won your {ch.level.title()} duel. +{amount} XP for playing"
        notify_player(s.name, ch.id, "completed", text)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False
    return True


def settle(ch):
    """Expire stale invites, enforce the match clock, finish matches. Safe to call often."""
    now = utcnow()
    if ch.status == "pending":
        created = naive(ch.created) or now
        if now - created > timedelta(hours=INVITE_HOURS):
            expire_challenge(ch, f"⌛ Challenge between {ch.creator} and {ch.opponent} expired.")
        return
    if ch.status != "accepted":
        return
    match, states, clock = ensure_match(ch)
    if clock.started_at is None:
        if now - naive(clock.accepted_at) > timedelta(minutes=READY_MINUTES):
            expire_challenge(ch, f"⌛ Match between {ch.creator} and {ch.opponent} never started and expired.")
        return
    deadline = naive(clock.started_at) + timedelta(seconds=clock.limit_seconds)
    if now >= deadline:
        changed = False
        for s in states.values():
            if not s.finished:
                s.finished = True        # unsolved + tries left = timed out
                changed = True
        if changed:
            db.session.commit()
    finalize_if_done(ch, states, match)


# ---------------------------------------------------------------- templates
BASE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Guessing Game Plus{% endblock %}</title>
  {{ analytics|safe }}
  <style>
    *{box-sizing:border-box}
    [hidden]{display:none!important}
    :root,body.theme-purple{--bg:linear-gradient(135deg,#1e1b4b,#4c1d95,#be185d);--accent:#facc15;--accent-ink:#1e1b4b}
    body.theme-ocean{--bg:linear-gradient(135deg,#082f49,#0369a1,#0f766e);--accent:#fde047;--accent-ink:#082f49}
    body.theme-sunset{--bg:linear-gradient(135deg,#7c2d12,#c2410c,#9d174d);--accent:#fde68a;--accent-ink:#431407}
    body.theme-dark{--bg:linear-gradient(135deg,#09090b,#27272a,#18181b);--accent:#a78bfa;--accent-ink:#0b0b10}
    body.theme-forest{--bg:linear-gradient(135deg,#052e16,#166534,#3f6212);--accent:#bef264;--accent-ink:#052e16}
    body{--panel:rgba(255,255,255,.10);--line:rgba(255,255,255,.20);--soft:rgba(0,0,0,.18);
      --good:#86efac;--bad:#fca5a5;--info:#93c5fd;
      margin:0;min-height:100vh;color:#fff;background:var(--bg) fixed;
      font-family:"Segoe UI",Arial,sans-serif}
    .shell{width:95%;max-width:560px;margin:0 auto;padding:14px 0 40px}
    .topbar{display:flex;justify-content:space-between;align-items:center;padding:6px 4px}
    .brand{color:#fff;text-decoration:none;font-weight:bold;font-size:18px}
    .bell{position:relative;color:#fff;text-decoration:none;font-size:22px;padding:4px 8px}
    .badge{position:absolute;top:-4px;right:-2px;min-width:18px;height:18px;border-radius:9px;padding:0 5px;
      background:#ef4444;color:#fff;font-size:11px;font-weight:bold;line-height:18px;text-align:center}
    .nav{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
    .nav a{color:var(--accent);text-decoration:none;padding:8px 12px;border-radius:10px;
      background:rgba(255,255,255,.08);font-size:14px;transition:.18s ease}
    .nav a:hover,.nav a.on{background:var(--accent);color:var(--accent-ink)}
    .card{padding:26px 22px;text-align:center;margin:10px 0;background:var(--panel);
      border:1px solid var(--line);border-radius:20px;box-shadow:0 20px 40px rgba(0,0,0,.3)}
    .card.left{text-align:left}
    h1{margin:0 0 8px} h2{margin:18px 0 8px}
    .sub{opacity:.88;margin:0 0 14px}
    .muted{opacity:.78;font-size:14px}
    a{color:var(--accent)}
    input,select{width:100%;padding:13px;margin:8px 0;font-size:18px;border:none;border-radius:12px;outline:none;text-align:center}
    select{font-size:16px;text-align:left}
    label{display:block;text-align:left;font-size:14px;opacity:.85;margin-top:8px}
    button,.btn{display:block;width:100%;padding:13px;font-size:17px;font-weight:bold;text-align:center;
      text-decoration:none;background:var(--accent);color:var(--accent-ink);border:none;border-radius:12px;
      cursor:pointer;margin-top:10px;transition:transform .18s ease,filter .18s ease}
    button:hover,.btn:hover{filter:brightness(1.07);transform:translateY(-1px)}
    button.alt,.btn.alt{background:rgba(255,255,255,.16);color:#fff}
    button.danger{background:#fda4af;color:#4c0519}
    .levels{display:flex;gap:8px;margin-bottom:18px}
    .levels a{flex:1;padding:8px;border-radius:10px;text-decoration:none;color:#fff;background:rgba(255,255,255,.15);font-size:14px}
    .levels a.on{background:var(--accent);color:var(--accent-ink);font-weight:bold}
    .msg{min-height:26px;margin:16px 0 10px;font-size:19px;font-weight:bold}
    .low,.info{color:var(--info)} .high,.error{color:var(--bad)}
    .win,.success{color:var(--good)} .lose{color:var(--bad)}
    .dots{margin:8px 0}
    .dots span{display:inline-block;width:14px;height:14px;margin:0 3px;border-radius:50%;background:rgba(255,255,255,.25)}
    .dots span.used{background:var(--accent)}
    .small{margin-top:14px;opacity:.9}
    .links{display:flex;justify-content:center;gap:12px;margin-top:14px;flex-wrap:wrap}
    .links a{display:inline-block;padding:9px 13px;border-radius:10px;background:rgba(255,255,255,.08);text-decoration:none}
    .links a:hover{background:var(--accent);color:var(--accent-ink)}
    table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}
    td,th{padding:8px 4px;text-align:left;border-bottom:1px solid var(--line)}
    tr.me td{color:var(--accent);font-weight:bold}
    .box{padding:14px;margin:12px 0;border-radius:14px;background:var(--soft);border:1px solid var(--line);text-align:left}
    .row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
    .track{height:9px;background:rgba(255,255,255,.18);border-radius:9px;overflow:hidden;margin:8px 0}
    .track span{display:block;height:100%;background:var(--accent)}
    .pill{display:inline-block;padding:4px 10px;border-radius:999px;background:rgba(255,255,255,.16);font-size:12px;text-transform:capitalize}
    .dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#9ca3af;margin-right:6px}
    .dot.online{background:#22c55e} .dot.in-game{background:#f59e0b}
    .chips{display:flex;flex-wrap:wrap;gap:8px}
    .chip{width:auto;display:inline-block;margin:0;padding:8px 12px;font-size:14px;border-radius:999px}
    .actions{display:flex;gap:8px;margin-top:10px} .actions form{flex:1} .actions button{margin:0}
    .notice{padding:12px;border-radius:10px;background:rgba(255,255,255,.12);margin:10px 0;text-align:left}
    .clock{font-size:44px;font-weight:bold;font-variant-numeric:tabular-nums;margin:6px 0}
    .clock.danger{color:var(--bad)}
    .chatbox{max-height:240px;overflow-y:auto;text-align:left}
    .chatline{padding:8px 10px;margin:6px 0;background:var(--soft);border-radius:8px;overflow-wrap:anywhere;font-size:14px}
    .chatline.mine{border-left:3px solid var(--accent)}
    .hist div{display:flex;justify-content:space-between;padding:6px 2px;border-bottom:1px solid var(--line);font-size:15px}
    #toasts{position:fixed;left:12px;right:12px;bottom:14px;display:flex;flex-direction:column;gap:8px;z-index:50;pointer-events:none;align-items:center}
    .toast{pointer-events:auto;display:block;max-width:520px;width:100%;padding:13px 15px;border-radius:12px;
      background:#111827;color:#fff;text-decoration:none;box-shadow:0 10px 30px rgba(0,0,0,.4);
      border-left:4px solid var(--accent);font-size:15px}
    .levels{flex-wrap:wrap}
    .levels a{flex:1 1 70px;font-size:13px;padding:8px 4px}
    .levels a.imp{flex-basis:110px;color:#fecaca}
    .levels a.imp.on{background:#ef4444;color:#fff}
    .levels a.locked{opacity:.55}
    .clue{padding:8px 10px;margin:6px 0;border-radius:8px;background:rgba(255,255,255,.08);text-align:left;font-size:15px;animation:cluein .4s ease-out}
    @keyframes cluein{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
    #epic{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.78);padding:16px}
    #epic.flash{animation:epicflash .9s ease-out}
    @keyframes epicflash{0%{background:#fff}100%{background:rgba(0,0,0,.78)}}
    #epicCanvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
    .epic-card{position:relative;width:100%;max-width:380px;padding:26px 22px;text-align:center;border-radius:22px;
      background:linear-gradient(160deg,#450a0a,#1e1b4b);border:2px solid var(--accent);
      box-shadow:0 0 60px rgba(250,204,21,.35);animation:epicpop .55s cubic-bezier(.2,1.4,.4,1)}
    @keyframes epicpop{0%{transform:scale(.4);opacity:0}100%{transform:scale(1);opacity:1}}
    .epic-skull{font-size:64px;line-height:1}
    .epic-card h2{margin:8px 0;letter-spacing:1px;color:var(--accent)}
    .epic-line{opacity:.9;margin:0 0 8px}
    .epic-xp{font-size:44px;font-weight:bold;font-variant-numeric:tabular-nums;margin:6px 0 10px}
    @media (prefers-reduced-motion:reduce){.epic-card,.clue,#epic.flash{animation:none}}
    .nav{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin:8px 0 12px}
    .nav a{display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 2px;border-radius:12px;font-size:11px;text-align:center}
    .nav a b{font-size:19px;font-weight:normal;line-height:1.1}
    .hero{padding:12px 14px;margin:0 0 14px;border-radius:14px;background:var(--soft);border:1px solid var(--line);text-align:left}
    .pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .pill.n{text-transform:none}
    .menu a{display:flex;justify-content:space-between;align-items:center;padding:14px;margin:8px 0;border-radius:12px;background:rgba(255,255,255,.08);color:#fff;text-decoration:none}
    .menu a:hover{background:rgba(255,255,255,.16)}
    .menu a.danger{background:rgba(239,68,68,.16);color:#fecaca}
  </style>
</head>
<body class="theme-{{ theme }}" data-auth="{{ '1' if nav_player else '' }}" data-activity="{% block activity %}online{% endblock %}">
<div class="shell">
  <header class="topbar">
    <a class="brand" href="/">🎯 Guessing Game</a>
    {% if nav_player %}<a class="bell" href="/notifications" title="Notifications">🔔<span id="bellCount" class="badge" {% if not unread %}hidden{% endif %}>{{ unread }}</span></a>{% endif %}
  </header>
  {% set ep = request.endpoint %}
  {% if nav_player %}
  <nav class="nav">
    <a href="/" class="{{ 'on' if ep == 'home' }}"><b>🎯</b>Play</a>
    <a href="/missions" class="{{ 'on' if ep == 'missions' }}"><b>📋</b>Missions</a>
    <a href="/challenges" class="{{ 'on' if ep in ('challenges', 'challenge_match') }}"><b>⚔️</b>Duels</a>
    <a href="/leaderboard" class="{{ 'on' if ep == 'leaderboard' }}"><b>🏆</b>Ranks</a>
    <a href="/profile" class="{{ 'on' if ep in ('profile', 'history', 'progress', 'change_pin', 'settings', 'delete_account') }}"><b>👤</b>Me</a>
  </nav>
  {% endif %}
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat, text in msgs %}<div class="notice {{ cat }}">{{ text }}</div>{% endfor %}
  {% endwith %}
  <main class="card {% block cardclass %}{% endblock %}">{% block content %}{% endblock %}</main>
</div>
<div id="toasts"></div>
<script>
{% raw %}
(function(){
  const body = document.body;
  function fmt(iso){
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
  }
  window.fmtTime = fmt;
  window.localizeTimes = function(root){
    (root || document).querySelectorAll('time[data-utc]').forEach(function(t){
      const s = fmt(t.dataset.utc); if (s) t.textContent = s;
    });
  };
  localizeTimes();
  if (!body.dataset.auth) return;

  function toast(n){
    const a = document.createElement('a');
    a.className = 'toast'; a.href = n.url; a.textContent = n.text;
    document.getElementById('toasts').appendChild(a);
    setTimeout(function(){ a.remove(); }, 6000);
  }
  function setBell(n){
    const b = document.getElementById('bellCount');
    if (!b) return;
    b.textContent = n; b.hidden = !n;
  }
  async function pulse(){
    try {
      const r = await fetch('/api/pulse?activity=' + encodeURIComponent(body.dataset.activity || 'online'), {cache:'no-store'});
      if (!r.ok) return;
      const d = await r.json();
      setBell(d.unread);
      let last = sessionStorage.getItem('lastToast');
      const ids = d.notices.map(function(n){ return n.id; });
      if (last === null) {                      // first page this tab: don't replay old stuff
        sessionStorage.setItem('lastToast', String(ids.length ? Math.max.apply(null, ids) : 0));
        return;
      }
      last = Number(last);
      d.notices.slice().reverse().forEach(function(n){
        if (n.id > last) { toast(n); last = n.id; }
      });
      sessionStorage.setItem('lastToast', String(last));
    } catch (e) {}
  }
  pulse();
  setInterval(pulse, 4000);
})();
{% endraw %}
</script>
<script>
{% raw %}
(function(){
  let raf = 0;
  window.celebrate = function(o){
    o = o || {};
    window.__epic = true;
    const old = document.getElementById('epic'); if (old) old.remove();
    const calm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    const ov = document.createElement('div'); ov.id = 'epic';
    const cv = document.createElement('canvas'); cv.id = 'epicCanvas';
    const card = document.createElement('div'); card.className = 'epic-card';
    const skull = document.createElement('div'); skull.className = 'epic-skull'; skull.textContent = '💀';
    const h = document.createElement('h2'); h.textContent = 'IMPOSSIBLE CONQUERED';
    const line = document.createElement('p'); line.className = 'epic-line';
    line.textContent = Number(o.tries) === 1 ? 'First try?! Nobody does that.' : 'Solved in ' + Number(o.tries) + ' tries. Most people never get here.';
    const xp = document.createElement('div'); xp.className = 'epic-xp'; xp.textContent = '+0 XP';
    const go = document.createElement('button'); go.type = 'button'; go.textContent = "Let's go 🔥";
    const sh = document.createElement('button'); sh.type = 'button'; sh.className = 'alt'; sh.textContent = 'Share the W 📤';
    card.append(skull, h, line, xp, go, sh);
    ov.append(cv, card);
    document.body.appendChild(ov);
    if (!calm) ov.classList.add('flash');
    function close(){ cancelAnimationFrame(raf); ov.remove(); }
    go.onclick = close;
    sh.onclick = async function(){
      const text = 'I just conquered IMPOSSIBLE mode in ' + Number(o.tries) + ' tries 💀🔥 Can you? ' + location.origin;
      try {
        if (navigator.share) { await navigator.share({text: text}); }
        else { await navigator.clipboard.writeText(text); sh.textContent = 'Copied ✅'; }
      } catch (e) {}
    };
    const target = Number(o.xp) || 0, t0 = performance.now();
    (function tick(now){
      const p = Math.min(1, (now - t0) / 1200);
      xp.textContent = '+' + Math.round(target * p) + ' XP';
      if (p < 1) requestAnimationFrame(tick);
    })(t0);
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.resume) ctx.resume();
      [523.25, 659.25, 783.99, 1046.5, 1318.5].forEach(function(f, i){
        const osc = ctx.createOscillator(), g = ctx.createGain();
        osc.type = 'triangle'; osc.frequency.value = f;
        const t = ctx.currentTime + i * 0.11;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
        osc.connect(g); g.connect(ctx.destination); osc.start(t); osc.stop(t + 0.4);
      });
    } catch (e) {}
    try { if (navigator.vibrate) navigator.vibrate([80, 40, 80, 40, 220]); } catch (e) {}
    if (!calm) {
      const c = cv.getContext('2d');
      cv.width = window.innerWidth; cv.height = window.innerHeight;
      const colors = ['#facc15', '#f472b6', '#60a5fa', '#34d399', '#f87171', '#ffffff'];
      const bits = [];
      for (let i = 0; i < 220; i++) {
        bits.push({
          x: Math.random() * cv.width, y: -20 - Math.random() * cv.height * 0.6,
          w: 6 + Math.random() * 7, h: 10 + Math.random() * 8,
          vx: -2 + Math.random() * 4, vy: 2 + Math.random() * 4,
          r: Math.random() * 6.28, vr: -0.2 + Math.random() * 0.4,
          c: colors[(Math.random() * colors.length) | 0]
        });
      }
      const end = performance.now() + 5500;
      (function frame(now){
        c.clearRect(0, 0, cv.width, cv.height);
        bits.forEach(function(b){
          b.x += b.vx; b.y += b.vy; b.vy += 0.04; b.r += b.vr;
          if (b.y > cv.height + 20 && now < end - 1500) { b.y = -20; b.x = Math.random() * cv.width; b.vy = 2 + Math.random() * 3; }
          c.save(); c.translate(b.x, b.y); c.rotate(b.r); c.fillStyle = b.c;
          c.fillRect(-b.w / 2, -b.h / 2, b.w, b.h); c.restore();
        });
        if (now < end) raf = requestAnimationFrame(frame);
      })(performance.now());
    }
  };
})();
{% endraw %}
</script>
{% block scripts %}{% endblock %}
</body>
</html>
"""

GAME = r"""{% extends "base.html" %}
{% block content %}
<h1>🎯 Guessing Game</h1>
{% if not player %}
  <p class="sub">Pick a name and a 4-digit PIN. New name? It's yours. Returning? Enter your PIN.</p>
  <form method="post" action="/name">
    <input name="name" maxlength="20" placeholder="Player name" required autofocus>
    <input name="pin" type="password" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" placeholder="4-digit PIN" required>
    <button type="submit">Start playing</button>
  </form>
  <div class="msg {{ kind }}">{{ message }}</div>
  <div class="links"><a href="/leaderboard">Leaderboard</a></div>
  <p class="muted">Create an account to save XP, badges and progress.</p>
{% else %}
  <div class="hero">
    <div class="row"><span>👤 <b>{{ player }}</b></span><span class="pill n">Lv {{ player_level }} · ⭐ {{ total_xp }} XP</span></div>
    <div class="track"><span style="width:{{ total_xp % 100 }}%"></span></div>
    <div class="pills">
      <span class="pill n">🎁 Daily: {{ daily_status|safe }}</span>
      <span class="pill n">🎯 {{ 'Win bonus claimed ✅' if bonus_claimed else 'Win bonus ready' }}</span>
    </div>
  </div>
  <p class="sub">I'm thinking of a number between 1 and {{ hi }}.</p>
  <div class="levels">
    {% for name in levels %}{% set locked = name == 'impossible' and not imp_unlocked %}<a href="/?level={{ name }}" class="{{ 'on' if name == level else '' }}{{ ' imp' if name == 'impossible' else '' }}{{ ' locked' if locked else '' }}">{{ '🔒 ' if locked else ('💀 ' if name == 'impossible' else '') }}{{ name.title() }}</a>{% endfor %}
  </div>
  {% if not imp_unlocked %}<p class="muted">🔒 Impossible mode unlocks after {{ unlock_wins }} wins ({{ wins_total }}/{{ unlock_wins }})</p>{% endif %}
  {% if clues %}
    <div class="box" id="clues"><b>🧠 Clues</b>
      {% for c in clues %}<div class="clue">{{ c }}</div>{% endfor %}
      <p class="muted" style="margin:6px 0 0">A new clue drops after every wrong guess.</p>
    </div>
  {% endif %}
  {% if over %}
    <a class="btn" href="/">Play again 🔁</a>
  {% else %}
    <form method="post">
      <input type="number" name="guess" min="1" max="{{ hi }}" placeholder="?" required autofocus>
      <button type="submit">Guess</button>
    </form>
  {% endif %}
  <div class="msg {{ kind }}" id="solo-msg">{{ message }}</div>
  <div class="dots">{% for i in range(max_tries) %}<span class="{{ 'used' if i < attempts else '' }}"></span>{% endfor %}</div>
  {% if best %}<div class="small">🏆 Your best on {{ level }}: {{ best }}</div>{% endif %}
{% endif %}
{% endblock %}
{% block scripts %}
{% if epic %}<script>celebrate({xp: {{ epic.xp|int }}, tries: {{ epic.tries|int }}});</script>{% endif %}
<script>
{% raw %}
(function(){
  const m = document.getElementById('solo-msg');
  if (m && !window.__epic && (m.classList.contains('win') || m.classList.contains('lose'))) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value = m.classList.contains('win') ? 880 : 220;
      g.gain.value = 0.035; o.start(); o.stop(ctx.currentTime + 0.12);
    } catch (e) {}
  }
})();
{% endraw %}
</script>
{% endblock %}
"""

BOARD = r"""{% extends "base.html" %}
{% block title %}Leaderboard{% endblock %}
{% block content %}
<h1>🏆 Leaderboard</h1>
<p class="muted">Ranked by XP · best attempts on {{ level.title() }} · updates live</p>
<div class="levels">
  {% for name in levels %}<a href="/leaderboard?level={{ name }}" class="{{ 'on' if name == level else '' }}{{ ' imp' if name == 'impossible' else '' }}">{{ '💀 ' if name == 'impossible' else '' }}{{ name.title() }}</a>{% endfor %}
</div>
<table id="board" data-level="{{ level }}" data-me="{{ player or '' }}">
  <thead><tr><th>#</th><th>Name</th><th>XP</th><th>Best</th></tr></thead>
  <tbody>
  {% for r in rows %}
    <tr class="{{ 'me' if r.name == player else '' }}">
      <td>{{ loop.index }}</td>
      <td><span class="dot {{ r.state }}"></span>{{ r.name }}</td>
      <td>⭐ {{ r.xp }}</td>
      <td>{{ r.best if r.best is not none else '-' }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<p class="sub" id="board-empty" {% if rows %}hidden{% endif %}>No players yet. Be the first 👀</p>

{% endblock %}
{% block scripts %}
<script>
{% raw %}
(function(){
  const t = document.getElementById('board');
  const level = t.dataset.level, me = t.dataset.me;
  async function refresh(){
    try {
      const r = await fetch('/api/leaderboard?level=' + encodeURIComponent(level), {cache:'no-store'});
      if (!r.ok) return;
      const d = await r.json();
      const tb = t.querySelector('tbody');
      tb.replaceChildren();
      d.rows.forEach(function(row, i){
        const tr = document.createElement('tr');
        if (row.name === me) tr.className = 'me';
        const c1 = document.createElement('td'); c1.textContent = String(i + 1);
        const c2 = document.createElement('td');
        const dot = document.createElement('span'); dot.className = 'dot ' + row.state;
        c2.appendChild(dot); c2.appendChild(document.createTextNode(row.name));
        const c3 = document.createElement('td'); c3.textContent = '⭐ ' + row.xp;
        const c4 = document.createElement('td'); c4.textContent = row.best === null ? '-' : String(row.best);
        tr.append(c1, c2, c3, c4); tb.appendChild(tr);
      });
      document.getElementById('board-empty').hidden = d.rows.length > 0;
    } catch (e) {}
  }
  setInterval(refresh, 5000);
})();
{% endraw %}
</script>
{% endblock %}
"""

HIST = r"""{% extends "base.html" %}
{% block title %}History{% endblock %}
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
    <td>{% if g.created %}<time data-utc="{{ iso(g.created) }}">{{ g.created.strftime('%b %d') }}</time>{% endif %}</td>
    <td>{{ g.level.title() }}</td><td>{{ '✅' if g.won else '❌' }}</td><td>{{ g.attempts }}</td><td>{{ g.secret }}</td>
  </tr>
  {% endfor %}
</table>
<p class="sub">Showing your last 20 games.</p>
{% else %}
<p class="sub">No games yet. Go play one 👀</p>
{% endif %}

{% endblock %}
"""

MISSIONS = r"""{% extends "base.html" %}
{% block title %}Daily Missions{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>🎯 Daily Missions</h1>
<p class="sub">Complete today's goals for bonus XP. Missions reset daily (UTC); your total XP stays. Duel wins count too.</p>
{% for m in missions %}
<section class="box">
  <div class="row"><h3 style="margin:0">{{ m.title }}</h3><b>+{{ m.reward }} XP</b></div>
  <p class="muted">{{ m.description }}</p>
  <div class="track"><span style="width:{{ m.bar }}%"></span></div>
  <p class="muted">{{ '✅ Completed' if m.completed else (m.progress ~ '/' ~ m.target ~ ' progress') }}</p>
</section>
{% endfor %}

{% endblock %}
"""

PROFILE = r"""{% extends "base.html" %}
{% block title %}Profile{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>👤 {{ name }}</h1>
<div class="box">
  <div class="row"><span>⭐ XP: <b>{{ xp }}</b></span><span>Level <b>{{ level }}</b></span></div>
  <div class="track"><span style="width:{{ xp % 100 }}%"></span></div>
  <p class="muted">{{ 100 - (xp % 100) }} XP to level {{ level + 1 }}</p>
</div>
<div class="box">
  <div class="row"><span>🎮 Games: <b>{{ played }}</b></span><span>🏆 Wins: <b>{{ wins }}</b></span></div>
  <p class="muted">⚔️ Duels: {{ duel_wins }} won / {{ duels }} played</p>
  <p>🏅 {{ badges|join(', ') if badges else 'No badges yet' }}</p>
</div>
<div class="box">
  <p style="margin:0 0 6px">🎁 Daily login reward: <b>{{ 'already claimed today' if claimed else 'ready to claim' }}</b></p>
  {% if not claimed %}<form method="post" action="/claim-daily"><button type="submit">Claim +10 XP</button></form>{% endif %}
</div>
<div class="menu">
  <a href="/history"><span>📜 History</span><span>›</span></a>
  <a href="/progress"><span>📈 Progress</span><span>›</span></a>
  <a href="/settings"><span>🎨 Theme</span><span>›</span></a>
  <a href="/change-pin"><span>🔐 Change PIN</span><span>›</span></a>
  <a href="/name"><span>🚪 Log out</span><span>›</span></a>
  <a href="/delete-account" class="danger"><span>🗑️ Delete account</span><span>›</span></a>
</div>
{% endblock %}
"""

CHANGE_PIN = r"""{% extends "base.html" %}
{% block title %}Change PIN{% endblock %}
{% block content %}
<h1>🔐 Change PIN</h1>
<form method="post">
  <input name="old_pin" type="password" inputmode="numeric" maxlength="4" placeholder="Current PIN" required>
  <input name="new_pin" type="password" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" placeholder="New 4-digit PIN" required>
  <button type="submit">Change PIN</button>
</form>
<a class="btn alt" href="/profile">← Back to profile</a>
{% endblock %}
"""

SETTINGS = r"""{% extends "base.html" %}
{% block title %}Theme{% endblock %}
{% block content %}
<h1>🎨 Choose a theme</h1>
<p class="sub">Saved to your account, so it follows you on every page and device.</p>
<form method="post">
  {% for key, label in themes.items() %}
    <button type="submit" name="theme" value="{{ key }}" class="{{ '' if key == theme else 'alt' }}">{{ label }}{{ ' ✓' if key == theme }}</button>
  {% endfor %}
</form>

{% endblock %}
"""

PROGRESS = r"""{% extends "base.html" %}
{% block title %}Progress{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>📈 Progress</h1>
<div class="box">
  <div class="row"><span>Total games: <b>{{ total }}</b></span><span>Wins: <b>{{ wins }}</b></span></div>
  <p class="muted">Win rate: {{ rate }}%</p>
  <div class="track"><span style="width:{{ rate }}%"></span></div>
</div>
<div class="box">
  <b>Personal bests (fewest tries)</b>
  {% for lvl, b in bests %}<div class="row"><span>{{ lvl.title() }}</span><span>{{ b if b else '-' }}</span></div>{% endfor %}
</div>
<h3>Last 20 games</h3>
<div class="hist">
  {% for n, g in recent %}<div><span>Game {{ n }}</span><span>{{ '🏆 Win' if g.won else '❌ Loss' }} · {{ g.attempts }} tries · {{ g.level.title() }}</span></div>
  {% else %}<p class="muted">No games yet.</p>{% endfor %}
</div>

{% endblock %}
"""

NOTIFS = r"""{% extends "base.html" %}
{% block title %}Notifications{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>🔔 Notifications</h1>
{% for n in notices %}
  <a class="box" style="display:block;color:#fff;text-decoration:none;{{ 'border-color:var(--accent)' if not n.seen }}" href="{{ n.url }}">
    <div>{{ '🆕 ' if not n.seen }}{{ n.text }}</div>
    <div class="muted"><time data-utc="{{ iso(n.created) }}">{{ n.created.strftime('%d %b %H:%M') }} UTC</time></div>
  </a>
{% else %}<p class="muted">Nothing yet. Challenge someone!</p>{% endfor %}
<a class="btn alt" href="/challenges">Go to challenges</a>
{% endblock %}
"""

CHALLENGES = r"""{% extends "base.html" %}
{% block title %}Challenges{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>🤝 Challenges</h1>
<p class="muted">Same number, same tries, same clock. Fewest tries wins, ties are draws. Invites expire after {{ invite_hours }}h.</p>
<form method="post" action="/challenges">
  <input type="hidden" name="action" value="send">
  <label for="opponent">Opponent username</label>
  <input id="opponent" name="opponent" maxlength="20" placeholder="Enter exact player name" required>
  <label for="level">Difficulty</label>
  <select id="level" name="level">
    {% for k, (hi, tries) in levels.items() %}<option value="{{ k }}" {{ 'selected' if k == 'medium' }} {{ 'disabled' if k == 'impossible' and not imp_unlocked }}>{{ '💀 ' if k == 'impossible' else '' }}{{ k.title() }}{{ ' 🔒' if k == 'impossible' and not imp_unlocked else '' }} · 1–{{ hi }} · {{ tries }} tries · {{ clocks[k] }}s</option>{% endfor %}
  </select>
  <button type="submit">Send challenge →</button>
</form>
<div id="challengeList">{% include "challenge_list.html" %}</div>
{% endblock %}
{% block scripts %}
<script>
{% raw %}
(function(){
  const box = document.getElementById('challengeList');
  let cache = null;
  async function refresh(){
    try {
      const r = await fetch('/challenges/fragment', {cache:'no-store'});
      if (!r.ok) return;
      const h = await r.text();
      if (cache !== null && h !== cache) { box.innerHTML = h; localizeTimes(box); }
      cache = h;
    } catch (e) {}
  }
  refresh();
  setInterval(refresh, 4000);
  document.addEventListener('click', function(e){
    const b = e.target.closest('.chip');
    if (!b) return;
    const i = document.getElementById('opponent');
    i.value = b.dataset.name; i.focus();
  });
})();
{% endraw %}
</script>
{% endblock %}
"""

CHALLENGE_LIST = r"""<h2>🟢 Online now</h2>
{% if online %}
  <div class="chips">
    {% for p in online %}<button type="button" class="chip" data-name="{{ p.name }}"><span class="dot {{ 'in-game' if p.activity == 'in-game' else 'online' }}"></span>{{ p.name }}{{ ' · in a match' if p.activity == 'in-game' }}</button>{% endfor %}
  </div>
  <p class="muted">Tap a name to challenge them.</p>
{% else %}<p class="muted">Nobody else is online right now.</p>{% endif %}

<h2>📬 Your challenges</h2>
{% for it in items %}
  {% set c = it.c %}
  <section class="box">
    <div class="row"><strong>{{ c.creator|pn }} vs {{ c.opponent|pn }}</strong><span class="pill">{{ c.status }}</span></div>
    <p class="muted" style="margin:6px 0 2px">{{ 'Incoming' if it.incoming else 'Sent' }} · {{ it.level }} · ⏱ {{ it.limit }}s clock</p>
    <p class="muted" style="margin:2px 0"><span class="dot {{ it.presence.state }}"></span>{{ it.other|pn }} is {{ it.presence.state }}{% if it.presence.state == 'offline' and it.presence.last_seen %} · last seen <time data-utc="{{ it.presence.last_seen }}"></time>{% endif %}</p>
    <p class="muted" style="margin:2px 0">Sent <time data-utc="{{ iso(c.created) }}">{{ c.created.strftime('%d %b %H:%M') if c.created else '' }}</time></p>
    {% if it.incoming and c.status == 'pending' %}
      <div class="actions">
        <form method="post" action="/challenges"><input type="hidden" name="challenge_id" value="{{ c.id }}"><input type="hidden" name="action" value="accept"><button type="submit">✓ Accept</button></form>
        <form method="post" action="/challenges"><input type="hidden" name="challenge_id" value="{{ c.id }}"><input type="hidden" name="action" value="decline"><button class="danger" type="submit">✕ Decline</button></form>
      </div>
    {% endif %}
    {% if c.status == 'accepted' %}<a class="btn" href="/challenge/{{ c.id }}">▶ Open match</a>{% endif %}
    {% if it.result %}<p style="margin:8px 0 0"><b>{{ it.result }}</b> · <a href="/challenge/{{ c.id }}">view</a></p>{% endif %}
  </section>
{% else %}<p class="muted">No challenges yet. Send your first one!</p>{% endfor %}
"""

MATCH = r"""{% extends "base.html" %}
{% block title %}Match{% endblock %}
{% block activity %}in-game{% endblock %}
{% block content %}
<section id="match" data-cid="{{ c.id }}" data-hi="{{ hi }}" data-max="{{ max_tries }}" data-limit="{{ limit }}">
  <h1>⚔️ {{ c.creator|pn }} vs {{ c.opponent|pn }}</h1>
  <p class="muted">{{ c.level.title() }} · number 1–{{ hi }} · {{ max_tries }} tries · {{ limit }}s clock</p>
  <div class="clock" id="clock">--:--</div>
  <p class="muted" id="clockLabel"></p>
  <p class="msg" id="banner"></p>
  <p class="msg" id="flash" style="font-size:16px;min-height:20px"></p>
  <div class="dots" id="dots"></div>
  <div class="box">
    <div><span class="dot" id="oppdot"></span><b id="oppname">{{ other|pn }}</b> <span id="oppstat" class="muted"></span></div>
    <div class="muted" id="oppprog" style="margin-top:4px"></div>
  </div>
  <div class="box" id="clueBox" hidden><b>🧠 Clues</b><div id="clues"></div></div>
  <form id="readyForm" hidden><button type="submit">I'm ready</button></form>
  <form id="guessForm" hidden>
    <input id="guess" type="number" min="1" max="{{ hi }}" placeholder="Your guess (1–{{ hi }})" required>
    <button type="submit">Submit guess</button>
  </form>
  <div class="hist" id="history"></div>
  <div class="box" id="result" hidden></div>
  <p class="muted">Each player plays independently on the same clock. Speed doesn't win, fewer tries does. Everything is saved.</p>
  <a href="/challenges">← Back to challenges</a>
</section>
</main>
<main class="card left">
  <h2 style="margin-top:0">💬 Match chat</h2>
  <div id="chat" class="chatbox"></div>
  <form id="chatForm">
    <input id="chatInput" maxlength="300" placeholder="Send a message…" autocomplete="off" required>
    <button type="submit">Send</button>
  </form>
{% endblock %}
{% block scripts %}
<script>
{% raw %}
(function(){
  const root = document.getElementById('match');
  const cid = root.dataset.cid, maxTries = Number(root.dataset.max), limit = Number(root.dataset.limit);
  const $ = function(id){ return document.getElementById(id); };
  let deadline = null, phase = '', chatKey = '', busy = false;

  function mmss(s){
    s = Math.max(0, Math.ceil(s));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }
  async function post(payload){
    const r = await fetch('/api/challenge/' + cid + '/action', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    return r.json();
  }
  function flash(text, kind){
    const f = $('flash'); f.textContent = text || ''; f.className = 'msg ' + (kind || '');
    f.style.fontSize = '16px';
  }

  function render(d){
    if (d.phase === 'closed') { location.href = '/challenges'; return; }
    phase = d.phase;
    $('banner').textContent = d.banner;
    $('banner').className = 'msg ' + d.banner_kind;
    deadline = d.remaining === null ? null : Date.now() + d.remaining * 1000;
    const clock = $('clock');
    if (phase === 'lobby') { clock.textContent = mmss(limit); clock.classList.remove('danger'); $('clockLabel').textContent = 'Clock starts when both players are ready'; }
    else if (phase === 'done') { clock.textContent = '🏁'; clock.classList.remove('danger'); $('clockLabel').textContent = 'Match finished'; }
    else { $('clockLabel').textContent = 'Time left'; }

    const dots = $('dots'); dots.replaceChildren();
    for (let i = 0; i < maxTries; i++) {
      const s = document.createElement('span'); if (i < d.me.attempts) s.className = 'used'; dots.appendChild(s);
    }

    $('oppdot').className = 'dot ' + d.opp.presence;
    let st = ' is ' + d.opp.presence;
    if (d.opp.presence === 'offline' && d.opp.last_seen) st += ' · last seen ' + fmtTime(d.opp.last_seen);
    $('oppstat').textContent = st;
    $('oppprog').textContent = phase === 'lobby'
      ? (d.opp.ready ? 'Ready ✅' : 'Not ready yet ⏳')
      : 'Tries: ' + d.opp.attempts + '/' + maxTries + ' · ' + (d.opp.finished ? 'Finished' : 'Playing');

    $('readyForm').hidden = !(phase === 'lobby' && !d.me.ready);
    $('guessForm').hidden = !(phase === 'live' && !d.me.finished);

    const h = $('history'); h.replaceChildren();
    d.me.guesses.forEach(function(g){
      const row = document.createElement('div');
      const a = document.createElement('span'); a.textContent = '#' + g.n + ' · ' + g.guess;
      const b = document.createElement('span'); b.textContent = g.hint;
      row.append(a, b); h.appendChild(row);
    });

    const cb = $('clueBox'), cl = $('clues');
    if (d.clues && d.clues.length) {
      cb.hidden = false; cl.replaceChildren();
      d.clues.forEach(function(t){
        const p = document.createElement('div'); p.className = 'clue'; p.textContent = t; cl.appendChild(p);
      });
    } else { cb.hidden = true; }
    if (d.result && d.level === 'impossible' && d.result.winner === d.you) {
      let seen = false;
      try { seen = localStorage.getItem('epic' + cid); localStorage.setItem('epic' + cid, '1'); } catch (e) {}
      if (!seen) celebrate({xp: d.result.xp, tries: d.me.attempts});
    }
    const res = $('result');
    if (d.result) {
      res.hidden = false;
      res.textContent = d.result.text + ' The number was ' + d.result.secret + '.' +
        (d.result.xp ? ' You earned +' + d.result.xp + ' XP.' : '');
    } else { res.hidden = true; }

    const key = d.messages.map(function(m){ return m.id; }).join(',');
    if (key !== chatKey) {
      chatKey = key;
      const c = $('chat');
      const stick = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
      c.replaceChildren();
      if (!d.messages.length) {
        const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No messages yet. Be civil-ish 😅'; c.appendChild(p);
      }
      d.messages.forEach(function(m){
        const p = document.createElement('p');
        p.className = 'chatline' + (m.mine ? ' mine' : '');
        p.textContent = fmtTime(m.created) + ' · ' + m.sender + ': ' + m.body;
        c.appendChild(p);
      });
      if (stick || !chatKey) c.scrollTop = c.scrollHeight;
    }
  }

  async function load(){
    try {
      const r = await fetch('/api/challenge/' + cid + '/state', {cache:'no-store'});
      if (r.status === 404) { location.href = '/challenges'; return; }
      if (!r.ok) return;
      render(await r.json());
    } catch (e) { $('oppstat').textContent = ' · connection interrupted, retrying…'; }
  }

  $('readyForm').addEventListener('submit', async function(e){
    e.preventDefault();
    const d = await post({action:'ready'}); flash(d.message, d.ok ? 'success' : 'error'); load();
  });
  $('guessForm').addEventListener('submit', async function(e){
    e.preventDefault();
    const inp = $('guess'); const v = inp.value; inp.value = '';
    const d = await post({action:'guess', guess:v}); flash(d.message, d.kind || (d.ok ? 'info' : 'error'));
    load(); inp.focus();
  });
  $('chatForm').addEventListener('submit', async function(e){
    e.preventDefault();
    const inp = $('chatInput'); const v = inp.value; inp.value = '';
    const d = await post({action:'chat', message:v});
    if (!d.ok) flash(d.message, 'error');
    chatKey = ''; load(); inp.focus();
  });

  setInterval(function(){
    if (phase === 'live' && deadline !== null) {
      const left = (deadline - Date.now()) / 1000;
      const c = $('clock'); c.textContent = mmss(left); c.classList.toggle('danger', left <= 15);
      if (left <= 0 && !busy) { busy = true; load().finally(function(){ busy = false; }); }
    }
  }, 250);
  load();
  setInterval(load, 1500);
})();
{% endraw %}
</script>
{% endblock %}
"""

DELETE_ACCOUNT = r"""{% extends "base.html" %}
{% block title %}Delete account{% endblock %}
{% block cardclass %}left{% endblock %}
{% block content %}
<h1>🗑️ Delete account</h1>
<p class="sub">This is permanent. Here's what happens:</p>
<div class="box">
  <p style="margin:0 0 6px">❌ Your name, PIN, XP, badges, history, scores and missions are erased.</p>
  <p style="margin:0 0 6px">❌ You disappear from the leaderboard.</p>
  <p style="margin:0 0 6px">❌ Your chat messages are deleted and open challenges are cancelled.</p>
  <p style="margin:0">ℹ️ Finished duels stay in your opponent's history, with your name replaced by "a deleted player".</p>
</div>
<form method="post">
  <label for="pin">Enter your PIN</label>
  <input id="pin" name="pin" type="password" inputmode="numeric" maxlength="4" placeholder="Your PIN" required>
  <label for="confirm">Type DELETE to confirm</label>
  <input id="confirm" name="confirm" autocomplete="off" placeholder="DELETE" required>
  <button class="danger" type="submit">Delete my account forever</button>
</form>
<a class="btn alt" href="/profile">Cancel</a>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "game.html": GAME, "board.html": BOARD, "history.html": HIST,
    "missions.html": MISSIONS, "profile.html": PROFILE, "change_pin.html": CHANGE_PIN,
    "settings.html": SETTINGS, "progress.html": PROGRESS, "notifications.html": NOTIFS,
    "challenges.html": CHALLENGES, "challenge_list.html": CHALLENGE_LIST, "match.html": MATCH,
    "delete_account.html": DELETE_ACCOUNT,
})
app.jinja_env.globals["iso"] = iso
app.jinja_env.filters["pn"] = pname


@app.context_processor
def inject_globals():
    player = session.get("player")
    theme = session.get("theme", "purple")
    unread = 0
    if player:
        row = db.session.get(PlayerTheme, player)
        if row and row.theme in THEMES:
            theme = row.theme
        unread = ChallengeNotice.query.filter_by(recipient=player, seen=False).count()
    if theme not in THEMES:
        theme = "purple"
    return {"analytics": os.environ.get("ANALYTICS_SCRIPT", ""), "theme": theme,
            "themes": THEMES, "nav_player": player, "unread": unread}


@app.after_request
def no_store_for_api(resp):
    if request.path.startswith("/api/") or request.path == "/challenges/fragment":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- auth
@app.before_request
def drop_deleted_player():
    """If this browser is still logged in as a deleted account, log it out instead of recreating data."""
    name = session.get("player")
    if name and db.session.query(Player.id).filter_by(name=name).first() is None:
        session.pop("player", None)


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
        if not re.fullmatch(r"[A-Za-z0-9_ -]{1,20}", name):
            session["login_error"] = "Use only letters, numbers, spaces, _ or -."
            return redirect(url_for("home"))
        player = Player(name=name, pin_hash=generate_password_hash(pin))
        db.session.add(player)
        db.session.add(PlayerXP(name=name, xp=0))
        db.session.add(PlayerTheme(name=name, theme=session.get("theme", "purple")
                                   if session.get("theme") in THEMES else "purple"))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            session["login_error"] = "That name was just taken. Try again."
            return redirect(url_for("home"))
    else:
        now = utcnow()
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
    th = db.session.get(PlayerTheme, player.name)
    if th and th.theme in THEMES:
        session["theme"] = th.theme
    return redirect(url_for("home"))
    
# ---------------------------------------------------------------- solo game
@app.route("/", methods=["GET", "POST"])
def home():
    session.permanent = True
    if not session.get("seen"):
        session["seen"] = True
        bump("visitors")

    player = session.get("player")
    login_error = session.pop("login_error", None)

    new_level = request.args.get("level")
    lock_msg = ""
    if request.method == "GET" and new_level in LEVELS:
        if new_level == "impossible" and not impossible_unlocked(player):
            lock_msg = (f"🔒 Impossible unlocks after {UNLOCK_WINS} wins. "
                        f"You have {win_count(player)}.")
        else:
            session["level"] = new_level
            solo_reset(new_level)

    level = session.get("level", "medium")
    if level not in LEVELS or (level == "impossible" and not impossible_unlocked(player)):
        level = "medium"
        session["level"] = "medium"
    hi, max_tries = LEVELS[level]
    st = solo_get(level)

    message, kind, over = "", "", False
    epic = None
    if login_error:
        message, kind = login_error, "high"
    elif lock_msg:
        message, kind = lock_msg, "high"
    attempts = st.attempts

    if request.method == "POST" and player:
        try:
            guess = int(request.form["guess"])
        except (KeyError, ValueError):
            guess = None

        if guess is None or not 1 <= guess <= hi:
            message, kind = f"Pick a number from 1 to {hi}", "high"
        else:
            st.attempts += 1
            db.session.commit()                 # persist the try before anything that may roll back
            tries, secret = st.attempts, st.secret
            attempts = tries

            if guess == secret:
                over, kind = True, "win"
                xp_reward = max(5, 35 - (tries * 5))
                if level == "impossible":
                    xp_reward = xp_reward * 3 + 50
                first_win_today = claim_once(DailyWinBonus, name=player, bonus_date=today_str())
                daily_bonus = 20 if first_win_today else 0
                db.session.add(Score(name=player, level=level, attempts=tries))
                db.session.add(Game(name=player, level=level, won=True, attempts=tries, secret=secret))
                award_xp(player, xp_reward + daily_bonus)
                update_daily_missions(player, True, level, tries)
                st = solo_get(level)
                st.secret, st.attempts = random.randint(1, hi), 0
                db.session.commit()
                message = f"You got it in {tries} attempts! 🎉 Saved to the leaderboard. ⭐ +{xp_reward} XP!"
                if daily_bonus:
                    message += f" 🎯 +{daily_bonus} daily challenge bonus!"
                if level == "impossible":
                    epic = {"xp": xp_reward + daily_bonus, "tries": tries}
                bump("games_played")
                bump("wins")
            elif tries >= max_tries:
                over, kind = True, "lose"
                db.session.add(Game(name=player, level=level, won=False, attempts=tries, secret=secret))
                play_xp = 10 if level == "impossible" else 5
                award_xp(player, play_xp)
                update_daily_missions(player, False, level, tries)
                st = solo_get(level)
                st.secret, st.attempts = random.randint(1, hi), 0
                db.session.commit()
                message = f"Out of attempts. It was {secret} 💀 ⭐ +{play_xp} XP for playing!"
                bump("games_played")
            else:
                message, kind = hint_for(guess, secret, level)

    best, total_xp, bonus_claimed, daily_status = None, 0, False, ""
    if player:
        best = (db.session.query(func.min(Score.attempts)).filter_by(name=player, level=level).scalar())
        total_xp = get_xp(player)
        bonus_claimed = DailyWinBonus.query.filter_by(name=player, bonus_date=today_str()).first() is not None
        claimed = DailyReward.query.filter_by(name=player, reward_date=today_str()).first() is not None
        daily_status = "claimed today ✅" if claimed else '<a href="/profile">claim it on your profile</a>'

    clues = []
    if level == "impossible" and player and not over:
        clues = clues_shown(impossible_clues(st.secret, session.get("sid", "")), st.attempts)
    return render_template(
        "game.html", message=message, kind=kind, over=over, attempts=attempts,
        level=level, levels=list(LEVELS), hi=hi, max_tries=max_tries, player=player,
        best=best, total_xp=total_xp, player_level=get_level(total_xp),
        badges=get_badges(player) if player else [], daily_status=daily_status,
        bonus_claimed=bonus_claimed, epic=epic, clues=clues,
        imp_unlocked=impossible_unlocked(player), unlock_wins=UNLOCK_WINS,
        wins_total=win_count(player),
    )


# ---------------------------------------------------------------- leaderboard / history
def leaderboard_rows(level):
    best = (db.session.query(Score.name.label("name"), func.min(Score.attempts).label("best"))
            .filter(Score.level == level).group_by(Score.name).subquery())
    xp = func.coalesce(PlayerXP.xp, 0)
    q = (db.session.query(Player.name, xp.label("xp"), best.c.best)
         .outerjoin(PlayerXP, PlayerXP.name == Player.name)
         .outerjoin(best, best.c.name == Player.name)
         .order_by(xp.desc(), best.c.best.is_(None), best.c.best.asc(), Player.name.asc())
         .limit(100))
    return [{"name": n, "xp": int(x), "best": b, "state": presence_of(n)["state"]} for n, x, b in q.all()]


@app.route("/leaderboard")
def leaderboard():
    level = request.args.get("level") or session.get("level", "medium")
    if level not in LEVELS:
        level = "medium"
    return render_template("board.html", rows=leaderboard_rows(level), level=level,
                           levels=list(LEVELS), player=session.get("player"))


@app.route("/api/leaderboard")
def api_leaderboard():
    level = request.args.get("level", "medium")
    if level not in LEVELS:
        level = "medium"
    return jsonify(rows=leaderboard_rows(level))


@app.route("/history")
def history():
    player = session.get("player")
    if not player:
        return redirect(url_for("home"))
    games = Game.query.filter_by(name=player).order_by(Game.created.desc(), Game.id.desc()).all()
    played = len(games)
    wins = sum(1 for g in games if g.won)
    streak = 0
    for g in games:
        if not g.won:
            break
        streak += 1
    rate = round(100 * wins / played) if played else 0
    return render_template("history.html", games=games[:20], played=played, wins=wins,
                           rate=rate, streak=streak, player=player)


# ---------------------------------------------------------------- missions / profile / settings
@app.route("/missions")
def missions():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    today = today_str()
    rows = []
    for key, d in MISSION_DEFINITIONS.items():
        row = DailyMission.query.filter_by(name=name, mission_date=today, mission_key=key).first()
        progress = row.progress if row else 0
        rows.append({"title": d["title"], "description": d["description"], "reward": d["reward"],
                     "target": d["target"], "progress": progress,
                     "completed": bool(row and row.completed),
                     "bar": min(100, int(progress * 100 / d["target"]))})
    return render_template("missions.html", missions=rows)


@app.route("/profile")
def profile():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    games = Game.query.filter_by(name=name).all()
    xp = get_xp(name)
    duels = Challenge.query.filter(Challenge.status == "completed",
                                   (Challenge.creator == name) | (Challenge.opponent == name)).all()
    duel_wins = sum(1 for c in duels if (c.creator == name and c.creator_won) or
                    (c.opponent == name and c.opponent_won))
    claimed = DailyReward.query.filter_by(name=name, reward_date=today_str()).first() is not None
    return render_template("profile.html", name=name, xp=xp, level=get_level(xp),
                           played=len(games), wins=sum(1 for g in games if g.won),
                           duels=len(duels), duel_wins=duel_wins,
                           badges=get_badges(name), claimed=claimed)


@app.route("/claim-daily", methods=["GET", "POST"])
def claim_daily():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    if request.method == "GET":
        return redirect(url_for("profile"))
    if claim_once(DailyReward, name=name, reward_date=today_str()):
        award_xp(name, 10)
        db.session.commit()
        flash("🎁 You claimed 10 XP!", "success")
    else:
        flash("Daily reward already claimed today.", "info")
    return redirect(url_for("profile"))


@app.route("/change-pin", methods=["GET", "POST"])
def change_pin():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    if request.method == "POST":
        old_pin = request.form.get("old_pin", "")
        new_pin = request.form.get("new_pin", "")
        player = Player.query.filter_by(name=name).first()
        now = utcnow()
        if player and player.locked_until and player.locked_until > now:
            flash("Too many wrong tries. Wait a few minutes.", "error")
        elif not player or not check_password_hash(player.pin_hash, old_pin):
            if player:
                player.fails += 1
                if player.fails >= MAX_FAILS:
                    player.fails = 0
                    player.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                db.session.commit()
            flash("Current PIN is incorrect.", "error")
        elif not re.fullmatch(r"\d{4}", new_pin):
            flash("New PIN must be exactly 4 digits.", "error")
        else:
            player.pin_hash = generate_password_hash(new_pin)
            player.fails = 0
            db.session.commit()
            flash("PIN updated successfully.", "success")
            return redirect(url_for("profile"))
        return redirect(url_for("change_pin"))
    return render_template("change_pin.html")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    if request.method == "POST":
        theme = request.form.get("theme", "purple")
        if theme in THEMES:
            session["theme"] = theme
            row = db.session.get(PlayerTheme, name)
            if row is None:
                db.session.add(PlayerTheme(name=name, theme=theme))
            else:
                row.theme = theme
            db.session.commit()
            flash(f"Theme set to {THEMES[theme]}.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html")


@app.route("/progress")
def progress():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    games = Game.query.filter_by(name=name).order_by(Game.created.asc(), Game.id.asc()).all()
    wins = sum(1 for g in games if g.won)
    bests = [(lvl, db.session.query(func.min(Score.attempts)).filter_by(name=name, level=lvl).scalar())
             for lvl in LEVELS]
    start = max(1, len(games) - 19)
    return render_template("progress.html", total=len(games), wins=wins,
                           rate=round(wins * 100 / len(games)) if games else 0,
                           bests=bests, recent=list(enumerate(games[-20:], start=start)))


@app.route("/notifications")
def notifications():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    rows = ChallengeNotice.query.filter_by(recipient=name).order_by(ChallengeNotice.id.desc()).limit(50).all()
    items = [{"text": n.text, "created": n.created, "seen": n.seen,
              "url": f"/challenge/{n.challenge_id}" if n.kind in ("started", "completed") else "/challenges"}
             for n in rows]
    ChallengeNotice.query.filter_by(recipient=name, seen=False).update({"seen": True}, synchronize_session=False)
    db.session.commit()
    return render_template("notifications.html", notices=items)


# ---------------------------------------------------------------- challenges
def online_players(name):
    cutoff = utcnow() - timedelta(seconds=ONLINE_SECONDS)
    return (PlayerPresence.query.filter(PlayerPresence.last_seen >= cutoff, PlayerPresence.name != name)
            .order_by(PlayerPresence.name).limit(20).all())


def challenge_items(name):
    rows = (Challenge.query.filter((Challenge.creator == name) | (Challenge.opponent == name))
            .order_by(Challenge.id.desc()).limit(50).all())
    for c in rows:
        if c.status in ("pending", "accepted"):
            settle(c)
    items = []
    for c in rows:
        other = c.creator if c.opponent == name else c.opponent
        result = ""
        if c.status == "completed":
            w = c.creator if c.creator_won else c.opponent if c.opponent_won else None
            result = "🤝 Draw" if w is None else ("🏆 You won" if w == name else f"💀 {pname(w)} won")
        items.append({"c": c, "incoming": c.opponent == name, "other": other, "presence": presence_of(other),
                      "result": result, "level": c.level.title(), "limit": CHALLENGE_SECONDS[c.level]})
    return items


@app.route("/challenges", methods=["GET", "POST"])
def challenges():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))

    if request.method == "POST":
        action = request.form.get("action", "send")
        if action in {"accept", "decline"}:
            try:
                cid = int(request.form.get("challenge_id", ""))
            except ValueError:
                cid = 0
            ch = db.session.get(Challenge, cid)
            if ch:
                settle(ch)
                ch = db.session.get(Challenge, cid)
            if not ch:
                flash("Challenge not found.", "error")
            elif ch.opponent != name:
                flash("Only the invited player can respond to this challenge.", "error")
            elif ch.status != "pending":
                flash("This challenge is no longer open.", "error")
            else:
                new_status = "accepted" if action == "accept" else "declined"
                n = (Challenge.query.filter_by(id=ch.id, status="pending")
                     .update({"status": new_status}, synchronize_session=False))
                if n:
                    notify_player(ch.creator, ch.id, new_status, f"{name} {new_status} your challenge.")
                db.session.commit()
                if n and action == "accept":
                    ch = db.session.get(Challenge, cid)
                    ensure_match(ch)
                    flash(f"You accepted {ch.creator}'s challenge! Open the match to get ready.", "success")
                elif n:
                    flash("Challenge declined.", "success")
        else:
            opponent = request.form.get("opponent", "").strip()
            level = request.form.get("level", "medium")
            target = (Player.query.filter(func.lower(Player.name) == opponent.lower()).first()
                      if opponent else None)
            if not target:
                flash("That player account was not found.", "error")
            elif target.name == name:
                flash("You cannot challenge yourself.", "error")
            elif level not in LEVELS:
                flash("Choose a valid difficulty.", "error")
            elif level == "impossible" and not impossible_unlocked(name):
                flash(f"🔒 Win {UNLOCK_WINS} games to unlock Impossible duels.", "error")
            elif level == "impossible" and not impossible_unlocked(target.name):
                flash(f"{target.name} hasn't unlocked Impossible yet.", "error")
            elif Challenge.query.filter(Challenge.status == "pending",
                                        ((Challenge.creator == name) & (Challenge.opponent == target.name)) |
                                        ((Challenge.creator == target.name) & (Challenge.opponent == name))).first():
                flash(f"There's already a pending challenge between you and {target.name}.", "error")
            else:
                ch = Challenge(creator=name, opponent=target.name, level=level, status="pending")
                db.session.add(ch)
                db.session.flush()
                notify_player(target.name, ch.id, "incoming", f"⚔️ {name} challenged you ({level.title()}).")
                db.session.commit()
                flash(f"Challenge sent to {target.name}!", "success")
        return redirect(url_for("challenges"))

    return render_template("challenges.html", items=challenge_items(name), online=online_players(name),
                           levels=LEVELS, clocks=CHALLENGE_SECONDS, invite_hours=INVITE_HOURS,
                           imp_unlocked=impossible_unlocked(name))


@app.route("/challenges/fragment")
def challenges_fragment():
    name = session.get("player")
    if not name:
        return "", 401
    touch_presence(name, "online")
    db.session.commit()
    return render_template("challenge_list.html", items=challenge_items(name), online=online_players(name))


def build_match_state(ch, name):
    """Everything the match page needs. Secret is only revealed once the match is over."""
    settle(ch)
    ch = db.session.get(Challenge, ch.id)
    if ch.status not in ("accepted", "completed"):
        return {"phase": "closed", "status": ch.status}

    match, states, clock = ensure_match(ch)
    other_name = ch.opponent if name == ch.creator else ch.creator
    mine, other = states[name], states[other_name]
    hi, max_tries = LEVELS[ch.level]
    now = utcnow()

    if ch.status == "completed":
        phase = "done"
    elif clock.started_at is not None:
        phase = "live"
    else:
        phase = "lobby"

    remaining = None
    if phase == "live":
        deadline = naive(clock.started_at) + timedelta(seconds=clock.limit_seconds)
        remaining = max(0.0, (deadline - now).total_seconds())

    guesses = []
    for i, g in enumerate([int(x) for x in mine.guesses.split(",") if x], start=1):
        text, _kind = hint_for(g, match.secret, ch.level)
        guesses.append({"n": i, "guess": g, "hint": text})

    result, banner, bkind = None, "", "info"
    if phase == "done":
        winner = ch.creator if ch.creator_won else ch.opponent if ch.opponent_won else None
        reward = ChallengeReward.query.filter_by(challenge_id=ch.id, name=name).first()
        text = "🤝 Draw!" if winner is None else (f"🏆 {pname(winner)} wins!")
        result = {"text": text, "secret": match.secret, "winner": winner, "xp": reward.amount if reward else 0}
        banner = text
        bkind = "win" if winner == name else ("lose" if winner else "info")
    elif phase == "lobby":
        banner = (f"You're ready. Waiting for {other_name}…" if mine.ready
                  else "Hit ready. The clock starts when both players are ready.")
    else:
        if mine.finished:
            if mine.solved:
                banner, bkind = f"Solved in {mine.attempts}! Waiting for {other_name} to finish…", "success"
            elif mine.attempts >= max_tries:
                banner, bkind = f"Out of tries. Waiting for {other_name} to finish…", "error"
            else:
                banner, bkind = f"⏰ Time's up! Waiting for the match to wrap up…", "error"
        else:
            banner = "Clock's running. Fewest tries wins."

    msgs = (ChallengeMessage.query.filter_by(challenge_id=ch.id)
            .order_by(ChallengeMessage.id.desc()).limit(30).all())
    msgs.reverse()
    clues = []
    if ch.level == "impossible" and phase in ("live", "done"):
        every = impossible_clues(match.secret, f"duel{ch.id}")
        clues = every if phase == "done" else clues_shown(every, mine.attempts)
    return {
        "level": ch.level, "you": name, "clues": clues,
        "phase": phase, "status": ch.status, "banner": banner, "banner_kind": bkind,
        "remaining": remaining, "limit": clock.limit_seconds,
        "me": {"ready": mine.ready, "attempts": mine.attempts, "finished": mine.finished,
               "solved": mine.solved, "guesses": guesses},
        "opp": {"name": other_name, "ready": other.ready, "attempts": other.attempts,
                "finished": other.finished, "presence": presence_of(other_name)["state"],
                "last_seen": presence_of(other_name)["last_seen"]},
        "result": result,
        "messages": [{"id": m.id, "sender": m.sender, "body": m.body, "created": iso(m.created),
                      "mine": m.sender == name} for m in msgs],
    }


@app.route("/challenge/<int:challenge_id>")
def challenge_match(challenge_id):
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    ch = db.session.get(Challenge, challenge_id)
    if not ch or name not in {ch.creator, ch.opponent}:
        return "Challenge not found.", 404
    settle(ch)
    ch = db.session.get(Challenge, challenge_id)
    if ch.status not in ("accepted", "completed"):
        flash(f"That challenge is {ch.status}.", "info")
        return redirect(url_for("challenges"))
    ensure_match(ch)
    hi, max_tries = LEVELS[ch.level]
    other = ch.opponent if name == ch.creator else ch.creator
    return render_template("match.html", c=ch, other=other, hi=hi, max_tries=max_tries,
                           limit=CHALLENGE_SECONDS[ch.level])


@app.route("/api/challenge/<int:challenge_id>/state")
def api_challenge_state(challenge_id):
    name = session.get("player")
    if not name:
        return jsonify(error="Login required"), 401
    ch = db.session.get(Challenge, challenge_id)
    if not ch or name not in {ch.creator, ch.opponent}:
        return jsonify(error="Challenge not found"), 404
    touch_presence(name, "in-game")
    db.session.commit()
    return jsonify(build_match_state(ch, name))


@app.route("/api/challenge/<int:challenge_id>/action", methods=["POST"])
def api_challenge_action(challenge_id):
    name = session.get("player")
    if not name:
        return jsonify(ok=False, message="Log in first."), 401
    ch = db.session.get(Challenge, challenge_id)
    if not ch or name not in {ch.creator, ch.opponent}:
        return jsonify(ok=False, message="Challenge not found."), 404

    data = request.get_json(silent=True) or request.form
    action = data.get("action", "")
    settle(ch)
    ch = db.session.get(Challenge, challenge_id)
    if ch.status not in ("accepted", "completed"):
        return jsonify(ok=False, message="This match isn't active anymore.")
    match, states, clock = ensure_match(ch)
    mine = states[name]
    other_name = ch.opponent if name == ch.creator else ch.creator
    hi, max_tries = LEVELS[ch.level]

    if action == "chat":
        body = str(data.get("message", "")).strip()
        if not body:
            return jsonify(ok=False, message="Type a message first.")
        if len(body) > 300:
            return jsonify(ok=False, message="Messages must be 300 characters or fewer.")
        db.session.add(ChallengeMessage(challenge_id=ch.id, sender=name, body=body))
        db.session.commit()
        return jsonify(ok=True, message="Sent.")

    if ch.status == "completed":
        return jsonify(ok=False, message="This match has already ended.")

    if action == "ready":
        if mine.ready:
            return jsonify(ok=True, message="You're already ready.")
        mine.ready = True
        db.session.commit()
        ready_count = ChallengePlayerState.query.filter_by(challenge_id=ch.id, ready=True).count()
        if ready_count >= 2:
            started = (ChallengeClock.query.filter_by(challenge_id=ch.id, started_at=None)
                       .update({"started_at": utcnow()}, synchronize_session=False))
            if started:
                for who, other in ((ch.creator, ch.opponent), (ch.opponent, ch.creator)):
                    notify_player(who, ch.id, "started", f"▶️ Your duel with {other} just started. Clock is running!")
            db.session.commit()
            return jsonify(ok=True, message="Both players ready. Clock started! ⏱️")
        return jsonify(ok=True, message=f"You're ready. Waiting for {other_name}.")

    if action == "guess":
        if clock.started_at is None:
            return jsonify(ok=False, message="The match starts only after both players are ready.")
        if mine.finished:
            return jsonify(ok=False, message="You're finished. Chat while your opponent wraps up.")
        if mine.attempts >= max_tries:
            return jsonify(ok=False, message="No attempts left.")
        try:
            guess = int(data.get("guess", ""))
        except (TypeError, ValueError):
            guess = None
        if guess is None or not 1 <= guess <= hi:
            return jsonify(ok=False, message=f"Enter a number from 1 to {hi}.")

        mine.attempts += 1
        previous = [x for x in mine.guesses.split(",") if x]
        previous.append(str(guess))
        mine.guesses = ",".join(previous)
        text, kind = hint_for(guess, match.secret, ch.level)
        if guess == match.secret:
            mine.solved = True
            mine.finished = True
            text = "Correct! You solved it 🎯 The winner is decided once both of you finish."
        elif mine.attempts >= max_tries:
            mine.finished = True
            text, kind = "No attempts left. Chat while your opponent finishes.", "error"
        db.session.commit()
        finalize_if_done(ch, states, match)
        return jsonify(ok=True, message=text, kind="success" if kind == "win" else kind if kind == "error" else "info")

    return jsonify(ok=False, message="Unknown action.")


# ---------------------------------------------------------------- live pulse (presence + notifications)
@app.route("/api/pulse")
def api_pulse():
    name = session.get("player")
    if not name:
        return jsonify(error="Login required"), 401
    activity = request.args.get("activity", "online")
    if activity not in {"online", "in-game"}:
        activity = "online"
    touch_presence(name, activity)
    db.session.commit()

    # Keep clocks honest even if nobody has the match page open.
    active = (Challenge.query.filter(Challenge.status.in_(("pending", "accepted")),
                                     (Challenge.creator == name) | (Challenge.opponent == name)).limit(20).all())
    for c in active:
        settle(c)

    q = ChallengeNotice.query.filter_by(recipient=name, seen=False)
    unseen = q.order_by(ChallengeNotice.id.desc()).limit(10).all()
    return jsonify(unread=q.count(), notices=[{
        "id": n.id, "text": n.text, "kind": n.kind, "created": iso(n.created),
        "url": f"/challenge/{n.challenge_id}" if n.kind in ("started", "completed") else "/challenges",
    } for n in unseen])


@app.route("/delete-account", methods=["GET", "POST"])
def delete_account():
    name = session.get("player")
    if not name:
        return redirect(url_for("home"))
    if request.method == "POST":
        player = Player.query.filter_by(name=name).first()
        now = utcnow()
        pin = request.form.get("pin", "")
        if not player:
            session.pop("player", None)
            return redirect(url_for("home"))
        if player.locked_until and player.locked_until > now:
            flash("Too many wrong tries. Wait a few minutes.", "error")
        elif not check_password_hash(player.pin_hash, pin):
            player.fails += 1
            if player.fails >= MAX_FAILS:
                player.fails = 0
                player.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            db.session.commit()
            flash("Wrong PIN.", "error")
        elif request.form.get("confirm", "").strip().upper() != "DELETE":
            flash("Type DELETE to confirm.", "error")
        else:
            try:
                sid = session.get("sid")
                erase_player(name)
                if sid:
                    SoloState.query.filter_by(sid=sid).delete(synchronize_session=False)
                db.session.commit()
            except Exception:
                db.session.rollback()
                flash("Something went wrong and nothing was deleted. Try again.", "error")
                return redirect(url_for("delete_account"))
            session.pop("player", None)
            session.pop("theme", None)
            flash("Your account was deleted. 👋", "success")
            return redirect(url_for("home"))
        return redirect(url_for("delete_account"))
    return render_template("delete_account.html")


# ---------------------------------------------------------------- admin
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
