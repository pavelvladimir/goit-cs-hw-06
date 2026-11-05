# GoIT CS — домашнє завдання 6

Застосунок із формою повідомлень. HTTP-сервер приймає дані, передає їх через TCP
Socket-серверу, а той записує повідомлення в MongoDB.

Увесь код Python-сервера — у `main.py`, без Python-вебфреймворків. HTTP працює на
порту 3000, Socket — на 5000, кожен у своєму процесі. Для frontend використано
Next.js і Base UI, як у завданні 02. Під час збірки вони створюють статичні файли,
які потім віддає Python. Окремого Node.js сервера немає.

## Запуск

Потрібні Docker із Compose v2, [uv](https://docs.astral.sh/uv/getting-started/installation/)
і Make. З кореня проєкту:

```bash
make up
```

Команда створить `.env` і паролі в `secrets/`, збере образ та запустить застосунок
із MongoDB. Наявні налаштування й паролі не перезаписуються. Перший запуск потребує
інтернету; Node.js на хості для цього не потрібний.

Відкрити [головну сторінку](http://127.0.0.1:3000) або
[форму](http://127.0.0.1:3000/message.html).
[Swagger UI](http://127.0.0.1:3000/docs) містить опис API та дозволяє надіслати запит.

Ті самі дії без Make:

```bash
uv run --locked python -m scripts.init_local
docker compose up --build -d --wait
```

`docker compose up` — варіант команди `docker-compose up` для Compose v2.
Якщо порт зайнятий, змініть `APP_PORT` у `.env`.

## Що відбувається з повідомленням

Socket-сервер перетворює байти форми на словник і додає час отримання з `datetime.now()`:

```json
{
  "date": "2026-11-01 12:30:00.123456",
  "username": "Pavel",
  "message": "Привіт!"
}
```

MongoDB також додає `_id`. Дані зберігаються у volume `mongo_data`.
Сторінки `index.html`, `message.html`, файли `style.css` і `logo.png` доступні через
Python-сервер. Для невідомої адреси він повертає `error.html` зі статусом 404.

## Логи та зупинка

```bash
make logs
make down
```

Звичайний `down` залишає дані. `docker compose down --volumes` видаляє їх разом із volume.
Налаштування, перегляд записів і діагностика описані в [operations.md](docs/operations.md).

## Перевірка

Для локальної розробки додатково потрібні Node.js із `.nvmrc` і pnpm 12.11.2.

```bash
make sync
make install-browsers
make check
```

Це перевірить код, типи, збірку, Python, браузери та роботу з MongoDB.
Docker-тести створюють окрему базу й прибирають її після завершення.
Робочі повідомлення вони не використовують. Решту команд показує `make help`.

`make audit-image` окремо збирає й перевіряє кінцевий Docker-образ через Trivy,
зберігає SBOM і звіт у `artifacts/image-audit/`. [Деталі та винятки](docs/image-audit.md).

## Документація

- [Як працюють сервери та TCP-протокол](docs/architecture.md)
- [Frontend і локальна розробка](docs/frontend.md)
- [Налаштування та робота з даними](docs/operations.md)
- [Збої, навантаження і резервні копії](docs/resilience.md)
- [Результати тестів](docs/verification.md)
- [Аудит Docker-образу та SBOM](docs/image-audit.md)
- [Використані матеріали](docs/sources.md)
