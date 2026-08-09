// ── Language detection (no CodeMirror imports) ────────────────────
// CodeMirror language extensions live in language-cm.ts and are only
// loaded when the editor mounts. This module provides type detection
// utilities that don't need CodeMirror.

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
