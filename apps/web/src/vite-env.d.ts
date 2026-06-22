/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CONTROL_PLANE_URL?: string;
  readonly VITE_INFERENCE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
