/// <reference types="vite/client" />

// Environment values Vite builds into the bundle. Public: never put a secret in one.
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
