# -*- coding: utf-8 -*-
"""
Смывы — единый локальный сервер: приложение + вход/регистрация + роли + админ-панель + ИИ.

Безопасность (серверная, не в браузере):
  • Регистрация и вход; пароли — PBKDF2-SHA256 с солью.
  • Роли: pending (по умолчанию после регистрации, без прав), viewer, quality, admin.
  • Сессии — подписанная HMAC-cookie (HttpOnly, SameSite=Strict).
  • Аудит: регистрация, вход, смена ролей, блокировки.
  • Уведомление всем активным admin по email при новой заявке (SMTP, .smtp_config).
  • Первый зарегистрированный пользователь автоматически становится admin.

Журнал смывов хранится в SQLite (роли ограничивают интерфейс/действия).
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
import time
import threading
import urllib.request
import urllib.error
from datetime import timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.6.0"
HOST = os.environ.get("SMYVY_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMYVY_PORT", "8000"))
DB_PATH = os.path.join(BASE, "smyvy_app.db")
BACKUP_DIR = os.path.join(BASE, "backups")
BACKUP_DIR_FILE = os.path.join(BASE, ".backup_dir")
SHARED_JOURNAL = os.path.join(BACKUP_DIR, "smyvy-journal.json")
JOURNAL_LOCK = threading.Lock()
JOURNAL_CACHE = {"rev": 0, "payload": None}
BACKUP_HOUR = 20
SECRET_PATH = os.path.join(BASE, ".secret")
SESSION_TTL = 12 * 3600

ROLES = ("pending", "viewer", "quality", "admin")
ROLE_TITLES = {"pending": "Ожидает роли", "viewer": "Наблюдатель",
               "quality": "Служба качества", "admin": "Администратор"}

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
# Корпоративный SMTP-релей (как validationOnTT / documents_conveyer / hr_passport):
# 10.0.4.70:25 без логина, TLS выкл.
SMTP_FROM = "smyvy@fmmr.market"
SMTP_FROM_NAME = "Смывы Мясницкий Ряд"
SMTP_HOST = "10.0.4.70"
SMTP_PORT = 25
SMTP_USE_TLS = False


def smtp_config():
    """Настройки SMTP: переменные окружения или файл .smtp_config (в .gitignore)."""
    cfg = {
        "host": os.environ.get("SMYVY_SMTP_HOST", SMTP_HOST),
        "port": int(os.environ.get("SMYVY_SMTP_PORT", str(SMTP_PORT))),
        "user": os.environ.get("SMYVY_SMTP_USER", ""),
        "password": os.environ.get("SMYVY_SMTP_PASSWORD", ""),
        "from_email": os.environ.get("SMYVY_SMTP_FROM", SMTP_FROM),
        "from_name": os.environ.get("SMYVY_SMTP_FROM_NAME", SMTP_FROM_NAME),
        "use_tls": os.environ.get("SMYVY_SMTP_USE_TLS", str(SMTP_USE_TLS)).lower()
                   in ("1", "true", "yes"),
    }
    p = os.path.join(BASE, ".smtp_config")
    if os.path.exists(p):
        try:
            file_cfg = json.loads(open(p, encoding="utf-8").read())
            for k in ("host", "from_email", "from_name"):
                if k in file_cfg and file_cfg[k]:
                    cfg[k] = file_cfg[k]
            # user/password допускают пустую строку (релей без AUTH)
            for k in ("user", "password"):
                if k in file_cfg and file_cfg[k] is not None:
                    cfg[k] = file_cfg[k]
            if file_cfg.get("port"):
                cfg["port"] = int(file_cfg["port"])
            if "use_tls" in file_cfg:
                cfg["use_tls"] = bool(file_cfg["use_tls"])
        except Exception:
            pass
    return cfg


def smtp_ready():
    """Готовность: задан host. Пароль не обязателен (корпоративный релей без AUTH)."""
    return bool((smtp_config().get("host") or "").strip())


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
    host = (cfg.get("host") or "").strip()
    if not host or not to_addrs:
        return False
    user = (cfg.get("user") or "").strip()
    pw = (cfg.get("password") or "").strip()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = ", ".join(to_addrs)
    port = int(cfg["port"])
    use_tls = bool(cfg.get("use_tls"))
    # Порт 465 + TLS → SMTP_SSL; иначе обычный SMTP (релей :25) или STARTTLS
    if use_tls and port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pw)
            s.sendmail(cfg["from_email"], to_addrs, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if use_tls:
                ctx = ssl.create_default_context()
                s.starttls(context=ctx)
            if user:
                s.login(user, pw)
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
    CREATE TABLE IF NOT EXISTS deleted_probes(
      id INTEGER PRIMARY KEY,
      deleted_at TEXT NOT NULL,
      deleted_by TEXT NOT NULL,
      deleted_by_name TEXT NOT NULL,
      probe_id TEXT,
      probe_date TEXT,
      workshop TEXT,
      point TEXT,
      status TEXT,
      master TEXT,
      indicators TEXT);
    """)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    if "full_name" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
    if "must_change_pw" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0")
    con.execute("UPDATE users SET full_name=login WHERE full_name IS NULL OR trim(full_name)=''")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS probes(
      id TEXT PRIMARY KEY,
      origin TEXT NOT NULL,
      payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_meta(
      key TEXT PRIMARY KEY,
      value TEXT);
    """)
    con.commit()
    con.close()


def now():
    return datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip_text(value, limit):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _name_key(value):
    return str(value or "").strip().lower()


def _merge_name_list(left, right):
    out = []
    seen = set()
    for name in list(left or []) + list(right or []):
        key = _name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(str(name).strip())
    return out


def _merge_named_lists(left, right):
    merged = {}
    for src in (left, right):
        if not isinstance(src, dict):
            continue
        for key, names in src.items():
            label = str(key)
            merged[label] = _merge_name_list(merged.get(label), names if isinstance(names, list) else [])
    return merged


def _as_named_map(value):
    if isinstance(value, dict):
        out = {}
        for key, names in value.items():
            if isinstance(names, list):
                out[str(key)] = names
        return out
    if isinstance(value, list):
        names = [str(name).strip() for name in value if str(name or "").strip()]
        return {"*": names} if names else {}
    return {}


def _trim_common_foremen(foremen):
    if not isinstance(foremen, dict) or "*" not in foremen:
        return foremen
    placed = set()
    for key, names in foremen.items():
        if key == "*" or not isinstance(names, list):
            continue
        for name in names:
            placed.add(_name_key(name))
    common = [name for name in foremen.get("*") or [] if _name_key(name) not in placed]
    out = {key: names for key, names in foremen.items() if key != "*"}
    if common:
        out["*"] = common
    return out


def _drop_names(names, removed):
    keys = {_name_key(name) for name in (removed if isinstance(removed, list) else [])}
    return [name for name in names if _name_key(name) not in keys]


def _drop_named(lists, removed):
    if not isinstance(removed, dict):
        return lists
    out = {}
    for key, names in lists.items():
        gone = []
        for removed_key, removed_names in removed.items():
            if _name_key(removed_key) == _name_key(key) and isinstance(removed_names, list):
                gone.extend(removed_names)
        left = _drop_names(names, gone)
        if left:
            out[key] = left
    return out


def _merge_catalog(prev, incoming):
    """Названия только добавляются. Убранное возвращается лишь по явному списку unhide."""
    prev = prev if isinstance(prev, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    unhide = incoming.get("unhide") if isinstance(incoming.get("unhide"), dict) else {}
    unhide_foremen = unhide.get("foremen") if isinstance(unhide.get("foremen"), dict) else {}
    hidden_foremen = _drop_named(
        _merge_named_lists(_as_named_map(prev.get("hiddenForemen")), _as_named_map(incoming.get("hiddenForemen"))),
        unhide_foremen,
    )
    return {
        "cehs": _merge_name_list(prev.get("cehs"), incoming.get("cehs")),
        "points": _merge_named_lists(prev.get("points"), incoming.get("points")),
        "foremen": _trim_common_foremen(_merge_named_lists(_as_named_map(prev.get("foremen")), _as_named_map(incoming.get("foremen")))),
        "hiddenCehs": _drop_names(_merge_name_list(prev.get("hiddenCehs"), incoming.get("hiddenCehs")), unhide.get("cehs")),
        "hiddenPoints": _drop_named(_merge_named_lists(prev.get("hiddenPoints"), incoming.get("hiddenPoints")), unhide.get("points")),
        "hiddenForemen": hidden_foremen,
    }


def _entry_key(entry):
    eid = str(entry.get("id") or "").strip()
    if eid:
        return "id:" + eid
    return "raw:" + json.dumps([entry.get("d"), entry.get("c"), entry.get("p"), entry.get("s")], ensure_ascii=False, sort_keys=True)


def _indicators_filled(entry):
    return any(ch != "." for ch in str(entry.get("s") or ""))


def _history_items(entry):
    if not isinstance(entry, dict):
        return []
    raw = entry.get("edit_history")
    items = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            if changes == ["Внесены результаты смыва"] or not changes:
                continue
            items.append({
                "at": str(item.get("at") or ""),
                "by": str(item.get("by") or ""),
                "changes": [str(x) for x in changes],
            })
    if items:
        return items
    changes = entry.get("last_edit_changes")
    if not entry.get("edited_by") or changes == ["Внесены результаты смыва"]:
        return []
    if not isinstance(changes, list) or not changes:
        return []
    return [{
        "at": str(entry.get("edited_at") or ""),
        "by": str(entry.get("edited_by") or ""),
        "changes": [str(x) for x in changes],
    }]


def _merge_histories(prev, incoming):
    seen = set()
    out = []
    for item in _history_items(prev) + _history_items(incoming):
        key = item["at"] + "\n" + item["by"] + "\n" + json.dumps(item["changes"], ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda item: item["at"])
    return out


def _merge_entry(prev, incoming):
    history = _merge_histories(prev, incoming)
    prev_at = str(prev.get("edited_at") or "")
    incoming_at = str(incoming.get("edited_at") or "")
    if prev_at and (not incoming_at or incoming_at < prev_at):
        out = dict(prev)
    else:
        out = dict(prev)
        out.update(incoming)
        if _indicators_filled(prev) and not _indicators_filled(incoming):
            out["s"] = prev.get("s")
            if "k" in prev:
                out["k"] = prev.get("k")
            if prev.get("stage"):
                out["stage"] = prev.get("stage")
        for key in ("foreman", "master", "p", "d"):
            if not str(incoming.get(key) or "").strip() and str(prev.get(key) or "").strip():
                out[key] = prev.get(key)
    if history:
        out["edit_history"] = history
        last = history[-1]
        if str(last.get("at") or "") >= str(out.get("edited_at") or ""):
            out["edited_at"] = last.get("at") or ""
            out["edited_by"] = last.get("by") or ""
            out["last_edit_changes"] = last.get("changes") or []
    return out


def _read_journal_file(path):
    if not path or not os.path.exists(path):
        return {"entries": [], "catalog": {}}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    return {"entries": [], "catalog": {}}


def _write_journal_file(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _latest_dated_journal():
    if not os.path.isdir(BACKUP_DIR):
        return None
    names = []
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith("smyvy-journal-") and name.endswith(".json")):
            continue
        names.append(name)
    if not names:
        return None
    names.sort()
    return os.path.join(BACKUP_DIR, names[-1])


def _load_shared_journal():
    if os.path.exists(SHARED_JOURNAL):
        return _read_journal_file(SHARED_JOURNAL)
    latest = _latest_dated_journal()
    if latest:
        return _read_journal_file(latest)
    return {"entries": [], "catalog": {}}


def _deleted_probe_ids():
    con = db()
    rows = con.execute(
        "SELECT probe_id FROM deleted_probes WHERE probe_id IS NOT NULL AND trim(probe_id) != ''"
    ).fetchall()
    con.close()
    return {str(r["probe_id"]) for r in rows}


def _undelete_probe_ids(ids):
    wanted = [str(i).strip() for i in ids if str(i or "").strip()]
    if not wanted:
        return
    con = db()
    con.executemany("DELETE FROM deleted_probes WHERE probe_id=?", [(i,) for i in wanted])
    con.commit()
    con.close()


def _without_deleted(entries, deleted_ids):
    return [entry for entry in entries if str(entry.get("id") or "") not in deleted_ids]


def _meta_get(key):
    con = db()
    row = con.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else ""


def _meta_set(key, value):
    con = db()
    con.execute("INSERT INTO app_meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()


def _probe_id(entry):
    eid = str(entry.get("id") or "").strip()
    if eid:
        return eid
    raw = "%s|%s|%s" % (entry.get("d") or "", entry.get("c"), entry.get("p") or "")
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _rows_to_entries(rows):
    out = []
    for row in rows:
        try:
            item = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(item, dict):
            item["origin"] = row["origin"]
            item["id"] = row["id"]
            out.append(item)
    return out


def _db_entries(origin=None):
    con = db()
    if origin:
        rows = con.execute("SELECT id, origin, payload FROM probes WHERE origin=?", (origin,)).fetchall()
    else:
        rows = con.execute("SELECT id, origin, payload FROM probes").fetchall()
    con.close()
    return _rows_to_entries(rows)


def _save_user_entries(entries):
    con = db()
    con.execute("DELETE FROM probes WHERE origin='user'")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = _probe_id(entry)
        if eid.startswith("h") and not str(entry.get("id") or "").startswith("u"):
            continue
        payload = dict(entry)
        payload["id"] = eid
        payload.pop("origin", None)
        if _indicators_filled(payload):
            payload.pop("stage", None)
        con.execute(
            "INSERT OR REPLACE INTO probes(id, origin, payload) VALUES(?,?,?)",
            (eid, "user", json.dumps(payload, ensure_ascii=False)),
        )
    con.commit()
    con.close()


def _load_catalog_db():
    raw = _meta_get("catalog")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_catalog_db(catalog):
    _meta_set("catalog", json.dumps(catalog or {}, ensure_ascii=False))


def _hist_from_html():
    path = os.path.join(BASE, "smyvy.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except Exception:
        return []
    match = re.search(r"const HIST = (\{.*?\});\s*const REF", html, re.S)
    if not match:
        return []
    try:
        hist = json.loads(match.group(1))
    except Exception:
        return []
    probes = hist.get("probes") if isinstance(hist, dict) else None
    return probes if isinstance(probes, list) else []


def sync_swabs_into_db():
    """Из страницы добавляются только те старые пробы, которых в базе ещё нет."""
    hist = _hist_from_html()
    con = db()
    for entry in hist:
        if not isinstance(entry, dict):
            continue
        eid = _probe_id(entry)
        payload = dict(entry)
        payload["id"] = eid
        row = con.execute("SELECT id FROM probes WHERE id=?", (eid,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO probes(id, origin, payload) VALUES(?,?,?)",
                (eid, "hist", json.dumps(payload, ensure_ascii=False)),
            )
    con.commit()
    con.close()
    if _meta_get("journal_imported_v2") == "1":
        return
    incoming = []
    if os.path.isdir(BACKUP_DIR):
        names = sorted(name for name in os.listdir(BACKUP_DIR) if name.startswith("smyvy-journal") and name.endswith(".json"))
        for name in names:
            shared = _read_journal_file(os.path.join(BACKUP_DIR, name))
            for entry in shared.get("entries") or []:
                if isinstance(entry, dict) and str(entry.get("id") or "").startswith("u"):
                    incoming.append(entry)
            if isinstance(shared.get("catalog"), dict):
                _save_catalog_db(_merge_catalog(_load_catalog_db(), shared.get("catalog")))
    merged = _without_deleted(_merge_journal(_db_entries("user"), incoming), _deleted_probe_ids())
    _save_user_entries(merged)
    _meta_set("journal_imported_v2", "1")


def _store_shared_journal(incoming_entries, incoming_catalog, replace=False):
    with JOURNAL_LOCK:
        prev_users = _db_entries("user")
        if replace:
            merged_users = [entry for entry in incoming_entries if isinstance(entry, dict)]
            _undelete_probe_ids(_probe_id(entry) for entry in merged_users)
        else:
            merged_users = _merge_journal(prev_users, incoming_entries)
        deleted = _deleted_probe_ids()
        merged_users = _without_deleted(merged_users, deleted)
        if len(merged_users) > 20000:
            merged_users = merged_users[-20000:]
        _save_user_entries(merged_users)
        catalog = _merge_catalog(_load_catalog_db(), incoming_catalog)
        _save_catalog_db(catalog)
        entries = _without_deleted(_db_entries(), deleted)
        deleted_ids = sorted(deleted)
        _remember_journal(entries, deleted_ids, catalog)
        payload = {"saved_at": now(), "entries": entries, "catalog": catalog}
        _write_journal_file(SHARED_JOURNAL, payload)
        day = datetime.date.today().isoformat()
        _write_journal_file(os.path.join(BACKUP_DIR, "smyvy-journal-%s.json" % day), payload)
        return entries, deleted_ids


def _invalidate_journal_cache():
    with JOURNAL_LOCK:
        JOURNAL_CACHE["payload"] = None


def _remember_journal(entries, deleted_ids, catalog):
    JOURNAL_CACHE["rev"] = int(JOURNAL_CACHE["rev"]) + 1
    JOURNAL_CACHE["payload"] = {
        "rev": JOURNAL_CACHE["rev"],
        "entries": entries,
        "deleted_ids": list(deleted_ids),
        "catalog": catalog or {},
    }
    return JOURNAL_CACHE["payload"]


def _journal_snapshot():
    with JOURNAL_LOCK:
        if JOURNAL_CACHE["payload"] is not None:
            return JOURNAL_CACHE["payload"]
        deleted_ids = sorted(_deleted_probe_ids())
        entries = _without_deleted(_db_entries(), set(deleted_ids))
        return _remember_journal(entries, deleted_ids, _load_catalog_db())


def _merge_journal(prev_entries, incoming_entries):
    merged = []
    index = {}
    for source in (prev_entries, incoming_entries):
        if not isinstance(source, list):
            continue
        for entry in source:
            if not isinstance(entry, dict):
                continue
            key = _entry_key(entry)
            if key in index:
                merged[index[key]] = _merge_entry(merged[index[key]], entry)
            else:
                index[key] = len(merged)
                merged.append(entry)
    return merged


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

    def _must_change_pw(self):
        u = self._user()
        return bool(u and u["must_change_pw"])

    # --- GET: статика ---
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/input", "/journal", "/statistics", "/ai", "/users",
                 "/vvod", "/zhurnal", "/statistika", "/ii", "/polzovateli"):
            qs = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
            self.path = "/smyvy.html" + qs
            p = "/smyvy.html"
        if p.startswith("/api/") and p != "/api/me" and self._must_change_pw():
            return self._err("сначала смените временный пароль", 403)
        if p == "/api/me":
            u = self._user()
            return self._json({"login": u["login"], "role": u["role"], "full_name": u["full_name"] or u["login"], "must_change": bool(u["must_change_pw"])} if u else {"login": None})
        if p == "/api/users":
            u = self._user()
            if not u or u["role"] != "admin":
                return self._err("нужны права администратора", 403)
            con = db()
            rows = [{"id": r["id"], "login": r["login"], "full_name": r["full_name"] or r["login"], "role": r["role"], "active": r["active"], "created_at": r["created_at"]}
                    for r in con.execute("SELECT * FROM users ORDER BY id")]
            con.close()
            return self._json({"users": rows, "roles": [{"k": k, "t": ROLE_TITLES[k]} for k in ROLES]})
        if p == "/api/deleted-probes":
            return self._list_deleted_probes()
        if p == "/api/journal":
            return self._get_shared_journal()
        if p == "/smyvy.html":
            return self._send_app_html()
        return super().do_GET()

    def _send_app_html(self):
        with open(os.path.join(BASE, "smyvy.html"), encoding="utf-8") as f:
            html = f.read()
        user = self._user()
        lock = "lock hidden" if user and user["role"] != "pending" and not user["must_change_pw"] else "lock ask"
        html = html.replace('<div id="lock" class="lock">', '<div id="lock" class="%s">' % lock, 1)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # --- POST ---
    def do_POST(self):
        p = self.path.split("?")[0]
        if p not in ("/api/register", "/api/login", "/api/logout", "/api/change-password") and self._must_change_pw():
            return self._err("сначала смените временный пароль", 403)
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
        if p == "/api/deleted-probes":
            return self._log_deleted_probe()
        if p == "/api/backup":
            return self._save_journal_backup()
        return self._err("не найдено", 404)

    def _save_journal_backup(self):
        u = self._user()
        if not u or u["role"] == "pending":
            return self._err("нужен вход", 401)
        b = self._body()
        if not isinstance(b, dict) or not isinstance(b.get("entries"), list):
            return self._err("неверный запрос")
        entries = b.get("entries")
        if len(entries) > 20000:
            return self._err("слишком много записей", 422)
        replace = bool(b.get("replace"))
        if replace and u["role"] not in ("quality", "admin"):
            return self._err("нужны права на изменение", 403)
        merged, deleted_ids = _store_shared_journal(entries, b.get("catalog"), replace)
        restored = sum(1 for entry in merged if isinstance(entry, dict) and str(entry.get("id") or "").startswith("u"))
        return self._json({
            "ok": True,
            "count": len(merged),
            "restored": restored,
            "entries": merged,
            "deleted_ids": deleted_ids,
            "catalog": _load_catalog_db(),
            "rev": JOURNAL_CACHE["rev"],
        })

    def _journal_rev_query(self):
        if "?" not in self.path:
            return None
        for part in self.path.split("?", 1)[1].split("&"):
            if not part.startswith("rev="):
                continue
            try:
                return int(part.split("=", 1)[1])
            except Exception:
                return None
        return None

    def _get_shared_journal(self):
        u = self._user()
        if not u or u["role"] == "pending":
            return self._err("нужен вход", 401)
        snap = _journal_snapshot()
        if self._journal_rev_query() == snap["rev"]:
            return self._json({"unchanged": True, "rev": snap["rev"]})
        return self._json(snap)

    def _list_deleted_probes(self):
        u = self._user()
        if not u or u["role"] != "admin":
            return self._err("нужны права администратора", 403)
        con = db()
        rows = con.execute(
            "SELECT deleted_at, deleted_by, deleted_by_name, probe_date, workshop, point, status, master, indicators "
            "FROM deleted_probes ORDER BY id DESC LIMIT 500"
        ).fetchall()
        con.close()
        return self._json({"items": [dict(r) for r in rows]})

    def _log_deleted_probe(self):
        u = self._user()
        if not u or u["role"] not in ("quality", "admin"):
            return self._err("нужны права на удаление", 403)
        b = self._body()
        if not isinstance(b, dict):
            return self._err("неверный запрос")
        probe_date = clip_text(b.get("probe_date"), 40)
        workshop = clip_text(b.get("workshop"), 200)
        point = clip_text(b.get("point"), 300)
        if not probe_date or not workshop or not point:
            return self._err("в записи нет даты, цеха или точки", 422)
        con = db()
        con.execute(
            "INSERT INTO deleted_probes(deleted_at, deleted_by, deleted_by_name, probe_id, probe_date, workshop, point, status, master, indicators) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                now(),
                u["login"],
                u["full_name"] or u["login"],
                clip_text(b.get("probe_id"), 80),
                probe_date,
                workshop,
                point,
                clip_text(b.get("status"), 80),
                clip_text(b.get("master"), 200),
                clip_text(b.get("indicators"), 2000),
            ),
        )
        con.commit()
        con.close()
        _invalidate_journal_cache()
        audit(u["login"], "delete_probe", "%s · %s · %s" % (probe_date, workshop, point))
        return self._json({"ok": True})

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
        con.execute("UPDATE users SET pass_hash=?, must_change_pw=1 WHERE id=?", (hash_password(temp), uid))
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
        if u["must_change_pw"] and new == (b.get("old") or ""):
            return self._err("новый пароль должен отличаться от временного", 422)
        con = db()
        con.execute("UPDATE users SET pass_hash=?, must_change_pw=0 WHERE id=?", (hash_password(new), u["id"]))
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
        return self._json({"login": row["login"], "role": row["role"], "full_name": row["full_name"] or row["login"], "must_change": bool(row["must_change_pw"])}, cookie=cookie)

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


def extra_backup_dir():
    env = os.environ.get("SMYVY_BACKUP_DIR", "").strip()
    if env:
        return env
    if not os.path.exists(BACKUP_DIR_FILE):
        return ""
    try:
        with open(BACKUP_DIR_FILE, encoding="utf-8") as f:
            return f.readline().strip()
    except Exception:
        return ""


def _backup_targets(day):
    name = "smyvy-app-%s.db" % day.isoformat()
    targets = [os.path.join(BACKUP_DIR, name)]
    extra = extra_backup_dir()
    if extra:
        targets.append(os.path.join(extra, name))
    return targets


def _snapshot_db(dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def copy_app_db(day):
    if not os.path.exists(DB_PATH):
        return
    for dest in _backup_targets(day):
        try:
            _snapshot_db(dest)
        except Exception:
            pass


def next_backup_at(now=None):
    now = now or datetime.datetime.now()
    target = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return target


def catch_up_db_backup(now=None):
    now = now or datetime.datetime.now()
    if not os.path.exists(DB_PATH):
        return
    slot = now.replace(hour=BACKUP_HOUR, minute=0, second=0, microsecond=0)
    day = now.date() if now >= slot else now.date() - datetime.timedelta(days=1)
    for dest in _backup_targets(day):
        if os.path.exists(dest):
            continue
        try:
            _snapshot_db(dest)
        except Exception:
            pass


def backup_at_20_loop():
    while True:
        time.sleep((next_backup_at() - datetime.datetime.now()).total_seconds())
        try:
            copy_app_db(datetime.date.today())
        except Exception:
            pass
        time.sleep(65)


if __name__ == "__main__":
    import sys
    init_schema()
    sync_swabs_into_db()
    _save_user_entries(_db_entries("user"))
    catch_up_db_backup()
    threading.Thread(target=backup_at_20_loop, daemon=True).start()
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
            con.execute("UPDATE users SET pass_hash=?, active=1, must_change_pw=0 WHERE id=?", (hash_password(newpw), row["id"]))
            print("Пароль обновлён для:", email)
        con.commit()
        con.close()
        audit("cli", "setpass", email)
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "testmail":
        seed_admin()
        if not smtp_ready():
            print("SMTP не настроен. Создайте .smtp_config (см. .smtp_config.example).")
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
    print("Смывы %s: http://%s:%d/smyvy.html" % (APP_VERSION, HOST, PORT))
    print("Пользователей в базе: %d%s" % (n, "  (первый зарегистрированный станет админом)" if n == 0 else ""))
    print("Ключ ИИ (DeepSeek): " + ("есть" if ai_key() else "нет — создайте файл .ai_key"))
    print("Почта (%s): %s" % (SMTP_FROM, "настроена" if smtp_ready() else "нет — создайте .smtp_config"))
    print("Остановить — Ctrl+C или закройте окно.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
