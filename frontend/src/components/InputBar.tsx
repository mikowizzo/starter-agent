import { useState, useRef } from "react";
import { Send, Square, Paperclip, X, FileText } from "lucide-react";

// ── Input Bar ──────────────────────────────────────────────────────

export function InputBar({
  onSend,
  onStop,
  hasActiveRun,
  disabled,
}: {
  onSend: (text: string, files?: File[]) => void;
  onStop: () => void;
  hasActiveRun: boolean;
  disabled: boolean;
}) {
  const [input, setInput] = useState("");
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleSend() {
    if ((!input.trim() && selectedFiles.length === 0) || disabled) return;
    onSend(input, selectedFiles.length ? selectedFiles : undefined);
    setInput("");
    setSelectedFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.blur();
    }
  }

  return (
    <div className="pt-1">
      <div className="relative flex flex-col rounded-2xl border border-[var(--color-border)] bg-[var(--color-panel)] focus-within:border-[var(--color-accent)]">
        {/* File chips */}
        {selectedFiles.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2.5">
            {selectedFiles.map((f, i) => (
              <span
                key={i}
                className="flex items-center gap-1.5 rounded-lg bg-[var(--color-bg)] border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-text)]"
              >
                <FileText className="h-3 w-3 shrink-0 text-[var(--color-dim)]" />
                <span className="max-w-[160px] truncate">{f.name}</span>
                <button
                  onClick={() =>
                    setSelectedFiles((prev) => prev.filter((_, idx) => idx !== i))
                  }
                  className="text-[var(--color-dim)] hover:text-[var(--color-text)]"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="relative flex items-end">
          {/* Paperclip */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            className="absolute left-3 bottom-3.5 flex h-8 w-8 items-center justify-center rounded-lg text-[var(--color-dim)] hover:text-[var(--color-text)] transition active:scale-[0.95] disabled:opacity-40"
          >
            <Paperclip className="h-5 w-5" />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              const newFiles = Array.from(e.target.files ?? []);
              setSelectedFiles((prev) => [...prev, ...newFiles]);
              if (fileInputRef.current) fileInputRef.current.value = "";
            }}
          />

          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = e.target.scrollHeight + "px";
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder="Baka! What do you want?!"
            disabled={disabled}
            rows={1}
            className="max-h-36 min-h-[60px] w-full resize-none overflow-y-auto border-0 bg-transparent px-14 pr-14 py-4 text-[16px] text-[var(--color-text)] placeholder:text-[var(--color-dim)] focus:outline-none disabled:opacity-50"
          />

          {/* Send / Stop button */}
          <button
            onClick={hasActiveRun ? onStop : handleSend}
            disabled={!hasActiveRun && !input.trim() && selectedFiles.length === 0}
            className={`absolute right-3 bottom-3.5 flex h-8 w-8 items-center justify-center rounded-lg text-white hover:brightness-110 transition active:scale-[0.95] disabled:cursor-not-allowed disabled:opacity-40 ${
              hasActiveRun
                ? "bg-[var(--color-pink)] hover:brightness-110"
                : "bg-[var(--color-accent)]"
            }`}
          >
            {hasActiveRun ? (
              <Square className="h-3.5 w-3.5 fill-current" />
            ) : (
              <Send className="h-5 w-5" />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
