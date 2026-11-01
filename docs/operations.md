# Налаштування та дані

`make init` створює відсутні `.env` і паролі в `secrets/`. Повторний запуск
залишає їх без змін. Паролі й локальна конфігурація не входять до Git.

## Налаштування

Основні значення є в `.env.example`. Змінні середовища мають пріоритет над `.env`.

| Параметр | Типове значення | Для чого |
| --- | --- | --- |
| `APP_PORT` | 3000 | HTTP-порт на хості |
| `LOG_LEVEL` | INFO | Рівень логів |
| `DB_CONNECT_TIMEOUT` | 2 | Бюджет операції MongoDB у секундах, 1–30 |
| `SHUTDOWN_GRACE_SECONDS` | 20 | Час завершення процесів, 16–120 секунд |
| `HTTP_MAX_CONNECTIONS` | 32 | Кількість HTTP-обробників, 1–128 |
| `TCP_MAX_CONNECTIONS` | 32 | Кількість TCP-обробників, 1–128 |
| `RECEIVE_TIMEOUT` | 5 | Час читання всього запиту, 1–5 секунд |

Час завершення має бути не меншим за `DB_CONNECT_TIMEOUT + 13`.
Збільшення часу очікування БД не змінює 10-секундний timeout браузерного клієнта.
Внутрішні порти HTTP/TCP залишаються 3000/5000 незалежно від `APP_PORT`.

Шляхи паролів для Compose задають `MONGO_PASSWORD_PATH` і `MONGO_ADMIN_PASSWORD_PATH`.
Типово це `secrets/mongo_password` і `secrets/mongo_admin_password`.
У застосунку пароль читається з `MONGO_PASSWORD_FILE`; прямий запуск Python також
підтримує `MONGO_PASSWORD`, але обидва джерела одночасно задавати не можна.
Відносні шляхи рахуються від `.env`, символ `$` у значеннях не підставляється.

Для наявної БД не генеруйте нові паролі просто заміною файлів: це не змінить пароль
користувача MongoDB. `make init` налаштовує права файлів; приватну папку `secrets/`
не варто робити загальнодоступною.

## Перевірка роботи

```bash
docker compose ps
curl --fail http://127.0.0.1:3000/health
make logs
```

`/health` перевіряє весь шлях HTTP → TCP → MongoDB без створення повідомлення.
При 503 почніть із стану контейнерів і логів. Після короткого збою БД застосунок
сам відновлює з'єднання. UUID із заголовка відповіді `X-Request-ID` допомагає
знайти пов'язані HTTP, TCP і database-події в JSON-логах.

Переглянути збережені повідомлення:

```bash
docker compose exec -T app python -c '
import json
from main import mongo_client
with mongo_client() as client:
    records = list(client.messages.messages.find({}, {"_id": 0}))
    print(json.dumps(records, ensure_ascii=False, indent=2))
'
```

## Контейнери

`make down` залишає volume `mongo_data`; наступний `make up` використає ті самі дані.
Видалення volume видаляє й повідомлення. Для резервних копій є
[окремий опис](resilience.md#резервні-копії).

App працює в Distroless від UID/GID 65532, із файловою системою лише для читання.
У ньому немає shell, pip або Node.js, тому для діагностики доступний `python -c`.
Ліміти app: 1 CPU, 256 MiB RAM і 384 процеси/потоки.

MongoDB працює в окремому Percona-контейнері та не публікує порт на хості.
Застосунок має користувача з доступом лише до БД `messages`; admin secret
підключений тільки до MongoDB. Для запуску Python поза Compose потрібна окремо
доступна БД і налаштування `MONGO_HOST`, `MONGO_PORT`, `MONGO_DATABASE` та `MONGO_USER`.
