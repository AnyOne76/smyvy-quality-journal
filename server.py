# -*- coding: utf-8 -*-
"""
Смывы — серверная часть (production-заготовка).

Безопасность и надёжность заложены с самого начала:
  • Аутентификация: логин + пароль, хеширование PBKDF2-SHA256 с солью (stdlib).
  • Роли: viewer (только чтение), master (ввод), quality (ввод/правка/удаление), admin.
  • Сессии: подписанная HMAC-cookie (HttpOnly), срок жизни ограничен.
  • Аудит: каждое изменение и вход пишутся в таблицу audit (кто, что, когда).
  • Валидация ввода на сервере (клиентскую проверку обойти легко).
  • Хранилище: SQLite (по умолчанию) — один файл, легко бэкапить. Заменяется на PostgreSQL.
  • Зависимости: только стандартная библиотека (+ pandas/openpyxl лишь для разового импорта).

Запуск:
  python server.py init          # создать БД, импортировать историю из Excel, создать admin
  python server.py adduser       # добавить пользователя (логин, пароль, роль)
  python server.py run           # запустить сервер (по умолчанию 127.0.0.1:8000)

Перед публикацией на предприятии — см. README_SERVER.md (HTTPS, бэкапы, смена секрета).
"""
import os
import re
import json
import hmac
import base64
import hashlib
import secrets
import sqlite3
import getpass
import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- конфигурация ----------
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SMYVY_DB", os.path.join(BASE, "smyvy_prod.db"))
SECRET_PATH = os.path.join(BASE, ".secret")
HOST = os.environ.get("SMYVY_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMYVY_PORT", "8000"))
SESSION_TTL = 12 * 3600          # сколько живёт сессия (сек)
PBKDF2_ITERS = 240_000
FRONTEND = os.path.join(BASE, "smyvy.html")

INDICATORS = ["КМАФАнМ", "БГКП", "Proteus", "Salmonella", "Listeria", "Staph", "Плесень", "Дрожжи"]
ROLES = ("viewer", "master", "quality", "admin")
# какие роли имеют право на действие
CAN_WRITE = ("master", "quality", "admin")
CAN_EDIT = ("quality", "admin")


# ---------- секрет для подписи сессий ----------
def get_secret():
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "rb") as f:
            return f.read()
    s = secrets.token_bytes(32)
    with open(SECRET_PATH, "wb") as f:
        f.write(s)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except Exception:
        pass
    return s


SECRET = get_secret()


