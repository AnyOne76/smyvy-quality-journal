# -*- coding: utf-8 -*-
"""
Смывы — единый локальный сервер: приложение + вход/регистрация + роли + админ-панель + ИИ.

Безопасность (серверная, не в браузере):
  • Регистрация и вход; пароли — PBKDF2-SHA256 с солью.
  • Роли: pending (по умолчанию после регистрации, без прав), viewer, master, quality, admin.
  • Сессии — подписанная HMAC-cookie (HttpOnly, SameSite=Strict).
  • Аудит: регистрация, вход, смена ролей, блокировки.
  • Уведомление всем активным admin по email при новой заявке (SMTP, .smtp_config).
  • Первый зарегистрированный пользователь автоматически становится admin.

Данные журнала пока хранятся в браузере (роли ограничивают интерфейс/действия).
ИИ — прокси к DeepSeek (ключ в .ai_key). Всё в стандартной библиотеке Python.

Запуск:  python app_server.py   →  http://127.0.0.1:8000/smyvy.html
Слушает только localhost. Наружу — только за HTTPS-прокси.
"""
import os
import re
import json
import hmac
import base64
import hashlib
import secrets
import sqlite3
import smtplib
import ssl
import datetime
import urllib.request
import urllib.error
from datetime import timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("SMYVY_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMYVY_PORT", "8000"))
DB_PATH = os.path.join(BASE, "smyvy_app.db")
SECRET_PATH = os.path.join(BASE, ".secret")
SESSION_TTL = 12 * 3600

ROLES = ("pending", "viewer", "master", "quality", "admin")
ROLE_TITLES = {"pending": "Ожидает роли", "viewer": "Наблюдатель",
               "master": "Мастер", "quality": "Служба качества", "admin": "Администратор"}

# ---------- DeepSeek ----------
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
SYSTEM = {
    "chat": (
        "Ты — ассистент службы качества мясокомбината по микробиологическим смывам. Отвечай по-русски, кратко.\n"
        "ЖЁСТКИЕ ПРАВИЛА: 1) Источник — только блок «ТОЧНЫЕ ДАННЫЕ ЖУРНАЛА». 2) НЕ называй количеств и итогов, "
        "не считай; на вопрос-число отвечай «За точными числами смотрите вкладку „Статистика“» и дай качественный вывод "
        "(значение конкретной пробы, напр. «330 КОЕ», приводить можно). 3) Можно перечислять записи и давать рекомендации; "
        "ничего не добавляй сверх данных. 4) Если ответа нет — «в переданных данных этого нет».\n"
        "Формат: без таблиц, короткие абзацы, списки через дефис."
    ),
    "report": (
        "Ты — специалист службы качества. Составь деловой отчёт по смывам для руководства/аудита (ХАССП) по-русски: "
        "1) итог; 2) несоответствия и превышения; 3) проблемные точки; 4) рекомендации. Числа бери дословно из сводки, "
        "не выдумывай. Без таблиц — заголовки, абзацы, списки через дефис."
    ),
    "alert": (
        "Ты — специалист санитарии пищевого производства. По одному несоответствию дай короткую (2–4 предложения) "
        "рекомендацию: вероятная причина и что сделать. По-русски."
    ),
}
MAXTOK = {"chat": 1500, "report": 3500, "alert": 600}

# ---------- почта (уведомления администратору) ----------
SMTP_FROM = "smyvy.admin.mpz@gmail.com"
SMTP_FROM_NAME = "Смывы Мясницкий Ряд"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def smtp_config():
    """Настройки SMTP: переменные окружения или файл .smtp_config (в .gitignore)."""
    cfg = {
        "host": os.environ.get("SMYVY_SMTP_HOST", SMTP_HOST),
        "port": int(os.environ.get("SMYVY_SMTP_PORT", str(SMTP_PORT))),
        "user": os.environ.get("SMYVY_SMTP_USER", SMTP_FROM),
        "password": os.environ.get("SMYVY_SMTP_PASSWORD"),
        "from_email": os.environ.get("SMYVY_SMTP_FROM", SMTP_FROM),
        "from_name": os.environ.get("SMYVY_SMTP_FROM_NAME", SMTP_FROM_NAME),
    }
    p = os.path.join(BASE, ".smtp_config")
    if os.path.exists(p):
        try:
            file_cfg = json.loads(open(p, encoding="utf-8").read())
            for k in ("host", "user", "password", "from_email", "from_name"):
                if k in file_cfg and file_cfg[k]:
                    cfg[k] = file_cfg[k]
            if file_cfg.get("port"):
                cfg["port"] = int(file_cfg["port"])
        except Exception:
            pass
    return cfg


