# SBOM Python і frontend

```bash
make audit-python-sbom
make audit-frontend-sbom
make audit-all
```

Перші дві команди перевіряють відповідні lockfiles через закріплений Trivy.
`audit-all` додає аудит кінцевого Docker-образу та наявні `pip-audit` і `pnpm audit`.
Знахідка в одному кроці не зупиняє решту перевірок. Сервери запускати не потрібно.
Потрібні Docker, uv, а для frontend — Node.js із `.nvmrc` і pnpm із `package.json`.
Мережа потрібна для бази CVE, frontend-залежностей і додаткових аудитів.

Кожен запуск створює нову папку `artifacts/dependency-audit/<час-UTC>/`.
У корені — загальні `report.md` і `report.json`, усередині — окремі перевірки:

| Папка | Що містить |
| --- | --- |
| `python-runtime` | Граф runtime-залежностей `uv.lock` |
| `python-all` | Той самий lockfile разом із dev-залежностями |
| `frontend-declared` | Production dependencies з `pnpm-lock.yaml` |
| `frontend-all` | Повний frontend-граф і перевірку скопійованих файлів |
| `image` | [Фактичний вміст кінцевого образу](image-audit.md), лише для `audit-all` |

Кожна перевірка має `trivy.json`, `sbom.cdx.json`, `exceptions.json` і report.
SBOM створюється з того самого результату Trivy. Перевіряється повний набір PURL,
а не лише пакети зі знайденими CVE. Поруч залишаються копії вхідних файлів,
їхні SHA-256, Git commit, ознака незбережених змін, версія сканера та час бази CVE.
Звіти не входять до Git; наявна папка не перезаписується.

## Що саме потрапляє у веб

Lockfile містить варіанти для різних платформ. У Python він також містить корінь
проєкту, хоча `uv` не встановлює його як пакет. При `audit-all` список Python
runtime-залежностей звіряється з фактично встановленими пакетами образу.
Для поточного проєкту вони мають збігатися без кореня проєкту.

У frontend `dependencies` не означає «увесь код цього пакета є у браузері»,
а `devDependencies` не гарантує відсутності пакета у static export.
Наприклад, `swagger-ui-dist` лежить у dev, але його JS і CSS віддає сервер.

`frontend/published-assets.json` — спільний список для build і аудиту.
Після свіжого `make build-frontend` перевіряються версія встановленого пакета,
його наявність у повному SBOM і SHA-256 кожного скопійованого файлу, включно
з LICENSE/NOTICE. Зайві файли у `out/vendor/` теж викликають помилку.
Докази лежать у `frontend-all/published-assets.json` і report.
Після невдалого build старий export не використовується як успішний результат.

Ця перевірка підтверджує прямі копії vendor-файлів. Склад мініфікованих Next.js
chunks на рівні окремих npm-пакетів вона не відновлює; тому перевіряється весь
frontend lockfile. Для його пакетів також не гарантується повнота ліцензійних даних.

## Порівняння запусків

Передайте папку попереднього звіту; timestamp нижче потрібно замінити на свій:

```bash
make audit-all BASELINE=artifacts/dependency-audit/20261105T134716712448Z
```

Або порівняйте тільки Python:

```bash
uv run --locked python -m scripts.audit_dependencies --target python \
  --baseline artifacts/dependency-audit/20261105T134716712448Z
```

`changes.json` і загальний report покажуть додані й видалені PURL, зміни версій,
нові, зниклі та змінені знахідки. Оновлення пакета також видно як видалення
старої версії й додавання нової; ці числа не треба складати.
Зберігаються scoped npm names, кілька версій одного пакета й qualifiers PURL.
Різниця між commit-ами визначається за збереженими SBOM цих запусків.

Перед порівнянням перевіряються SHA-256 старих артефактів. Неповний, змінений
або відсутній baseline дає ERROR. Зміна бази CVE чи платформи образу позначається
окремо: нова знахідка не обов'язково спричинена зміною коду.
Звіти не підписані, тож SHA-256 допомагає виявити випадкову зміну файлів,
але не є незалежним підтвердженням їхнього походження.

## Політика та результат

Для Trivy діють ті самі рівні, що й для образу: CRITICAL, HIGH та UNKNOWN блокують,
MEDIUM і LOW залишаються видимими. Винятки — у `security/dependency-exceptions.json`.
Крім полів [політики образу](image-audit.md#винятки), потрібне поле `scope` з назвою
однієї з чотирьох lockfile-перевірок. Виняток із `frontend-all` не діє в іншому scope.
Прострочені й невикористані винятки блокують відповідну перевірку. Список зараз порожній.

`pip-audit` і `pnpm audit` зберігають власну політику й виконуються без приховування
знахідок. Їхній ненульовий код робить загальний результат неуспішним; причина є в log.
Тому MEDIUM може не блокувати Trivy, але блокувати `pnpm audit`.
Exit codes скрипта: `0` — PASS, `1` — FAIL, `2` — ERROR. Make має власний ненульовий код.

05.11.2026 перевірено 4/44 записи Python runtime/all (включно з коренем проєкту),
107/617 frontend declared/all і всі 4 файли Swagger UI. Python — без знахідок.
У повному frontend є MEDIUM у minimatch 10.2.6; `pnpm audit` також повідомляє про нього.
Образ має 31 HIGH і 11 UNKNOWN. `audit-all` завершив усі кроки з результатом **FAIL**.
Порівняння всіх п'яти SBOM між двома запусками підтвердило відсутність змін пакетів і знахідок.

Підтримка lockfiles: [Trivy uv](https://trivy.dev/docs/latest/coverage/language/python/#uv)
та [Trivy pnpm](https://trivy.dev/docs/latest/coverage/language/nodejs/#pnpm).
