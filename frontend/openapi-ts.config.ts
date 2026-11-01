import { defineConfig } from '@hey-api/openapi-ts'

export default defineConfig({
  input: './openapi.json',
  output: 'src/api/generated',
  plugins: [
    '@hey-api/typescript',
    // Requests run only in the browser; no Next.js server cache is involved.
    '@hey-api/client-fetch',
    'zod',
    { name: '@hey-api/sdk', validator: { response: 'zod' } },
  ],
})
