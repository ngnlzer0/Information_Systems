"""Сервер авторизації (Flask + SQLite + JWT).

Схема роботи:
  1. Користувач відкриває сайт через шлюз nginx. Для кожного запиту nginx робить
     підзапит на /verify (директива auth_request).
  2. Немає дійсного токена -> 401 -> nginx перенаправляє на /login.
  3. POST /login перевіряє пароль у SQLite, видає JWT у HttpOnly-cookie.
  4. /verify приймає токен і повертає заголовок X-Auth-User; nginx передає його
     в Grafana (auth proxy), і Grafana впускає користувача без власного логіну.

Публічні маршрути (через nginx): /login, /logout, /auth/api/login, /auth/api/users.
Внутрішні (лише в docker-мережі): /verify, /health, /metrics.
"""
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque

import jwt
from flask import Flask, Response, jsonify, make_response, redirect, render_template_string, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from common.obs import setup_logging, setup_tracing

SERVICE = "auth"
log = setup_logging(SERVICE)

DB_PATH = os.getenv("AUTH_DB_PATH", "/data/auth.db")
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_TTL = int(os.getenv("JWT_TTL_SECONDS", "3600"))
COOKIE_NAME = "is_token"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]

MAX_FAILED_ATTEMPTS = 5   # спроб входу
FAILED_WINDOW = 60        # за скільки секунд
MIN_PASSWORD_LEN = 8

LOGINS = Counter("auth_login_attempts", "Спроби входу", ["result"])
VERIFIES = Counter("auth_verify_requests", "Перевірки токена шлюзом", ["result"])

app = Flask(__name__)

provider = setup_tracing(SERVICE)
if provider:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    # /verify викликається на кожен запит до Grafana - у трейси його не пишемо
    FlaskInstrumentor().instrument_app(app, excluded_urls="verify,health,metrics")

# ------------------------------------------------------------------ паролі
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hash_password(password, bytes.fromhex(salt_hex)).split("$")[2]
    except ValueError:
        return False
    return hmac.compare_digest(candidate, digest_hex)


# Для неіснуючого користувача теж рахуємо хеш, щоб час відповіді не видавав, чи є такий логін
DUMMY_HASH = hash_password(secrets.token_hex(8))

# ------------------------------------------------------------------ SQLite
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   username      TEXT NOT NULL UNIQUE,
                   password_hash TEXT NOT NULL,
                   role          TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'viewer')),
                   created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD)),
            )
            log.info("Створено початкового адміністратора", extra={"username": ADMIN_USERNAME})


def get_user(username):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


init_db()

# --------------------------------------------------------- захист від підбору
_failed = defaultdict(deque)


def client_ip():
    return request.headers.get("X-Real-IP", request.remote_addr or "unknown")


def is_blocked(ip):
    attempts = _failed[ip]
    cutoff = time.monotonic() - FAILED_WINDOW
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= MAX_FAILED_ATTEMPTS


def register_failure(ip):
    _failed[ip].append(time.monotonic())


# --------------------------------------------------------------------- JWT
def issue_token(user):
    now = int(time.time())
    claims = {"sub": user["username"], "role": user["role"], "iat": now, "exp": now + JWT_TTL}
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def claims_from_request():
    """Повертає claims дійсного токена (cookie або Authorization: Bearer) або None."""
    token = request.cookies.get(COOKIE_NAME)
    header = request.headers.get("Authorization", "")
    if not token and header.startswith("Bearer "):
        token = header[7:]
    if not token:
        return None
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
    user = get_user(claims["sub"])  # видалений користувач одразу втрачає доступ
    if user is None:
        return None
    claims["role"] = user["role"]
    return claims


