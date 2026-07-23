import { useState, useRef } from "react";
import { Send, Square } from "lucide-react";

// ── Input Bar ──────────────────────────────────────────────────────

export function InputBar({
  onSend,
  onStop,
  hasActiveRun,
  disabled,
}: {
  onSend: (text: string) => void;
  onStop: () => void;
  hasActiveRun: boolean;
  disabled: boolean;
}) {
  const [input, setInput] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function handleSend() {
    if (!input.trim() || disabled) return;
    onSend(input);
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.blur();
    }
  }

  return (
    <div className="pt-1">
      <div className="relative flex flex-col rounded-2xl border border-[var(--color-border)] bg-[var(--color-panel)] focus-within:border-[var(--color-accent)]">
        <div className="relative flex items-end">
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
            className="max-h-36 min-h-[60px] w-full resize-none overflow-y-auto border-0 bg-transparent px-4 pr-14 py-4 text-[16px] text-[var(--color-text)] placeholder:text-[var(--color-dim)] focus:outline-none disabled:opacity-50"
          />

          {/* Send / Stop button */}
          <button
            onClick={hasActiveRun ? onStop : handleSend}
            disabled={!hasActiveRun && !input.trim()}
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
