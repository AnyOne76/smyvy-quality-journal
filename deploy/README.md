# Деплой на aidev

## systemd (user)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/smyvy-app.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now smyvy-app.service
loginctl enable-linger "$USER"
```

Проверка: `curl -I http://10.0.5.173:8020/input`

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
RUNNER_TOKEN='<token>' bash scripts/install-github-runner.sh
```

Labels: `aidev` (workflow: `runs-on: [self-hosted, aidev]`).

## GitHub Environment

**Settings** → **Environments** → **production** → **Required reviewers** (кто подтверждает выкладку).

Рекомендуется **branch protection** на `main`: обязательный status check **test**.
