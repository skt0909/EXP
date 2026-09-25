import { defineConfig } from 'vitest/config'

// Kept separate from vite.config.js on purpose -- that file configures the
// actual app build/dev-server and shouldn't gain test-only concerns just
// because Vitest happens to share Vite's config shape.
export default defineConfig({
  test: {
    environment: 'node',
  },
})
