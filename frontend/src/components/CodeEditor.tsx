import CodeMirror from "@uiw/react-codemirror";
import { githubDark } from "@uiw/codemirror-theme-github";
import { EditorView } from "@codemirror/view";
import { languageFromPath, isImageFile, isMarkdownFile } from "../lib/language";
import { rawUrl } from "../lib/filesApi";
import { MarkdownRenderer } from "./MarkdownRenderer";

interface CodeEditorProps {
  path: string;
  content: string;
  readOnly: boolean;
  onChange: (value: string) => void;
  onSave: () => void;
  /** When true, render markdown as rendered HTML instead of source */
  markdownPreview?: boolean;
}

export function CodeEditor({ path, content, readOnly, onChange, onSave, markdownPreview }: CodeEditorProps) {
  // ── Image preview ──────────────────────────────────────────────
  if (isImageFile(path)) {
    return (
      <div className="flex flex-1 min-h-0 items-center justify-center overflow-auto bg-[var(--color-bg)] p-8">
        <img
          src={rawUrl(path)}
          alt={path}
          className="max-h-full max-w-full rounded-lg object-contain"
        />
      </div>
    );
  }

  // ── Markdown preview toggle ────────────────────────────────────
  if (markdownPreview && isMarkdownFile(path)) {
    return (
      <div className="flex-1 min-h-0 overflow-auto bg-[var(--color-bg)] scrollbar-thin">
        <div className="mx-auto max-w-3xl p-6">
          <MarkdownRenderer content={content} />
        </div>
      </div>
    );
  }

  // ── Code editor ────────────────────────────────────────────────
  // CodeMirror needs a wrapping div with min-h-0 + overflow-hidden so
  // the flex child can actually resolve its height inside a flex column.
  // Without min-h-0, the editor's intrinsic content height wins and the
  // container collapses, showing a blank pane.
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
      <CodeMirror
        value={content}
        readOnly={readOnly}
        theme={githubDark}
        extensions={extensions}
        height="100%"
        style={{ height: "100%", flex: "1 1 0%", minHeight: "0" }}
        onChange={onChange}
        onCreateEditor={(view) => {
          view.domElement.addEventListener("keydown", (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "s") {
              e.preventDefault();
              onSave();
            }
          });
        }}
      />
    </div>
  );
}
