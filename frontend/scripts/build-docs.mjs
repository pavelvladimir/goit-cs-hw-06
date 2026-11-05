import { copyFile, mkdir, readFile } from 'node:fs/promises'

const assets = JSON.parse(await readFile(new URL('../published-assets.json', import.meta.url), 'utf8'))
for (const asset of assets) {
  const target = new URL(`../public/${asset.destination}/`, import.meta.url)
  await mkdir(target, { recursive: true })
  for (const filename of asset.files) {
    await copyFile(new URL(`../node_modules/${asset.package}/${filename}`, import.meta.url), new URL(filename, target))
  }
}
