import { useRef, useEffect, useCallback, useState } from "react";
import { Tree, NodeApi, TreeApi } from "react-arborist";
import {
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  FilePlus,
  FolderPlus,
  Trash2,
  Pencil,
} from "lucide-react";
import type { TreeNode, FileKind } from "../lib/filesApi";

interface FileTreeProps {
  data: TreeNode[];
  selectedPath: string | null;
  dirtyPaths: Set<string>;
  onSelect: (path: string) => void;
  onRename: (oldPath: string, newName: string) => void;
  onCreate: (parentPath: string, name: string, kind: FileKind) => void;
  onDelete: (path: string) => void;
  onMove: (src: string, dst: string) => void;
}

// ── Custom row renderer for react-arborist ─────────────────────────

function Row({
  node,
  style,
  dragHandle,
  selectedPath,
  dirtyPaths,
  onSelect,
}: {
  node: NodeApi<TreeNode>;
  style: React.CSSProperties;
  dragHandle?: (el: HTMLElement | null) => void;
  selectedPath: string | null;
  dirtyPaths: Set<string>;
  onSelect: (path: string) => void;
}) {
  const isDir = node.data.kind === "dir";
  const isSelected = node.data.id === selectedPath;
  const isDirty = dirtyPaths.has(node.data.id);
  const isEditing = node.isEditing;

  return (
    <div
      ref={dragHandle}
      style={style}
      onClick={() => !isEditing && onSelect(node.data.id)}
      className={`flex items-center gap-1 whitespace-nowrap rounded-md px-2 py-0.5 text-[13px] transition ${
        isSelected
          ? "bg-[var(--color-accent)]/15 text-[var(--color-accent)]"
          : "text-[var(--color-text)] hover:bg-[var(--color-border)]/40"
      }`}
    >
      {isDir ? (
        <button
          onClick={(e) => {
            e.stopPropagation();
            node.toggle();
          }}
          className="shrink-0"
        >
          <ChevronRight
            className={`h-3.5 w-3.5 transition-transform ${node.isOpen ? "rotate-90" : ""}`}
          />
        </button>
      ) : (
        <span className="inline-block w-3.5 shrink-0" />
      )}
      {isDir ? (
        node.isOpen ? (
          <FolderOpen className="h-3.5 w-3.5 shrink-0 text-[var(--color-dim)]" />
        ) : (
          <Folder className="h-3.5 w-3.5 shrink-0 text-[var(--color-dim)]" />
        )
      ) : (
        <FileIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-dim)]" />
      )}
      {isEditing ? (
        <input
          ref={(el) => { node.editRef.current = el; }}
          defaultValue={node.data.name}
          className="flex-1 bg-transparent text-[13px] text-[var(--color-text)] outline-none"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.stopPropagation();
              node.submit(e.currentTarget.value);
            }
            if (e.key === "Escape") {
              e.stopPropagation();
              node.reset();
            }
          }}
          onBlur={() => {
            const val = (node.editRef.current as HTMLInputElement | null)?.value;
            node.submit(val ?? node.data.name);
          }}
          autoFocus
        />
      ) : (
        <>
          <span className="truncate">{node.data.name}</span>
          {isDirty && <span className="text-[var(--color-accent)]">•</span>}
        </>
      )}
    </div>
  );
}

// ── FileTree component ─────────────────────────────────────────────

export function FileTree({
  data,
  selectedPath,
  dirtyPaths,
  onSelect,
  onRename,
  onCreate,
  onDelete,
  onMove,
}: FileTreeProps) {
  const treeRef = useRef<TreeApi<TreeNode>>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState(400);

  // Measure container height for react-arborist virtualization
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setHeight(entry.contentRect.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Keyboard shortcut for delete — only when NOT editing text
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      // Ignore if focus is in an input, textarea, or CodeMirror
      if (target.closest("input, textarea, [contenteditable], .cm-editor")) return;
      if (e.key === "Delete" && selectedPath) {
        onDelete(selectedPath);
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [selectedPath, onDelete]);

  // react-arborist v3 passes a single args object to onMove
  const handleMove = useCallback(
    ({ dragNodes, parentNode }: { dragNodes: NodeApi<TreeNode>[]; parentNode: NodeApi<TreeNode> | null }) => {
      const dragged = dragNodes[0];
      if (!dragged) return;
      const parentPath = parentNode?.data.id ?? "";
      const newPath = parentPath ? `${parentPath}/${dragged.data.name}` : dragged.data.name;
      if (newPath !== dragged.data.id) {
        onMove(dragged.data.id, newPath);
      }
    },
    [onMove],
  );

  // react-arborist v3 passes { id, name } to onRename
  const handleRename = useCallback(
    ({ id, name }: { id: string; name: string }) => {
      if (name && name !== "") {
        const node = findNode(data, id);
        if (node && name !== node.name) {
          onRename(id, name);
        }
      }
    },
    [onRename, data],
  );

  // Find parent path for create actions
  const parentPathForCreate = useCallback((): string => {
    if (!selectedPath) return "";
    const node = findNode(data, selectedPath);
    if (node?.kind === "dir") return selectedPath;
    const slashIdx = selectedPath.lastIndexOf("/");
    return slashIdx >= 0 ? selectedPath.slice(0, slashIdx) : "";
  }, [selectedPath, data]);

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-1 border-b border-[var(--color-border)] px-2 py-1.5">
        <button
          title="New file"
          onClick={() => {
            const name = prompt("New file name:");
            if (name) onCreate(parentPathForCreate(), name, "file");
          }}
          className="rounded p-1 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-[var(--color-accent)]"
        >
          <FilePlus className="h-3.5 w-3.5" />
        </button>
        <button
          title="New folder"
          onClick={() => {
            const name = prompt("New folder name:");
            if (name) onCreate(parentPathForCreate(), name, "dir");
          }}
          className="rounded p-1 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-[var(--color-accent)]"
        >
          <FolderPlus className="h-3.5 w-3.5" />
        </button>
        <div className="flex-1" />
        <button
          title="Rename"
          disabled={!selectedPath}
          onClick={() => selectedPath && treeRef.current?.edit(selectedPath)}
          className="rounded p-1 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-[var(--color-accent)] disabled:opacity-30"
        >
          <Pencil className="h-3.5 w-3.5" />
        </button>
        <button
          title="Delete"
          disabled={!selectedPath}
          onClick={() => selectedPath && onDelete(selectedPath)}
          className="rounded p-1 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-red-400 disabled:opacity-30"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Tree */}
      <div ref={wrapRef} className="flex-1 overflow-hidden py-1 scrollbar-thin">
        <Tree
          ref={treeRef as any}
          data={data}
          openByDefault={false}
          width="100%"
          height={height}
          rowHeight={28}
          indent={12}
          onMove={handleMove}
          onRename={handleRename}
          disableDrop={({ parentNode }: { parentNode: NodeApi<TreeNode> | null }) =>
            parentNode != null && parentNode.data.kind !== "dir"
          }
        >
          {(props) => (
            <Row
              {...props}
              selectedPath={selectedPath}
              dirtyPaths={dirtyPaths}
              onSelect={onSelect}
            />
          )}
        </Tree>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function findNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.id === path) return n;
    if (n.children) {
      const found = findNode(n.children, path);
      if (found) return found;
    }
  }
  return null;
}
