# Frontend

Frontend побудований на Next.js, React, TypeScript і Base UI. Використовується
статичний export, тому в готовому контейнері працює лише Python-сервер.
Версії залежностей записані в `frontend/package.json` і lockfile.

## Розробка

Потрібні Node.js із `.nvmrc` і pnpm 12.11.2. Команди виконуються з кореня проєкту:

```bash
make sync
make up
make dev-frontend
```

Next dev відкривається на [localhost:3001](http://localhost:3001) і пересилає API-запити
до Python на порту 3000. Якщо порт backend змінено, потрібно поправити адреси
в `frontend/next.config.ts`. CSS і компоненти оновлюються під час розробки автоматично.

```bash
make build-frontend
make lint-frontend
make test-frontend
```

Збірка генерує SDK, CSS, локальні файли Swagger UI та каталог `frontend/out/`.
`404.html` копіюється в `error.html`, щоб зберегти потрібну в завданні назву.
Згенеровані файли вручну редагувати не потрібно.

## Де що знаходиться

- `src/app/` — сторінки та layout із `DirectionProvider`, напрямок `ltr`.
- `src/components/` — форма й навігація на Base UI.
- `src/api/message.ts` — надсилання форми та перевірка відповіді.
- `src/style.css` — стилі, які зберігають вигляд початкових сторінок.
- `tests/fixtures/` — HTML/CSS для порівняння вигляду; `reference/` — HTML до міграції.

Bootstrap залишився тільки в тестових еталонах. Основна форма й навігація використовують
Base UI, а статичні картки та заголовки — звичайний HTML.

## Форма й API

React надсилає `username` і `message` на `POST /api/message` як URL-encoded тіло.
Для JSON-відповіді додає `Accept: application/json`. Також підтримується `POST /message`.

| HTTP | Відповідь | Дія форми |
| --- | --- | --- |
| 201 | `{"status":"saved"}` | Показати підтвердження й очистити поля |
| 400 | `{"error":"invalid_message"}` | Показати помилку, залишити текст |
| 503 | `{"error":"storage_unavailable"}` | Залишити текст: збереження не підтверджене |

Під час запиту повторне надсилання заблоковане. Клієнт чекає до 10 секунд і не
повторює запит автоматично. Невідома або пошкоджена відповідь не вважається успіхом.
Без JavaScript працює звичайна HTML-форма з перенаправленням після успіху;
при помилці вона показує статичну сторінку, але не зберігає введений текст.

Контракт задає `main.openapi_schema()`. `make generate-api` оновлює snapshot
і TypeScript/Zod клієнт, а `make check-api` перевіряє їхню відповідність.
Swagger UI доступний на `/docs` і використовує локальні файли. POST через нього
створює справжній запис у MongoDB.

Playwright перевіряє три браузери, клавіатуру, доступність, помилки форми та вигляд
сторінок. Звіти зберігаються в `frontend/playwright-report/`.
