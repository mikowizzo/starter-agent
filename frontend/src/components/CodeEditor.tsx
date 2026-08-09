import { Suspense, Component, use, type ReactNode } from "react";
import { isImageFile, isMarkdownFile } from "../lib/language";
import { rawUrl } from "../lib/filesApi";
import { MarkdownRenderer } from "./MarkdownRenderer";

interface CodeEditorProps {
  path: string;
  content: string;
  readOnly: boolean;
  onChange: (value: string) => void;
  onSave: () => void;
  markdownPreview?: boolean;
}

// ── Error Boundary: catches CodeMirror crash → fallback to textarea ─
class EditorBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };
  static getDerivedStateFromError() { return { hasError: true }; }
  componentDidCatch(err: Error) { console.error("[CodeEditor] CodeMirror crashed:", err); }
  render() { return this.state.hasError ? this.props.fallback : this.props.children; }
}

export function CodeEditor({ path, content, readOnly, onChange, onSave, markdownPreview }: CodeEditorProps) {
  // ── Image preview ──────────────────────────────────────────────
  if (isImageFile(path)) {
    return (
      <div className="flex flex-1 min-h-0 items-center justify-center overflow-auto bg-[var(--color-bg)] p-8">
        <img src={rawUrl(path)} alt={path} className="max-h-full max-w-full rounded-lg object-contain" />
      </div>
    );
  }

  // ── Markdown preview ───────────────────────────────────────────
  if (markdownPreview && isMarkdownFile(path)) {
    return (
      <div className="flex-1 min-h-0 overflow-auto bg-[var(--color-bg)] scrollbar-thin">
        <div className="mx-auto max-w-3xl p-6">
          <MarkdownRenderer>{content}</MarkdownRenderer>
        </div>
      </div>
    );
  }

  // ── Textarea fallback (always works, no deps) ──────────────────
  const textareaEl = (
    <textarea
      value={content}
      readOnly={readOnly}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); onSave(); }
      }}
      spellCheck={false}
      className="flex-1 min-h-0 w-full resize-none border-0 bg-[var(--color-bg)] p-4 font-mono text-[13px] leading-relaxed text-[var(--color-text)] outline-none scrollbar-thin"
    />
  );

  // ── Code editor (CodeMirror with error boundary + textarea fallback) ──
  return (
    <EditorBoundary fallback={textareaEl}>
      <Suspense fallback={textareaEl}>
        <CodeMirrorEditor
          path={path}
          content={content}
 readOnly={readOnly}
          onChange={onChange}
          onSave={onSave}
        />
      </Suspense>
    </EditorBoundary>
  );
}

// ── Load CodeMirror dynamically ────────────────────────────────────
const cmPromise = Promise.all([
  import("@uiw/react-codemirror"),
  import("@uiw/codemirror-theme-github"),
  import("@codemirror/view"),
  import("../lib/language-cm"),
]).then(([cm, theme, view, lang]) => ({
  CM: cm.default,
  githubDark: theme.githubDark,
  EditorView: view.EditorView,
  languageFromPath: lang.languageFromPath,
}));

function CodeMirrorEditor({ path, content, readOnly, onChange, onSave }: Omit<CodeEditorProps, "markdownPreview">) {
  const { CM, githubDark, EditorView, languageFromPath } = use(cmPromise);

  const extensions = [
    ...languageFromPath(path),
    EditorView.lineWrapping,
    EditorView.theme({
      "&": { fontSize: "13px", height: "100%" },
      ".cm-scroller": { fontFamily: "monospace", overflow: "auto" },
      ".cm-gutters": { backgroundColor: "transparent", border: "none" },
    }),
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <CM
        value={content}
        readOnly={readOnly}
        theme={githubDark}
        extensions={extensions}
        height="100%"
        style={{ height: "100%", flex: "1 1 0%", minHeight: "0" }}
        onChange={onChange}
        onCreateEditor={(view: any) => {
          view.domElement.addEventListener("keydown", (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); onSave(); }
          });
        }}
      />
    </div>
  );
}
