// ── File editor API — typed fetch wrappers for /api/files ───────────

export type FileKind = "file" | "dir";

export interface FileEntry {
  path: string;
  name: string;
  kind: FileKind;
  size: number | null;
  mtime_ns: string;
  symlink: boolean;
}

export interface TreeData {
  root_name: string;
  entries: FileEntry[];
  truncated: boolean;
}

export interface ReadResult {
  path: string;
  content: string;
  encoding: string;
  size: number;
  mtime_ns: string;
}

export interface WriteResult {
  path: string;
  size: number;
  mtime_ns: string;
}

export interface MoveResult {
  src: string;
  dst: string;
  kind: FileKind;
}

export interface MkdirResult {
  path: string;
  mtime_ns: string;
}

export interface DeleteResult {
  path: string;
  kind: FileKind;
}

// ── react-arborist node shape ──────────────────────────────────────

export interface TreeNode {
  id: string; // = path (unique)
  name: string;
  kind: FileKind;
  children?: TreeNode[];
}

// ── Tree builder: flat entries → nested react-arborist data ────────
// Two-pass build: create all nodes first, then link to parents.
// This is robust regardless of the backend's entry ordering — a
// single-pass build with re-sorting silently reparents orphans.

export function buildTreeData(entries: FileEntry[]): TreeNode[] {
  const root: TreeNode = { id: "", name: "", kind: "dir", children: [] };
  const nodes = new Map<string, TreeNode>([["", root]]);

  // Pass 1: create every node
  for (const e of entries) {
    nodes.set(e.path, {
      id: e.path,
      name: e.name,
      kind: e.kind,
      ...(e.kind === "dir" ? { children: [] } : {}),
    });
  }

  // Pass 2: link each node to its parent
  for (const e of entries) {
    const slashIdx = e.path.lastIndexOf("/");
    const parentPath = slashIdx >= 0 ? e.path.slice(0, slashIdx) : "";
    const parent = nodes.get(parentPath);
    const node = nodes.get(e.path);
    if (parent && node) {
      parent.children!.push(node);
    } else {
      // Orphan — shouldn't happen with correct backend ordering, but
      // attach to root rather than silently dropping.
      root.children.push(node!);
    }
  }

  return root.children!;
}

// ── API functions ──────────────────────────────────────────────────

async function fsFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail: any;
    try {
      detail = await res.json();
    } catch {
      detail = { detail: res.statusText };
    }
    const msg =
      detail?.detail?.message ??
      detail?.detail ??
      detail?.error ??
      `Request failed (${res.status})`;
    const code = detail?.detail?.code;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg)) as Error & {
      code?: string;
      context?: any;
    };
    err.code = code;
    err.context = detail?.detail;
    throw err;
  }
  return res.json();
}

export function fetchTree(): Promise<TreeData> {
  return fsFetch<TreeData>("/api/files/tree");
}

export function readFile(path: string): Promise<ReadResult> {
  return fsFetch<ReadResult>(`/api/files/file?path=${encodeURIComponent(path)}`);
}

export function writeFile(
  path: string,
  content: string,
  expected_mtime_ns: string | null,
): Promise<WriteResult> {
  return fsFetch<WriteResult>("/api/files/file", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content, expected_mtime_ns }),
  });
}

export function moveFile(src: string, dst: string): Promise<MoveResult> {
  return fsFetch<MoveResult>("/api/files/move", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ src, dst }),
  });
}

export function mkdir(path: string): Promise<MkdirResult> {
  return fsFetch<MkdirResult>("/api/files/mkdir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
}

export function deleteFile(path: string, recursive = false): Promise<DeleteResult> {
  const params = new URLSearchParams({ path });
  if (recursive) params.set("recursive", "true");
  return fsFetch<DeleteResult>(`/api/files/file?${params}`, { method: "DELETE" });
}

export function rawUrl(path: string): string {
  return `/api/files/raw?path=${encodeURIComponent(path)}`;
}