def smtp_ready():
    return bool((smtp_config().get("password") or "").strip())


def admin_emails():
    """Email всех активных администраторов (список обновляется при каждой отправке)."""
    con = db()
    rows = con.execute(
        "SELECT DISTINCT login FROM users WHERE role='admin' AND active=1 ORDER BY login"
    ).fetchall()
    con.close()
    return [r["login"].strip().lower() for r in rows if r["login"]]


def _mail_when():
    return datetime.datetime.now().strftime("%d.%m.%Y, %H:%M")


def _send_mail(to_addrs, subject, body):
    cfg = smtp_config()
    pw = (cfg.get("password") or "").strip()
    if not pw or not to_addrs:
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = ", ".join(to_addrs)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as s:
        s.login(cfg["user"], pw)
        s.sendmail(cfg["from_email"], to_addrs, msg.as_string())
    return True


def notify_new_registration(login, full_name):
    """Письмо всем администраторам о новой заявке на доступ."""
    to = admin_emails()
    if not to:
        audit("system", "mail_skip", "no admin recipients")
        print("Почта: пропуск — нет активных администраторов")
        return
    subject = "[Смывы] Новая заявка на доступ — %s" % full_name
    body = (
        "Здравствуйте!\n\n"
        "В системе учёта микробиологических смывов «Смывы» (Мясницкий Ряд) "
        "поступила новая заявка на доступ.\n\n"
        "Заявитель:\n"
        "  • ФИО: %s\n"
        "  • Email: %s\n"
        "  • Дата заявки: %s\n\n"
        "Статус: ожидает назначения роли администратором.\n"
        "До назначения роли пользователь не сможет работать с журналом.\n\n"
        "Что сделать:\n"
        "  1. Войдите в приложение «Смывы».\n"
        "  2. Откройте вкладку «Админ» → «Пользователи».\n"
        "  3. Найдите заявку (статус «Ожидает роли») и назначьте роль:\n"
        "     Наблюдатель, Мастер, Служба качества или Администратор.\n\n"
        "—\n"
        "Автоматическое уведомление. Ответ на это письмо не требуется.\n"
        "Смывы · Мясницкий Ряд"
    ) % (full_name, login, _mail_when())
    try:
        if _send_mail(to, subject, body):
            audit("system", "mail_sent", "new_registration %s -> %s" % (login, ",".join(to)))
            print("Почта: отправлено уведомление о %s -> %s" % (login, ", ".join(to)))
        else:
            audit("system", "mail_skip", "smtp not configured")
            print("Почта: пропуск — SMTP не настроен (.smtp_config)")
    except Exception as e:
        audit("system", "mail_fail", "%s: %s" % (login, str(e)[:200]))
        print("Почта: ошибка отправки для %s: %s" % (login, e))


# ---------- секрет / БД ----------
def get_secret():
    if os.path.exists(SECRET_PATH):
        return open(SECRET_PATH, "rb").read()
    s = secrets.token_bytes(32)
    open(SECRET_PATH, "wb").write(s)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except Exception:
        pass
    return s


