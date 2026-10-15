# Деплой на aidev

## Docker (прод)

```bash
cd /home/devuser/projects/smyvy-quality-journal
docker compose up -d --build
docker compose ps
curl -I http://10.0.5.173:8020/input
```

Данные на хосте в каталоге проекта (volume в `docker-compose.yml`). Политика `restart: unless-stopped` — контейнер поднимается после ребута Docker.

Домен: `deploy/nginx-laboratory-washes.conf.example`.

## Тесты на сервере

```bash
cd /home/devuser/projects/smyvy-quality-journal
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
```

## GitHub self-hosted runner

Бинарники уже можно положить в `~/actions-runner-smyvy`. Регистрация (токен живёт ~1 ч):

1. Репозиторий → **Settings** → **Actions** → **Runners** → **New self-hosted runner** → скопировать token.
2. На aidev:

```bash
cd /home/devuser/projects/smyvy-quality-journal
git pull origin main
# Только от devuser, без sudo. Токен — новый с GitHub (Settings → Actions → Runners).
RUNNER_TOKEN='<token>' bash scripts/install-github-runner.sh
```

Перекачать бинарники: `UPGRADE=1`. Перерегистрация: `RECONFIGURE=1`.

Сервис: `systemctl --user status actions-runner-smyvy` (не `./svc.sh` — он требует sudo).

Labels: `aidev` (workflow: `runs-on: [self-hosted, aidev]`).

## GitHub Environment

**Settings** → **Environments** → **production** → **Required reviewers** (кто подтверждает выкладку).

Рекомендуется **branch protection** на `main`: обязательный status check **test**.
