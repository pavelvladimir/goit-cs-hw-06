import { copyFile } from 'node:fs/promises'

// Keep the assignment's required filename for Python's 404 response.
await copyFile(new URL('../out/404.html', import.meta.url), new URL('../out/error.html', import.meta.url))
