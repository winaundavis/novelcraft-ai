interface ImportMetaEnv {
  readonly VITE_BETA_AUTH_REQUIRED?: string;
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
