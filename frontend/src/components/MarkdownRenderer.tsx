import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// module-level: stable identities so streaming tokens don't remount + reset scrollLeft.
const components: Components = {
  table: ({ node, ...props }) => (
    <div className="overflow-x-auto overscroll-x-contain">
      <table className="min-w-max" {...props} />
    </div>
  ),
};

function MarkdownRendererImpl({ children }: { children: string }) {
  return (
    <Markdown remarkPlugins={[remarkGfm]} components={components}>
      {children}
    </Markdown>
  );
}

export const MarkdownRenderer = memo(MarkdownRendererImpl);
