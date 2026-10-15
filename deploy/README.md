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

1. Репозиторий → **Settings** → **Actions** → **Runners** → **New self-hosted runner** (Linux x64).
2. Каталог, например `~/actions-runner-smyvy`, установка по инструкции GitHub.
3. Labels при регистрации: `aidev` (плюс стандартные `self-hosted`, `Linux`).
4. Сервис runner: `./svc.sh install` и `./svc.sh start` (или user-level unit).

## GitHub Environment

**Settings** → **Environments** → **production** → **Required reviewers** (кто подтверждает выкладку).

Рекомендуется **branch protection** на `main`: обязательный status check **test**.
