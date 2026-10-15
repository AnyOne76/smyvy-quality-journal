# Домен laboratory-washes.kolbasa.ru

## Симптом

`DNS_PROBE_FINISHED_NXDOMAIN` — **в DNS нет имени**, браузер даже не узнаёт IP. Это не ошибка Docker/«Смывов».

Проверка с aidev:

```bash
dig +short laboratory-washes.kolbasa.ru   # пусто — записи нет
dig +short validation.kolbasa.ru          # 10.0.5.173 — так должно быть и у laboratory-washes
```

## Заявка в ИТ (AD DNS, зона kolbasa.ru)

| Поле | Значение |
|------|----------|
| Имя | `laboratory-washes` (FQDN: `laboratory-washes.kolbasa.ru`) |
| Тип | **A** |
| IP | **10.0.5.173** (aidev, тот же хост, что validation/cv) |
| NS зоны | `dc1.work.local` / `dc2` / `dc3` (как у остальных `*.kolbasa.ru`) |

После создания записи на рабочей станции: `nslookup laboratory-washes.kolbasa.ru` → `10.0.5.173`.

## Nginx на aidev (после DNS)

1. Скопировать `deploy/nginx-laboratory-washes.conf.example` → `/etc/nginx/sites-available/laboratory-washes.kolbasa.ru`
2. Симлинк в `sites-enabled`, `nginx -t`, `reload nginx`
3. Приложение уже слушает **10.0.5.173:8020** (Docker)

## Пока DNS нет

Внутренняя сеть: **http://10.0.5.173:8020/input**
