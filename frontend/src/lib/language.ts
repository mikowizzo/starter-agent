// ── Language detection for CodeMirror extensions ──────────────────
// Maps file extensions to CodeMirror language packages.

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

export function isImageFile(path: string): boolean {
  const ext = path.split(".").pop()?.toLowerCase();
  return ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"].includes(ext ?? "");
}

export function isMarkdownFile(path: string): boolean {
  const ext = path.split(".").pop()?.toLowerCase();
  return ext === "md" || ext === "markdown";
}

export function languageLabel(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase();
  const labels: Record<string, string> = {
    ts: "TypeScript", tsx: "TSX", js: "JavaScript", jsx: "JSX",
    py: "Python", html: "HTML", css: "CSS", json: "JSON",
    md: "Markdown", yml: "YAML", yaml: "YAML",
    sh: "Shell", bash: "Shell", sql: "SQL",
  };
  return labels[ext ?? ""] ?? (ext ? ext.toUpperCase() : "Text");
}
