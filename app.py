import os
import random
from flask import Flask, request, render_template_string, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

MAX_TRIES = 5

PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Guessing Game</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
      font-family: "Segoe UI", Arial, sans-serif; color: white;
      background: linear-gradient(135deg, #1e1b4b, #4c1d95, #be185d);
    }
    .card {
      width: 90%; max-width: 380px; padding: 32px; text-align: center;
      background: rgba(255,255,255,0.1);
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 20px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.3);
    }
    h1 { margin: 0 0 8px; }
    .sub { opacity: 0.8; margin: 0 0 24px; }
    input {
      width: 100%; padding: 14px; margin-bottom: 12px;
      font-size: 24px; text-align: center;
      border: none; border-radius: 12px; outline: none;
    }
    button, .btn {
      display: block; width: 100%; padding: 14px;
      font-size: 18px; font-weight: bold; text-decoration: none;
      background: #facc15; color: #1e1b4b;
      border: none; border-radius: 12px; cursor: pointer;
    }
    button:hover, .btn:hover { transform: scale(1.03); }
    .msg { min-height: 28px; margin: 20px 0 12px; font-size: 20px; font-weight: bold; }
    .low { color: #93c5fd; }
    .high { color: #fca5a5; }
    .win { color: #86efac; }
    .lose { color: #fca5a5; }
    .dots span {
      display: inline-block; width: 14px; height: 14px; margin: 0 4px;
      border-radius: 50%; background: rgba(255,255,255,0.25);
    }
    .dots span.used { background: #facc15; }
    .best { margin-top: 18px; opacity: 0.9; }
  </style>
</head>
<body>
  <div class="card">
    <h1>🎯 Guessing Game</h1>
    <p class="sub">I'm thinking of a number between 1 and 20.</p>

    {% if over %}
      <a class="btn" href="/">Play again 🔁</a>
    {% else %}
      <form method="post">
        <input type="number" name="guess" min="1" max="20" placeholder="?" required autofocus>
        <button type="submit">Guess</button>
      </form>
    {% endif %}

    <div class="msg {{ kind }}">{{ message }}</div>
    <div class="dots">
      {% for i in range(5) %}<span class="{{ 'used' if i < attempts else '' }}"></span>{% endfor %}
    </div>

    {% if best %}<div class="best">🏆 Best: {{ best }} attempts</div>{% endif %}
  </div>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def home():
    if "secret" not in session:
        session["secret"] = random.randint(1, 20)
        session["attempts"] = 0

    message, kind, over = "", "", False

    if request.method == "POST":
        guess = int(request.form["guess"])
        session["attempts"] += 1
        tries = session["attempts"]
        secret = session["secret"]

        if guess == secret:
            message = f"You got it in {tries} attempts! 🎉"
            kind, over = "win", True
            best = session.get("best")
            if best is None or tries < best:
                session["best"] = tries
                message += " New best!"
        elif tries >= MAX_TRIES:
            message = f"Out of attempts. It was {secret} 💀"
            kind, over = "lose", True
        elif guess < secret:
            message, kind = "Too low 📉", "low"
        else:
            message, kind = "Too high 📈", "high"

    attempts = session["attempts"]
    if over:
        session.pop("secret")
        session.pop("attempts")

    return render_template_string(
        PAGE, message=message, kind=kind, over=over,
        attempts=attempts, best=session.get("best"),
    )

if __name__ == "__main__":
    app.run(debug=True)