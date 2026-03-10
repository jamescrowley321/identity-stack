/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DESCOPE_PROJECT_ID: string;
  readonly VITE_DESCOPE_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
