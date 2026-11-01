import { copyFile, mkdir } from 'node:fs/promises'

const target = new URL('../public/vendor/swagger/', import.meta.url)
await mkdir(target, { recursive: true })
for (const filename of ['swagger-ui-bundle.js', 'swagger-ui.css', 'LICENSE', 'NOTICE']) {
  await copyFile(new URL(`../node_modules/swagger-ui-dist/${filename}`, import.meta.url), new URL(filename, target))
}
