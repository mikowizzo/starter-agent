// CodeMirror language extensions - isolated in this module so they
// only load when the CodeEditor actually needs them (lazy loading).
import { javascript } from "@codemirror/lang-javascript";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { yaml } from "@codemirror/lang-yaml";
import type { Extension } from "@codemirror/state";

export function languageFromPath(path: string): Extension[] {
  const ext = path.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "ts":
    case "tsx":
      return [javascript({ typescript: true, jsx: true })];
    case "js":
    case "jsx":
    case "mjs":
    case "cjs":
      return [javascript({ jsx: true })];
    case "py":
      return [python()];
    case "html":
    case "htm":
    case "svg":
      return [html()];
    case "css":
    case "scss":
    case "sass":
      return [css()];
    case "json":
      return [json()];
    case "md":
    case "markdown":
      return [markdown()];
    case "yml":
    case "yaml":
      return [yaml()];
    default:
      return [];
  }
}