def safe_next(target):
    """Захист від open redirect: дозволені лише внутрішні шляхи."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if target.startswith("/login"):
        return "/"
    return target


# -------------------------------------------------------------- HTML-форма
LOGIN_PAGE = """<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вхід до системи моніторингу</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #111827; color: #e5e7eb;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
    form { background: #1f2937; padding: 2rem; border-radius: 12px; width: 320px;
           box-shadow: 0 10px 30px rgba(0,0,0,.4); }
    h1 { font-size: 1.25rem; margin: 0 0 1.25rem; }
    label { display: block; font-size: .85rem; margin: .75rem 0 .25rem; color: #9ca3af; }
    input { width: 100%; box-sizing: border-box; padding: .6rem; border-radius: 8px;
            border: 1px solid #374151; background: #111827; color: inherit; }
    button { width: 100%; margin-top: 1.25rem; padding: .7rem; border: 0; border-radius: 8px;
             background: #3b82f6; color: white; font-weight: 600; cursor: pointer; }
    .error { background: #7f1d1d; color: #fecaca; padding: .6rem; border-radius: 8px; font-size: .85rem; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Вхід до системи моніторингу БД</h1>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <input type="hidden" name="next" value="{{ next }}">
    <label for="username">Логін</label>
    <input id="username" name="username" autocomplete="username" required autofocus>
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Увійти</button>
  </form>
</body>
</html>"""


def render_login(error=None, status=200):
    page = render_template_string(LOGIN_PAGE, error=error, next=safe_next(request.values.get("next")))
    return Response(page, status=status, mimetype="text/html")


# ---------------------------------------------------------------- маршрути
def authenticate(username, password):
    """Повертає (user | None, помилка | None). Єдине місце перевірки пароля."""
    ip = client_ip()
    if is_blocked(ip):
        LOGINS.labels("blocked").inc()
        log.warning("Забагато невдалих спроб входу", extra={"client_ip": ip})
        return None, "Забагато невдалих спроб. Спробуйте за хвилину."
    user = get_user(username)
    ok = verify_password(password, user["password_hash"] if user else DUMMY_HASH)
    if not (user and ok):
        register_failure(ip)
        LOGINS.labels("failure").inc()
        log.warning("Невдала спроба входу", extra={"username": username, "client_ip": ip})
        return None, "Невірний логін або пароль."
    LOGINS.labels("success").inc()
    log.info("Успішний вхід", extra={"username": username, "client_ip": ip})
    return user, None


@app.get("/login")
def login_form():
    return render_login()


@app.post("/login")
def login_submit():
    user, error = authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
    if error:
        return render_login(error, status=429 if "Забагато" in error else 401)
    response = make_response(redirect(safe_next(request.form.get("next"))))
    response.set_cookie(
        COOKIE_NAME, issue_token(user), max_age=JWT_TTL, httponly=True, samesite="Lax", secure=COOKIE_SECURE
    )
    return response


@app.get("/logout")
def logout():
    response = make_response(redirect("/login"))
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/verify")
def verify():
    """Підзапит nginx (auth_request): 200 + X-Auth-User або 401."""
    claims = claims_from_request()
    if claims is None:
        VERIFIES.labels("denied").inc()
        return Response(status=401)
    VERIFIES.labels("allowed").inc()
    response = Response(status=200)
    response.headers["X-Auth-User"] = claims["sub"]
    response.headers["X-Auth-Role"] = claims["role"]
    return response


@app.post("/api/login")
def api_login():
    """Отримання токена для скриптів: curl -d '{"username":..,"password":..}'."""
    data = request.get_json(silent=True) or {}
    user, error = authenticate(str(data.get("username", "")), str(data.get("password", "")))
    if error:
        return jsonify(error=error), 429 if "Забагато" in error else 401
    return jsonify(access_token=issue_token(user), token_type="Bearer", expires_in=JWT_TTL)


@app.post("/api/users")
def api_create_user():
    """Створення користувача. Дозволено лише адміністратору."""
    claims = claims_from_request()
    if claims is None:
        return jsonify(error="Потрібна авторизація."), 401
    if claims["role"] != "admin":
        return jsonify(error="Недостатньо прав."), 403

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = data.get("role", "viewer")
    if not username or len(password) < MIN_PASSWORD_LEN or role not in ("admin", "viewer"):
        return jsonify(error=f"Потрібні username, password (мінімум {MIN_PASSWORD_LEN} символів) і role: admin|viewer."), 400
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hash_password(password), role),
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Такий користувач уже існує."), 409
    log.info("Створено користувача", extra={"username": username, "role": role, "created_by": claims["sub"]})
    return jsonify(username=username, role=role), 201


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
