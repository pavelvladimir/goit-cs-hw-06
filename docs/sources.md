# Використані матеріали

За основу взято [архів зі сторінками GoIT](https://drive.google.com/file/d/19JPeOCRcW8qG90xW4bCl7A2XvqSfmkpG/view).
Із нього збережено логотип і структуру сторінок. Тексти форми перекладено українською,
додано перевірку полів і повідомлення про результат. Замість початкового JSON-сховища використовується MongoDB.

HTML до міграції лежить у `frontend/reference/`, еталони для візуальних тестів —
у `frontend/tests/fixtures/`. Робочі сторінки генерує Next.js.
Bootstrap 5.2.2 залишився лише в еталонах; його MIT-ліцензія та авторство збережені
у `frontend/tests/fixtures/LICENSE-bootstrap.txt` і CSS.
Swagger UI постачається локально разом із LICENSE/NOTICE, які копіює build.

Документація використаних інструментів:

- [Python: HTTPServer](https://docs.python.org/3.13/library/http.server.html),
  [socketserver](https://docs.python.org/3.13/library/socketserver.html),
  [multiprocessing](https://docs.python.org/3.13/library/multiprocessing.html)
- [PyMongo: запис документів](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/insert/)
- [Docker Compose](https://docs.docker.com/compose/how-tos/startup-order/)
  і [Distroless](https://github.com/GoogleContainerTools/distroless)
- [Next.js static export](https://nextjs.org/docs/app/guides/static-exports)
- [Base UI Form](https://base-ui.com/react/components/form) і
  [DirectionProvider](https://base-ui.com/react/utils/direction-provider)
- [Playwright](https://playwright.dev/docs/test-configuration)
