import { useState, useEffect, useCallback } from "react";
import { Loader2, X, Save, RotateCcw, AlertCircle, FileText, Eye, Pencil, Download, Menu, Folder } from "lucide-react";
import { FileTree } from "./FileTree";
import { CodeEditor } from "./CodeEditor";
import {
  fetchTree,
  readFile,
  writeFile,
  moveFile,
  mkdir,
  deleteFile,
  rawUrl,
  buildTreeData,
  type TreeNode,
  type FileKind,
} from "../lib/filesApi";
import { languageLabel, isImageFile, isMarkdownFile } from "../lib/language";

interface FileExplorerProps {
  open: boolean;
  onClose: () => void;
}

// ── Open file buffer ────────────────────────────────────────────────

interface FileBuffer {
  path: string;
  content: string;
  savedContent: string;
  mtimeNs: string | null;
  loading: boolean;
  error: string | null;
  isBinary: boolean;
}

export function FileExplorer({ open, onClose }: FileExplorerProps) {
  const [treeData, setTreeData] = useState<TreeNode[]>([]);
  const [treeLoading, setTreeLoading] = useState(true);
  const [treeTruncated, setTreeTruncated] = useState(false);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [buffer, setBuffer] = useState<FileBuffer | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [markdownPreview, setMarkdownPreview] = useState(true); // default to preview mode
  const [sidebarOpen, setSidebarOpen] = useState(true); // mobile drawer — open by default so the tree is visible
  // ── Track virtual keyboard height via visualViewport ──────────
  // When the mobile keyboard opens, the layout viewport shrinks.
  // We expose the offset as a CSS variable so floating elements can
  // sit above the keyboard.
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const onResize = () => {
      const offset = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.documentElement.style.setProperty("--keyboard-offset", `${offset}px`);
    };
    vv.addEventListener("resize", onResize);
    return () => {
      vv.removeEventListener("resize", onResize);
      document.documentElement.style.setProperty("--keyboard-offset", "0px");
    };
  }, []);

  // ── Tree loading ────────────────────────────────────────────────

  const refreshTree = useCallback(async () => {
    try {
      const data = await fetchTree();
      setTreeData(buildTreeData(data.entries));
      setTreeTruncated(data.truncated);
    } catch (e: any) {
      setActionError(e.message);
    } finally {
      setTreeLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) refreshTree();
  }, [open, refreshTree]);

  // Auto-refresh tree every 5s while the explorer is open and visible
  useEffect(() => {
    if (!open) return;
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") refreshTree();
    }, 5000);
    return () => clearInterval(interval);
  }, [open, refreshTree]);

  // ── ESC to close (but not while editing in the tree or editor) ──

  useEffect(() => {
    if (!open) return;
    function handleEsc(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      const target = e.target as HTMLElement;
      // Don't close if user is editing (rename input, etc.)
      if (target.closest("input, textarea, [contenteditable], .cm-editor")) return;
      if (buffer && buffer.content !== buffer.savedContent) {
        if (!confirm("Discard unsaved changes?")) return;
      }
      onClose();
    }
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [open, onClose, buffer]);

  // ── Dirty check helper ──────────────────────────────────────────

  const isDirty = buffer != null && buffer.content !== buffer.savedContent;

  const confirmDiscardIfDirty = useCallback((): boolean => {
    if (isDirty && !confirm("Discard unsaved changes?")) return false;
    return true;
  }, [isDirty]);

  // ── Select a file (load its content) ────────────────────────────

  const handleSelect = useCallback(
    async (path: string) => {
      // Don't navigate away from unsaved changes without confirm
      if (buffer && buffer.path !== path && !confirmDiscardIfDirty()) return;
      // Reset to preview mode when switching files
      setMarkdownPreview(true);

      const node = findTreeNode(treeData, path);
      if (node?.kind === "dir") {
        setSelectedPath(path);
        return;
      }

      // Images: skip the text read, go straight to preview
      if (isImageFile(path)) {
        setSelectedPath(path);
        setBuffer({
          path,
          content: "",
          savedContent: "",
          mtimeNs: null,
          loading: false,
          error: null,
          isBinary: true,
        });
        return;
      }

      setSelectedPath(path);
      setBuffer({ path, content: "", savedContent: "", mtimeNs: null, loading: true, error: null, isBinary: false });

      try {
        const result = await readFile(path);
        setBuffer((b) =>
          b?.path === path
            ? {
                path,
                content: result.content,
                savedContent: result.content,
                mtimeNs: result.mtime_ns,
                loading: false,
                error: null,
                isBinary: false,
              }
            : b,
        );
      } catch (e: any) {
        setBuffer((b) =>
          b?.path === path
            ? {
                path,
                content: "",
                savedContent: "",
                mtimeNs: null,
                loading: false,
                error:
                  e.code === "binary_file"
                    ? "Binary file — use download button to access it."
                    : e.message,
                isBinary: e.code === "binary_file",
              }
            : b,
        );
      }
    },
    [treeData, buffer, confirmDiscardIfDirty],
  );

  // ── Save ─────────────────────────────────────────────────────────

  const handleSave = useCallback(async () => {
    if (!buffer || buffer.content === buffer.savedContent) return;
    const savedContent = buffer.content;
    const savedPath = buffer.path;
    setSaving(true);
    setSaveError(null);
    try {
      const result = await writeFile(savedPath, savedContent, buffer.mtimeNs);
      setBuffer((b) =>
        b && b.path === savedPath
          ? { ...b, savedContent, mtimeNs: result.mtime_ns }
          : b,
      );
    } catch (e: any) {
      if (e.code === "conflict") {
        setSaveError(
          e.context?.reason === "stale_mtime"
            ? "File was modified by the agent. Reload to see the latest version."
            : e.context?.reason === "mtime_required"
              ? "File was created externally. Reload."
              : "File changed on disk. Please reload.",
        );
      } else {
        setSaveError(e.message);
      }
    } finally {
      setSaving(false);
    }
  }, [buffer]);

  // ── Reload ──────────────────────────────────────────────────────

  const handleReload = useCallback(async () => {
    if (!buffer) return;
    setBuffer({ ...buffer, loading: true });
    try {
      const result = await readFile(buffer.path);
      setBuffer({
        path: buffer.path,
        content: result.content,
        savedContent: result.content,
        mtimeNs: result.mtime_ns,
        loading: false,
        error: null,
        isBinary: false,
      });
      setSaveError(null);
    } catch (e: any) {
      setBuffer({ ...buffer, loading: false, error: e.message });
    }
  }, [buffer]);

  // ── Rename / Move ────────────────────────────────────────────────

  const handleRename = useCallback(
    async (oldPath: string, newName: string) => {
      setActionError(null);
      try {
        const slashIdx = oldPath.lastIndexOf("/");
        const parentPath = slashIdx >= 0 ? oldPath.slice(0, slashIdx + 1) : "";
        const newPath = parentPath + newName;
        if (newPath === oldPath) return;
        await moveFile(oldPath, newPath);
        await refreshTree();
        // Update buffer if it was the renamed file OR inside the renamed dir
        setBuffer((b) => {
          if (!b) return b;
          if (b.path === oldPath) return { ...b, path: newPath };
          if (b.path.startsWith(oldPath + "/")) {
            return { ...b, path: newPath + b.path.slice(oldPath.length) };
          }
          return b;
        });
        if (selectedPath === oldPath) setSelectedPath(newPath);
      } catch (e: any) {
        setActionError(`Rename failed: ${e.message}`);
      }
    },
    [selectedPath, refreshTree],
  );

  // ── Create ──────────────────────────────────────────────────────

  const handleCreate = useCallback(
    async (parentPath: string, name: string, kind: FileKind) => {
      setActionError(null);
      try {
        const fullPath = parentPath ? `${parentPath}/${name}` : name;
        if (kind === "dir") {
          await mkdir(fullPath);
        } else {
          await writeFile(fullPath, "", null);
        }
        await refreshTree();
      } catch (e: any) {
        setActionError(`Create failed: ${e.message}`);
      }
    },
    [refreshTree],
  );

  // ── Delete ──────────────────────────────────────────────────────

  const handleDelete = useCallback(
    async (path: string) => {
      setActionError(null);
      const node = findTreeNode(treeData, path);
      const isDir = node?.kind === "dir";
      const msg = isDir
        ? `Delete "${path}" and all its contents?`
        : `Delete "${path}"?`;
      if (!confirm(msg)) return;
      try {
        await deleteFile(path, isDir ?? false);
        // Clear buffer if it was the deleted file OR inside the deleted dir
        setBuffer((b) => {
          if (!b) return b;
          if (b.path === path || b.path.startsWith(path + "/")) return null;
          return b;
        });
        if (selectedPath === path || selectedPath?.startsWith(path + "/")) {
          setSelectedPath(null);
        }
        await refreshTree();
      } catch (e: any) {
        setActionError(`Delete failed: ${e.message}`);
      }
    },
    [treeData, selectedPath, refreshTree],
  );

  // ── Move (drag-drop) ─────────────────────────────────────────────

  const handleMove = useCallback(
    async (src: string, dst: string) => {
      setActionError(null);
      try {
        await moveFile(src, dst);
        await refreshTree();
        // Update buffer path if the open file was moved or was inside a moved dir
        setBuffer((b) => {
          if (!b) return b;
          if (b.path === src) return { ...b, path: dst };
          if (b.path.startsWith(src + "/")) return { ...b, path: dst + b.path.slice(src.length) };
          return b;
        });
      } catch (e: any) {
        setActionError(`Move failed: ${e.message}`);
        await refreshTree();
      }
    },
    [refreshTree],
  );

  // ── Close with unsaved-changes guard ────────────────────────────

  const handleClose = useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    onClose();
  }, [onClose, confirmDiscardIfDirty]);

  // ── Render ──────────────────────────────────────────────────────

  if (!open) return null;

  const dirtyPaths = new Set<string>();
  if (isDirty && buffer) dirtyPaths.add(buffer.path);

  const showMdToggle = buffer != null && isMarkdownFile(buffer.path);

  return (
    <div className="fixed inset-0 z-[90] flex flex-col bg-[var(--color-bg)]">
      {/* Top bar */}
      <div className="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-2.5">
        <FileText className="h-4 w-4 text-[var(--color-accent)]" />
        <span className="text-sm font-semibold text-[var(--color-text)]">Files</span>
        {actionError && (
          <span className="flex items-center gap-1 text-[11px] text-red-400">
            <AlertCircle className="h-3 w-3" />
            {actionError}
            <button onClick={() => setActionError(null)} className="ml-1 underline">
              dismiss
            </button>
          </span>
        )}
        {treeTruncated && (
          <span className="text-[11px] text-[var(--color-dim)]">
            ⚠️ Tree truncated (20k entry cap)
          </span>
        )}
        <div className="flex-1" />
        <button
          onClick={handleClose}
          className="rounded-lg p-1.5 text-[var(--color-dim)] transition hover:bg-[var(--color-border)] hover:text-[var(--color-text)]"
          title="Close"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Body: tree | editor */}
      <div className="flex min-h-0 flex-1">
        {/* Mobile sidebar backdrop */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-[80] bg-black/50 md:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar — always visible on desktop, drawer on mobile */}
        <aside
          className={`absolute z-[81] flex h-full w-64 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg)] transition-transform duration-200 md:static md:translate-x-0 ${
            sidebarOpen ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          {/* Mobile sidebar header */}
          <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2.5 md:hidden">
            <span className="flex items-center gap-2 text-sm font-semibold text-[var(--color-text)]">
              <Folder className="h-4 w-4 text-[var(--color-accent)]" />
              Files
            </span>
            <button
              onClick={() => setSidebarOpen(false)}
              className="rounded-lg p-1.5 text-[var(--color-dim)] transition hover:bg-[var(--color-border)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {treeLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="h-5 w-5 animate-spin text-[var(--color-dim)]" />
            </div>
          ) : treeData.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-[var(--color-dim)]">
              Workspace is empty.
              <br />
              Create a file to get started.
            </div>
          ) : (
            <FileTree
              data={treeData}
              selectedPath={selectedPath}
              dirtyPaths={dirtyPaths}
              onSelect={(p) => {
                handleSelect(p);
                setSidebarOpen(false); // close drawer on mobile after selecting
              }}
              onRename={handleRename}
              onCreate={handleCreate}
              onDelete={handleDelete}
              onMove={handleMove}
            />
          )}
        </aside>

        {/* Editor pane */}
        <main className="flex min-w-0 min-h-0 flex-1 flex-col">
          {/* Mobile: open-files button when no buffer or to switch files */}
          <button
            onClick={() => setSidebarOpen(true)}
            className="flex items-center gap-2 border-b border-[var(--color-border)] px-3 py-2 md:hidden"
          >
            <Menu className="h-4 w-4 text-[var(--color-accent)]" />
            <span className="text-sm text-[var(--color-dim)]">Browse files</span>
          </button>
          {buffer?.loading ? (
            <div className="flex flex-1 items-center justify-center">
              <Loader2 className="h-5 w-5 animate-spin text-[var(--color-dim)]" />
            </div>
          ) : buffer?.error && !buffer.isBinary ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
              <AlertCircle className="h-8 w-8 text-[var(--color-dim)]" />
              <p className="text-sm text-[var(--color-dim)]">{buffer.error}</p>
            </div>
          ) : buffer?.isBinary && !isImageFile(buffer.path) ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
              <AlertCircle className="h-8 w-8 text-[var(--color-dim)]" />
              <p className="text-sm text-[var(--color-dim)]">Binary file — use the download button to access it.</p>
            </div>
          ) : !buffer ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
              <FileText className="h-8 w-8 text-[var(--color-dim)]" />
              <p className="text-sm text-[var(--color-dim)]">Select a file to view or edit</p>
            </div>
          ) : (
            <>
              {/* File toolbar */}
              <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-3 py-2">
                <span className="truncate text-[13px] text-[var(--color-text)]">
                  {buffer.path}
                </span>
                {isDirty && (
                  <span className="text-[10px] text-[var(--color-accent)]">● unsaved</span>
                )}
                <div className="flex-1" />
                <span className="hidden text-[10px] text-[var(--color-dim)] sm:inline">
                  {languageLabel(buffer.path)}
                </span>

                {/* Download button */}
                <a
                  href={rawUrl(buffer.path) + "&download=true"}
                  download
                  className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-[var(--color-dim)] transition hover:text-[var(--color-accent)]"
                  title="Download"
                >
                  <Download className="h-3 w-3" />
                  Download
                </a>

                {/* Markdown preview toggle */}
                {showMdToggle && (
                  <button
                    onClick={() => setMarkdownPreview((v) => !v)}
                    className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-[var(--color-dim)] transition hover:text-[var(--color-accent)]"
                    title={markdownPreview ? "Edit source" : "Preview rendered"}
                  >
                    {markdownPreview ? <Pencil className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                    {markdownPreview ? "Edit" : "Preview"}
                  </button>
                )}

                {saveError && (
                  <span className="flex items-center gap-1 text-[10px] text-red-400">
                    <AlertCircle className="h-3 w-3" />
                    {saveError}
                  </span>
                )}
                {saveError && (
                  <button
                    onClick={handleReload}
                    title="Reload file"
                    className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-[var(--color-dim)] transition hover:text-[var(--color-accent)]"
                  >
                    <RotateCcw className="h-3 w-3" />
                    Reload
                  </button>
                )}

                {/* Save button — desktop only; mobile uses toolbar in CodeEditor */}
                {!buffer.isBinary && (
                  <button
                    onClick={handleSave}
                    disabled={!isDirty || saving}
                    className="hidden items-center gap-1.5 rounded-md px-3 py-1 text-[12px] font-medium text-[var(--color-text)] transition hover:text-[var(--color-accent)] active:scale-[0.97] disabled:opacity-30 md:flex"
                  >
                    {saving ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Save className="h-3.5 w-3.5" />
                    )}
                    Save
                  </button>
                )}
              </div>

              {/* Editor / Preview */}
              <CodeEditor
                path={buffer.path}
                content={buffer.content}
                readOnly={false}
                onChange={(val) =>
                  setBuffer((b) => (b ? { ...b, content: val } : b))
                }
                onSave={handleSave}
                markdownPreview={markdownPreview}
                isDirty={isDirty}
              />
            </>
          )}
        </main>
      </div>

      {/* Footer hint */}
      <div className="flex items-center justify-between border-t border-[var(--color-border)] px-4 py-1.5 text-[10px] text-[var(--color-dim)]">
        <span className="truncate">
          {buffer && !buffer.isBinary
            ? `${languageLabel(buffer.path)} · ${buffer.content.split("\n").length} lines`
            : buffer?.isBinary
              ? "Binary file"
              : "No file open"}
        </span>
        <span className="hidden sm:inline">ESC to close · ⌘S to save · Drag files to move</span>
      </div>
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function findTreeNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.id === path) return n;
    if (n.children) {
      const found = findTreeNode(n.children, path);
      if (found) return found;
    }
  }
  return null;
}