# ---------- БД ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_schema():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY,
      login TEXT UNIQUE NOT NULL,
      pass_hash TEXT NOT NULL,
      role TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS probes(
      id INTEGER PRIMARY KEY,
      ceh TEXT NOT NULL,
      date TEXT,
      point TEXT NOT NULL,
      status TEXT NOT NULL,     -- строка из символов . n x e o по показателям
      kma INTEGER,
      master TEXT,
      created_by TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT
    );
    CREATE TABLE IF NOT EXISTS audit(
      id INTEGER PRIMARY KEY,
      ts TEXT NOT NULL,
      user TEXT,
      action TEXT NOT NULL,
      detail TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_probes_ceh ON probes(ceh);
    CREATE INDEX IF NOT EXISTS ix_probes_date ON probes(date);
    """)
    con.commit()
    con.close()


# ---------- пароли ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ITERS, base64.b64encode(salt).decode(),
                               base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---------- сессии (подписанная cookie) ----------
def make_session(user_id: int) -> str:
    exp = int(datetime.datetime.now(timezone.utc).timestamp()) + SESSION_TTL
    body = "%d.%d" % (user_id, exp)
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(("%s.%s" % (body, sig)).encode()).decode()


def read_session(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, exp, sig = raw.split(".")
        body = "%s.%s" % (user_id, exp)
        good = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp) < int(datetime.datetime.now(timezone.utc).timestamp()):
            return None
        return int(user_id)
    except Exception:
        return None


def now():
    return datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(user, action, detail=""):
    con = db()
    con.execute("INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)",
                (now(), user, action, detail))
    con.commit()
    con.close()


# ---------- валидация ----------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_probe(p):
    if not isinstance(p, dict):
        return "неверный формат"
    if not p.get("ceh"):
        return "не указан цех"
    if not p.get("point"):
        return "не указана точка"
    s = p.get("status", "")
    if not isinstance(s, str) or len(s) != len(INDICATORS) or any(ch not in ".nxeo" for ch in s):
        return "неверная строка показателей"
    if p.get("date") and not DATE_RE.match(p["date"]):
        return "неверная дата (нужно ГГГГ-ММ-ДД)"
    if not re.search(r"[nxeo]", s):
        return "не отмечен ни один показатель"
    return None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "SmyvyServer/1.0"

    def log_message(self, *a):
        pass  # тихо; для продакшена подключите нормальное логирование

    # --- утилиты ответа ---
    def _json(self, obj, code=200, cookie=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
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
            return {}

    def _user(self):
        cookie = self.headers.get("Cookie", "") or ""
        m = re.search(r"(?:^|;\s*)sid=([^;]+)", cookie)
        if not m:
            return None
        uid = read_session(m.group(1))
        if uid is None:
            return None
        con = db()
        row = con.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        con.close()
        return row

    # --- маршрутизация ---
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html", "/smyvy.html"):
            return self._serve_frontend()
        if p == "/api/me":
            u = self._user()
            return self._json({"login": u["login"], "role": u["role"]} if u else {"login": None})
        if p == "/api/probes":
            if not self._user():
                return self._err("нужен вход", 401)
            con = db()
            rows = [dict(r) for r in con.execute(
                "SELECT ceh,date,point,status,kma,master FROM probes ORDER BY date DESC, id DESC")]
            con.close()
            return self._json({"probes": rows})
        return self._err("не найдено", 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/login":
            return self._login()
        if p == "/api/logout":
            u = self._user()
            if u:
                audit(u["login"], "logout")
            return self._json({"ok": True}, cookie="sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        if p == "/api/probes":
            return self._add_probe()
        return self._err("не найдено", 404)

    def do_PUT(self):
        m = re.match(r"^/api/probes/(\d+)$", self.path.split("?")[0])
        if m:
            return self._edit_probe(int(m.group(1)))
        return self._err("не найдено", 404)

    def do_DELETE(self):
        m = re.match(r"^/api/probes/(\d+)$", self.path.split("?")[0])
        if m:
            return self._del_probe(int(m.group(1)))
        return self._err("не найдено", 404)

    # --- обработчики ---
    def _serve_frontend(self):
        if not os.path.exists(FRONTEND):
            return self._err("нет файла smyvy.html", 404)
        with open(FRONTEND, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _login(self):
        b = self._body()
        login = (b.get("login") or "").strip()
        con = db()
        row = con.execute("SELECT * FROM users WHERE login=? AND active=1", (login,)).fetchone()
        con.close()
        if not row or not verify_password(b.get("password") or "", row["pass_hash"]):
            audit(login or "?", "login_fail")
            return self._err("неверный логин или пароль", 401)
        audit(login, "login_ok")
        cookie = "sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (make_session(row["id"]), SESSION_TTL)
        return self._json({"login": row["login"], "role": row["role"]}, cookie=cookie)

    def _add_probe(self):
        u = self._user()
        if not u:
            return self._err("нужен вход", 401)
        if u["role"] not in CAN_WRITE:
            return self._err("нет прав на ввод", 403)
        p = self._body()
        err = validate_probe(p)
        if err:
            return self._err(err, 422)
        con = db()
        cur = con.execute(
            "INSERT INTO probes(ceh,date,point,status,kma,master,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (p["ceh"], p.get("date"), p["point"], p["status"], p.get("kma"),
             (p.get("master") or "").strip(), u["login"], now()))
        con.commit()
        pid = cur.lastrowid
        con.close()
        audit(u["login"], "add", "id=%d ceh=%s point=%s" % (pid, p["ceh"], p["point"]))
        return self._json({"id": pid}, 201)

    def _edit_probe(self, pid):
        u = self._user()
        if not u:
            return self._err("нужен вход", 401)
        if u["role"] not in CAN_EDIT:
            return self._err("нет прав на правку", 403)
        p = self._body()
        err = validate_probe(p)
        if err:
            return self._err(err, 422)
        con = db()
        row = con.execute("SELECT id FROM probes WHERE id=?", (pid,)).fetchone()
        if not row:
            con.close()
            return self._err("запись не найдена", 404)
        con.execute("UPDATE probes SET ceh=?,date=?,point=?,status=?,kma=?,master=?,updated_at=? WHERE id=?",
                    (p["ceh"], p.get("date"), p["point"], p["status"], p.get("kma"),
                     (p.get("master") or "").strip(), now(), pid))
        con.commit()
        con.close()
        audit(u["login"], "edit", "id=%d" % pid)
        return self._json({"ok": True})

    def _del_probe(self, pid):
        u = self._user()
        if not u:
            return self._err("нужен вход", 401)
        if u["role"] not in CAN_EDIT:
            return self._err("нет прав на удаление", 403)
        con = db()
        con.execute("DELETE FROM probes WHERE id=?", (pid,))
        con.commit()
        con.close()
        audit(u["login"], "delete", "id=%d" % pid)
        return self._json({"ok": True})


# ---------- CLI ----------
def cmd_init():
    init_schema()
    # импорт истории из Excel (если есть)
    xlsx = os.path.join(BASE, "07. Результаты смывов 2026.xlsx")
    imported = 0
    if os.path.exists(xlsx):
        imported = import_excel(xlsx)
    # создать admin
    con = db()
    exists = con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
    con.close()
    if not exists:
        pw = secrets.token_urlsafe(9)
        create_user("admin", pw, "admin")
        print("Создан пользователь admin с временным паролем: %s" % pw)
        print("!!! Смените его после первого входа. !!!")
    print("Готово. Импортировано проб: %d. База: %s" % (imported, DB_PATH))


def import_excel(path):
    """Разовый импорт истории из Excel через parser.parse_workbook."""
    import pandas as pd
    from parser import parse_workbook, KMA_LIMIT
    f = parse_workbook(path)
    con = db()
    count = 0
    for (ceh, date, point), g in f.groupby(["цех", "дата", "точка"], sort=False):
        chars, kma = [], None
        for ind in INDICATORS:
            row = g[g["показатель"] == ind]
            if row.empty:
                chars.append(".")
                continue
            st = row.iloc[0]["статус"]
            val = str(row.iloc[0]["значение"])
            if ind == "КМАФАнМ":
                if st == "не тестировали":
                    chars.append(".")
                elif st == "превышение":
                    chars.append("o")
                    kma = int(row.iloc[0]["кмафанм_кое"] or 0)
                elif "менее" in val.lower():
                    chars.append("n")
                else:
                    chars.append("e")
                    kv = row.iloc[0]["кмафанм_кое"]
                    kma = int(kv) if pd.notna(kv) else None
            else:
                chars.append("." if st == "не тестировали" else ("x" if st == "несоответствие" else "n"))
        s = "".join(chars)
        if not re.search(r"[nxeo]", s):
            continue
        d = date.strftime("%Y-%m-%d") if pd.notna(date) else None
        con.execute("INSERT INTO probes(ceh,date,point,status,kma,master,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (ceh, d, point, s, kma, "", "import", now()))
        count += 1
    con.commit()
    con.close()
    return count


def create_user(login, password, role):
    if role not in ROLES:
        raise SystemExit("роль должна быть одной из: %s" % ", ".join(ROLES))
    con = db()
    con.execute("INSERT INTO users(login,pass_hash,role,active,created_at) VALUES(?,?,?,1,?)",
                (login, hash_password(password), role, now()))
    con.commit()
    con.close()
    audit("cli", "create_user", "login=%s role=%s" % (login, role))


def cmd_adduser():
    init_schema()
    login = input("Логин: ").strip()
    pw = getpass.getpass("Пароль: ")
    print("Роли: %s" % ", ".join(ROLES))
    role = input("Роль: ").strip()
    create_user(login, pw, role)
    print("Пользователь %s (%s) создан." % (login, role))


def cmd_run():
    init_schema()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Смывы-сервер: http://%s:%d  (Ctrl+C для остановки)" % (HOST, PORT))
    print("ВНИМАНИЕ: за пределами localhost поднимайте только за HTTPS-прокси. См. README_SERVER.md")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"init": cmd_init, "adduser": cmd_adduser, "run": cmd_run}.get(cmd, cmd_run)()
