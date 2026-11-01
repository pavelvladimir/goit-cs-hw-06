import tailwind from '@tailwindcss/postcss'
import { readFile, writeFile } from 'node:fs/promises'
import postcss from 'postcss'

const source = new URL('../src/style.css', import.meta.url)
const target = new URL('../public/style.css', import.meta.url)
const css = await readFile(source, 'utf8')
const result = await postcss([tailwind()]).process(css, { from: source.pathname, to: target.pathname })
await writeFile(target, result.css)
