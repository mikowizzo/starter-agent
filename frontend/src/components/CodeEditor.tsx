import { useRef, useEffect, useMemo, useCallback } from "react";
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
  /** When true, file has unsaved changes (used for save button state) */
  isDirty?: boolean;
}

export function CodeEditor({ path, content, readOnly, onChange, onSave, markdownPreview, isDirty }: CodeEditorProps) {
  const taRef = useRef<HTMLTextAreaElement>(null);

  // ── Insert text at cursor position ─────────────────────────────
  const insertAtCursor = useCallback(
    (text: string) => {
      const ta = taRef.current;
      if (!ta) return;
      const s = ta.selectionStart;
      const en = ta.selectionEnd;
      const newVal = content.slice(0, s) + text + content.slice(en);
      onChange(newVal);
      requestAnimationFrame(() => {
        ta.focus();
        ta.selectionStart = ta.selectionEnd = s + text.length;
      });
    },
    [content, onChange],
  );

  // ── Image preview ──────────────────────────────────────────────
  if (isImageFile(path)) {
    return (
      <div className="flex flex-1 min-h-0 items-center justify-center overflow-auto bg-[var(--color-bg)] p-4 sm:p-8">
        <img
          src={rawUrl(path)}
          alt={path}
          loading="lazy"
          className="max-h-full max-w-full rounded-lg object-contain"
          style={{ touchAction: "pinch-zoom" }}
        />
      </div>
    );
  }

  // ── Markdown preview toggle ────────────────────────────────────
  if (markdownPreview && isMarkdownFile(path)) {
    return (
      <div className="flex-1 min-h-0 overflow-auto bg-[var(--color-bg)] scrollbar-thin">
        <div
          className="prose prose-invert prose-sm mx-auto max-w-3xl p-4 sm:p-6"
          style={{ WebkitTextSizeAdjust: "100%" }}
        >
          <MarkdownRenderer>{content}</MarkdownRenderer>
        </div>
      </div>
    );
  }

  // ── Line numbers (memoized — avoids re-splitting every keystroke) ──
  const lineCount = useMemo(() => content.split("\n").length, [content]);

  // ── Code editor (textarea) ─────────────────────────────────────
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* line-number gutter — hidden on narrow screens */}
        <div
          className="code-gutter select-none overflow-hidden whitespace-nowrap py-3 pr-2 pl-3 text-right font-mono text-[13px] leading-[1.6] text-[var(--color-dim)] hidden sm:block"
          style={{ pointerEvents: "none" }}
        >
          {Array.from({ length: lineCount }, (_, i) => (
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
              insertAtCursor("  ");
            }
          }}
          onScroll={(e) => {
            // Sync gutter scroll with textarea
            const gutter = e.currentTarget.previousElementSibling as HTMLElement;
            if (gutter) gutter.scrollTop = e.currentTarget.scrollTop;
          }}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          data-gramm="false"
          className="code-textarea flex-1 resize-none overflow-auto border-0 bg-transparent px-3 py-3 font-mono text-[13px] leading-[1.6] text-[var(--color-text)] outline-none scrollbar-thin"
          style={{
            overscrollBehavior: "contain",
            WebkitOverflowScrolling: "touch",
            touchAction: "pan-x pan-y",
          }}
        />
      </div>

      {/* ── Mobile accessory toolbar ─────────────────────────────── */}
      {/* Tab key, brackets, and other characters missing from virtual
          keyboards. Also provides a visible Save button since there's
          no Cmd/Ctrl+S on touch devices. */}
      {!readOnly && (
        <div className="flex gap-1 overflow-x-auto border-t border-[var(--color-border)] bg-[var(--color-panel)] px-2 py-1 md:hidden">
          {["Tab", "(", ")", "{", "}", "[", "]", "=", ";", ":", "/", "<", ">", '"', "'"].map(
            (k) => (
              <button
                key={k}
                type="button"
                className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded font-mono text-sm text-[var(--color-text)] transition active:scale-95 active:bg-[var(--color-border)]"
                onPointerDown={(e) => e.preventDefault()}
                onClick={() => insertAtCursor(k === "Tab" ? "  " : k)}
              >
                {k}
              </button>
            ),
          )}
          <button
            type="button"
            className="flex min-h-[44px] shrink-0 items-center justify-center gap-1.5 rounded px-4 font-medium text-sm text-[var(--color-text)] transition active:scale-95 active:bg-[var(--color-border)]"
            onPointerDown={(e) => e.preventDefault()}
            onClick={onSave}
            disabled={!isDirty}
            style={{ opacity: isDirty ? 1 : 0.3 }}
          >
            Save
          </button>
        </div>
      )}
    </div>
  );
}
