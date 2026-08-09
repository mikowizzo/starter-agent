import { useRef } from "react";
import { isImageFile, isMarkdownFile } from "../lib/language";
import { rawUrl } from "../lib/filesApi";
import { MarkdownRenderer } from "./MarkdownRenderer";

interface CodeEditorProps {
  path: string;
  content: string;
  readOnly?: boolean;
  onChange?: (value: string) => void;
  onSave?: () => void;
  markdownPreview?: boolean;
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

  // ── Text editor ────────────────────────────────────────────────
  const lineCount = content.split("\n").length;
  const gutterRef = useRef<HTMLDivElement>(null);

  const syncScroll = (e: React.UIEvent<HTMLTextAreaElement>) => {
    if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
  };

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden bg-[var(--color-bg)]">
      <div
        ref={gutterRef}
        className="select-none overflow-hidden border-r border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-3 py-4 text-right font-mono text-[13px] leading-relaxed text-[var(--color-text-secondary)]"
      >
        {Array.from({ length: lineCount }, (_, i) => (
          <div key={i}>{i + 1}</div>
        ))}
      </div>
      <textarea
        value={content}
        readOnly={readOnly}
        onChange={(e) => onChange?.(e.target.value)}
        onScroll={syncScroll}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); onSave?.(); }
          if (e.key === "Tab") {
            e.preventDefault();
            const t = e.currentTarget;
            const s = t.selectionStart, en = t.selectionEnd;
            const next = content.slice(0, s) + "  " + content.slice(en);
            onChange?.(next);
            requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = s + 2; });
          }
        }}
        spellCheck={false}
        wrap="off"
        className="min-h-0 flex-1 resize-none border-0 bg-transparent p-4 font-mono text-[13px] leading-relaxed text-[var(--color-text)] outline-none scrollbar-thin"
      />
    </div>
  );
}
