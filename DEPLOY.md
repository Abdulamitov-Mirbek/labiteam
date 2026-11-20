# Деплой Сигнал.kg на сервер RUBA

Сервер `ruba.memorymee.org` (209.97.143.25) уже держит боевой сайт RUBA. Сигнал.kg ставится
**отдельным** docker-проектом и **не трогает** `/opt/ruba` и его контейнеры. Порты наружу не
публикуются — HTTPS даёт Caddy RUBA по сети `ruba_default`.

## Первый деплой

```bash
ssh root@ruba.memorymee.org
install -d /opt/signalkg && cd /opt/signalkg
git clone https://github.com/Abdulamitov-Mirbek/labiteam.git . 2>/dev/null || git pull
# (необязательно) ключ 2ГИС для подложки карты:
#   echo 'DGIS_KEY=...' > .env
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Контейнер: `signalkg-web`, слушает порт 8000 **внутри** сети `ruba_default`. Наружу порт не
открыт. Жалобы, замеры и кеш объявлений лежат в томе `signalkg_var` (переживают пересборку).
Память: лимит 256 МБ (по факту ~50 МБ).

## HTTPS через Caddy RUBA

Caddy терминирует TLS для всех сайтов и настраивается **в репозитории RUBA**, а не на сервере.
Чтобы открыть Сигнал.kg по HTTPS:

1. Направьте A-запись домена (например `signal.memorymee.org`) на `209.97.143.25`.
2. Передайте владельцу RUBA для site-записи Caddy:
   - домен: `signal.memorymee.org` (или выбранный);
   - контейнер и порт: `signalkg-web:8000`;
   - сеть: `ruba_default` (контейнер уже в ней).

   Site-запись Caddy выглядит так:

   ```
   signal.memorymee.org {
       reverse_proxy signalkg-web:8000
   }
   ```

После этого телефоны открывают приложение по `https://signal.memorymee.org/app/` — GPS,
установка на экран и «Поделиться» работают (временная ссылка Cloudflare больше не нужна).

## Обновление

```bash
cd /opt/signalkg && git pull && docker compose -f docker-compose.prod.yml up -d --build
```

Chrome-плагин ходит на `127.0.0.1:8000` (`extension/background.js`) — он остаётся локальным
инструментом разработки и к серверу отношения не имеет.

## Важное ограничение за обратным прокси

Приложение берёт IP из соединения и не доверяет `X-Forwarded-For` (защита от подмены). За Caddy
все запросы приходят с одного IP (Caddy), поэтому лимиты «5 жалоб и 20 проверок объявлений в
минуту» становятся **общими на всех**, а не на каждого. Для демонстрации это приемлемо. Если
понадобится честный лимит по пользователю — доверять `CF-Connecting-IP`/`X-Forwarded-For`
**только** когда соединение пришло из сети `ruba_default`. Это отдельная правка в `server/app.py`.
