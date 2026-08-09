import { useRef, useEffect } from "react";
import { isImageFile, isMarkdownFile } from "../lib/language";
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
  const taRef = useRef<HTMLTextAreaElement>(null);

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
        <div className="prose prose-invert prose-sm mx-auto max-w-3xl p-6">
          <MarkdownRenderer>{content}</MarkdownRenderer>
        </div>
      </div>
    );
  }

  // ── Code editor (textarea) ─────────────────────────────────────
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      {/* line-number gutter */}
      <div
        className="select-none overflow-hidden whitespace-nowrap py-3 pr-2 pl-3 text-right font-mono text-[13px] leading-[1.6] text-[var(--color-text-dim)]"
        style={{ pointerEvents: "none" }}
      >
        {content.split("\n").map((_, i) => (
          <div key={i}>{i + 1}</div>
        ))}
      </div>
      <textarea
        ref={taRef}
        value={content}
        readOnly={readOnly}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "s") {
            e.preventDefault();
            onSave();
          }
          if (e.key === "Tab") {
            e.preventDefault();
            const ta = e.currentTarget;
            const s = ta.selectionStart;
            const en = ta.selectionEnd;
            const newVal = content.slice(0, s) + "  " + content.slice(en);
            onChange(newVal);
            requestAnimationFrame(() => {
              ta.selectionStart = ta.selectionEnd = s + 2;
            });
          }
        }}
        onScroll={(e) => {
          // Sync gutter scroll with textarea
          const gutter = (e.currentTarget.previousElementSibling as HTMLElement);
          if (gutter) gutter.scrollTop = e.currentTarget.scrollTop;
        }}
        spellCheck={false}
        className="flex-1 resize-none overflow-auto border-0 bg-transparent px-3 py-3 font-mono text-[13px] leading-[1.6] text-[var(--color-text)] outline-none scrollbar-thin"
      />
    </div>
  );
}