SECRET = get_secret()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_schema():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, pass_hash TEXT NOT NULL,
      role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user TEXT, action TEXT NOT NULL, detail TEXT);
    """)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    if "full_name" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
    con.execute("UPDATE users SET full_name=login WHERE full_name IS NULL OR trim(full_name)=''")
    con.commit()
    con.close()


def now():
    return datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(user, action, detail=""):
    con = db()
    con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)", (now(), user, action, detail))
    con.commit()
    con.close()


# ---------- пароли / сессии ----------
def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240000)
    return "pbkdf2$240000$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw, stored):
    try:
        _, it, s, d = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(s), int(it))
        return hmac.compare_digest(dk, base64.b64decode(d))
    except Exception:
        return False


def make_session(uid):
    exp = int(datetime.datetime.now(timezone.utc).timestamp()) + SESSION_TTL
    body = "%d.%d" % (uid, exp)
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(("%s.%s" % (body, sig)).encode()).decode()


def read_session(tok):
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        uid, exp, sig = raw.split(".")
        good = hmac.new(SECRET, ("%s.%s" % (uid, exp)).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp) < int(datetime.datetime.now(timezone.utc).timestamp()):
            return None
        return int(uid)
    except Exception:
        return None


def ai_key():
    k = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("KIE_API_KEY")
    if k:
        return k.strip()
    p = os.path.join(BASE, ".ai_key")
    if not os.path.exists(p):
        return None
    # Берём первую непустую строку без комментариев
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def ask_ai(task, question, context):
    system = SYSTEM.get(task, SYSTEM["chat"])
    if task == "chat":
        user = "Данные журнала (сводка):\n%s\n\nВопрос: %s" % (context, question)
    elif task == "report":
        user = "Сводка по журналу смывов:\n%s\n\nСоставь отчёт." % context
    else:
        user = "Несоответствие:\n%s\n\nДай рекомендацию." % context
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAXTOK.get(task, 1500),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + ai_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode("utf-8"))
    choices = d.get("choices") or []
    if not choices:
        raise RuntimeError("пустой ответ DeepSeek")
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip()


# ---------- HTTP ----------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def seed_admin():
    """Создать администратора по умолчанию из .admin_seed (email + хеш пароля), если его ещё нет.
    Пароль в открытом виде нигде не хранится — только хеш; файл в .gitignore."""
    p = os.path.join(BASE, ".admin_seed")
    if not os.path.exists(p):
        return
    try:
        seed = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return
    email = (seed.get("email") or "").strip()
    ph = seed.get("pass_hash")
    if not email or not ph:
        return
    full_name = re.sub(r"\s+", " ", (seed.get("full_name") or email).strip())
    con = db()
    if not con.execute("SELECT 1 FROM users WHERE login=?", (email,)).fetchone():
        con.execute("INSERT INTO users(login,pass_hash,full_name,role,active,created_at) VALUES(?,?,?,?,1,?)",
                    (email, ph, full_name, "admin", now()))
        con.commit()
        audit("system", "seed_admin", email)
    con.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=BASE, **k)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def _user(self):
        m = re.search(r"(?:^|;\s*)sid=([^;]+)", self.headers.get("Cookie", "") or "")
        if not m:
            return None
        uid = read_session(m.group(1))
        if uid is None:
            return None
        con = db()
        row = con.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        con.close()
        return row

    # --- GET: статика ---
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/me":
            u = self._user()
            return self._json({"login": u["login"], "role": u["role"], "full_name": u["full_name"] or u["login"]} if u else {"login": None})
        if p == "/api/users":
            u = self._user()
            if not u or u["role"] != "admin":
                return self._err("нужны права администратора", 403)
            con = db()
            rows = [{"id": r["id"], "login": r["login"], "full_name": r["full_name"] or r["login"], "role": r["role"], "active": r["active"], "created_at": r["created_at"]}
                    for r in con.execute("SELECT * FROM users ORDER BY id")]
            con.close()
            return self._json({"users": rows, "roles": [{"k": k, "t": ROLE_TITLES[k]} for k in ROLES]})
        return super().do_GET()

    # --- POST ---
    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/register":
            return self._register()
        if p == "/api/login":
            return self._login()
        if p == "/api/logout":
            u = self._user()
            if u:
                audit(u["login"], "logout")
            return self._json({"ok": True}, cookie="sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        if p == "/api/users/set-role":
            return self._admin_user("role")
        if p == "/api/users/set-name":
            return self._admin_user("name")
        if p == "/api/users/set-active":
            return self._admin_user("active")
        if p == "/api/users/delete":
            return self._admin_user("delete")
        if p == "/api/users/reset-password":
            return self._admin_reset_pw()
        if p == "/api/change-password":
            return self._change_pw()
        if p == "/api/ai":
            return self._ai()
        return self._err("не найдено", 404)

    def _admin_reset_pw(self):
        u = self._user()
        if not u or u["role"] != "admin":
            return self._err("нужны права администратора", 403)
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        try:
            uid = int(b.get("id"))
        except Exception:
            return self._err("неверный id")
        con = db()
        target = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            con.close()
            return self._err("пользователь не найден", 404)
        temp = secrets.token_urlsafe(6)
        con.execute("UPDATE users SET pass_hash=? WHERE id=?", (hash_password(temp), uid))
        con.commit()
        con.close()
        audit(u["login"], "reset_password", target["login"])
        return self._json({"ok": True, "login": target["login"], "password": temp})

    def _change_pw(self):
        u = self._user()
        if not u:
            return self._err("нужен вход", 401)
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        if not verify_password(b.get("old") or "", u["pass_hash"]):
            return self._err("текущий пароль неверен", 403)
        new = b.get("new") or ""
        if len(new) < 5:
            return self._err("новый пароль не короче 5 символов", 422)
        con = db()
        con.execute("UPDATE users SET pass_hash=? WHERE id=?", (hash_password(new), u["id"]))
        con.commit()
        con.close()
        audit(u["login"], "change_password")
        return self._json({"ok": True})

    # --- обработчики ---
    def _register(self):
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        login = (b.get("login") or "").strip().lower()
        pw = b.get("password") or ""
        full_name = re.sub(r"\s+", " ", (b.get("full_name") or "").strip())
        if not EMAIL_RE.match(login) or len(login) > 100:
            return self._err("введите корректный email", 422)
        if len(pw) < 5:
            return self._err("пароль не короче 5 символов", 422)
        if len(full_name) < 5:
            return self._err("укажите ФИО", 422)
        con = db()
        if con.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            con.close()
            return self._err("такой логин уже занят", 409)
        first = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
        role = "admin" if first else "pending"
        con.execute("INSERT INTO users(login,pass_hash,full_name,role,active,created_at) VALUES(?,?,?,?,1,?)",
                    (login, hash_password(pw), full_name, role, now()))
        con.commit()
        con.close()
        audit(login, "register", "role=%s" % role)
        if role == "pending":
            notify_new_registration(login, full_name)
        msg = "Вы зарегистрированы как администратор." if first else "Заявка отправлена. Доступ откроется, когда администратор назначит роль."
        return self._json({"ok": True, "first_admin": first, "message": msg})

    def _login(self):
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        login = (b.get("login") or "").strip().lower()
        con = db()
        row = con.execute("SELECT * FROM users WHERE login=? AND active=1", (login,)).fetchone()
        con.close()
        if not row or not verify_password(b.get("password") or "", row["pass_hash"]):
            audit(login or "?", "login_fail")
            return self._err("неверный логин или пароль", 401)
        audit(login, "login_ok")
        cookie = "sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (make_session(row["id"]), SESSION_TTL)
        return self._json({"login": row["login"], "role": row["role"], "full_name": row["full_name"] or row["login"]}, cookie=cookie)

    def _admin_user(self, what):
        u = self._user()
        if not u or u["role"] != "admin":
            return self._err("нужны права администратора", 403)
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        try:
            uid = int(b.get("id"))
        except Exception:
            return self._err("неверный id")
        con = db()
        target = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            con.close()
            return self._err("пользователь не найден", 404)
        if what == "role":
            role = b.get("role")
            if role not in ROLES:
                con.close()
                return self._err("неизвестная роль")
            if target["id"] == u["id"] and role != "admin":
                con.close()
                return self._err("нельзя снять с себя роль администратора", 409)
            con.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
            detail = "%s -> %s" % (target["login"], role)
        elif what == "name":
            full_name = re.sub(r"\s+", " ", (b.get("full_name") or "").strip())
            if len(full_name) < 5:
                con.close()
                return self._err("укажите ФИО")
            con.execute("UPDATE users SET full_name=? WHERE id=?", (full_name, uid))
            detail = "%s full_name=%s" % (target["login"], full_name)
        elif what == "active":
            act = 1 if b.get("active") else 0
            if target["id"] == u["id"] and not act:
                con.close()
                return self._err("нельзя заблокировать самого себя", 409)
            con.execute("UPDATE users SET active=? WHERE id=?", (act, uid))
            detail = "%s active=%d" % (target["login"], act)
        else:  # delete
            if target["id"] == u["id"]:
                con.close()
                return self._err("нельзя удалить самого себя", 409)
            con.execute("DELETE FROM users WHERE id=?", (uid,))
            detail = "delete %s" % target["login"]
        con.commit()
        con.close()
        audit(u["login"], "admin_" + what, detail)
        return self._json({"ok": True})

    def _ai(self):
        u = self._user()
        if not u:
            return self._err("нужен вход", 401)
        if u["role"] == "pending":
            return self._err("роль ещё не назначена администратором", 403)
        if not ai_key():
            return self._err("Ключ ИИ не задан (файл .ai_key)", 503)
        b = self._body()
        if b is None:
            return self._err("неверный запрос")
        task = b.get("task", "chat")
        if task not in SYSTEM:
            return self._err("неизвестная задача")
        if task == "chat" and not (b.get("question") or "").strip():
            return self._err("пустой вопрос")
        try:
            return self._json({"answer": ask_ai(task, (b.get("question") or "").strip(), (b.get("context") or "").strip())})
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            return self._err("Ошибка ИИ (%d): %s" % (e.code, detail), 502)
        except Exception as e:
            return self._err("Ошибка ИИ: " + str(e)[:200], 502)


if __name__ == "__main__":
    import sys
    init_schema()
    # восстановление пароля с ПК:  python app_server.py setpass email@дом пароль
    if len(sys.argv) >= 2 and sys.argv[1] == "setpass":
        if len(sys.argv) < 4:
            print("Использование: python app_server.py setpass EMAIL НОВЫЙ_ПАРОЛЬ")
            sys.exit(1)
        email, newpw = sys.argv[2].strip().lower(), sys.argv[3]
        con = db()
        row = con.execute("SELECT id FROM users WHERE login=?", (email,)).fetchone()
        if not row:
            con.execute("INSERT INTO users(login,pass_hash,full_name,role,active,created_at) VALUES(?,?,?,?,1,?)",
                        (email, hash_password(newpw), email, "admin", now()))
            print("Создан админ:", email)
        else:
            con.execute("UPDATE users SET pass_hash=?, active=1 WHERE id=?", (hash_password(newpw), row["id"]))
            print("Пароль обновлён для:", email)
        con.commit()
        con.close()
        audit("cli", "setpass", email)
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "testmail":
        seed_admin()
        if not smtp_ready():
            print("SMTP не настроен. Создайте .smtp_config с паролем приложения Gmail.")
            sys.exit(1)
        to = admin_emails()
        if not to:
            print("Нет получателей: в базе нет активных администраторов (role=admin)")
            sys.exit(1)
        try:
            _send_mail(
                to,
                "[Смывы] Тест рассылки",
                "Проверка уведомлений «Смывы Мясницкий Ряд».\n\n"
                "Письмо отправлено всем активным администраторам:\n  • " + "\n  • ".join(to),
            )
            print("Тестовое письмо отправлено администраторам:", ", ".join(to))
        except Exception as e:
            print("Ошибка отправки:", e)
            sys.exit(1)
        sys.exit(0)
    seed_admin()
    con = db()
    n = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    con.close()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Смывы: http://%s:%d/smyvy.html" % (HOST, PORT))
    print("Пользователей в базе: %d%s" % (n, "  (первый зарегистрированный станет админом)" if n == 0 else ""))
    print("Ключ ИИ (DeepSeek): " + ("есть" if ai_key() else "нет — создайте файл .ai_key"))
    print("Почта (%s): %s" % (SMTP_FROM, "настроена" if smtp_ready() else "нет — создайте .smtp_config"))
    print("Остановить — Ctrl+C или закройте окно.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
