"""Workspace filesystem REST API — browse, read, write, move, delete files.

Adapted from Kimi K3's reference implementation. Provides a secure,
workspace-confined filesystem API for the file editor UI.

All paths are workspace-relative POSIX strings. The service layer raises
typed domain errors; a single exception handler maps them to structured JSON.

Endpoints (all under /api/files):

    GET    /tree                       flat entry list for the file tree
    GET    /file?path=...              read text file (1MB cap, binary rejected)
    PUT    /file                       write file (5MB cap, mtime concurrency)
    DELETE /file?path=...&recursive=   delete file or directory
    GET    /raw?path=...               binary download / image preview
    POST   /mkdir                      create directory
    POST   /move                       rename / move
"""

from __future__ import annotations

import errno
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from app.config import BASE_DIR

FileKind = Literal["file", "dir"]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Never touch these, anywhere, at any depth. Exact component matches only,
#: so ".gitignore" and ".env.example" remain accessible on purpose.
DENIED_NAMES: frozenset[str] = frozenset({".git", ".env"})

#: Hidden from the tree (noise), but still reachable by direct path.
TREE_EXCLUDE_NAMES: frozenset[str] = frozenset({
    "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "dist", "build", "target", ".next", ".nuxt", ".turbo",
    ".DS_Store", ".clones",
})

READ_MAX_BYTES = 1 * 1024 * 1024       # 1MB text reads
WRITE_MAX_BYTES = 5 * 1024 * 1024      # 5MB writes
RAW_MAX_BYTES = 50 * 1024 * 1024       # 50MB binary/downloads
TREE_MAX_ENTRIES = 20_000              # cap for pathological repos

# ---------------------------------------------------------------------------
# Domain errors — the service layer raises these, never HTTPException.
# ---------------------------------------------------------------------------


class FSError(Exception):
    code: str = "internal_error"
    status: int = 500

    def __init__(self, message: str, **context):
        super().__init__(message)
        self.context = context


class InvalidPath(FSError):
    code, status = "invalid_path", 400


class PathOutsideWorkspace(FSError):
    code, status = "path_outside_workspace", 403


class DeniedPath(FSError):
    code, status = "path_denied", 403


class NotFound(FSError):
    code, status = "not_found", 404


class AlreadyExists(FSError):
    code, status = "already_exists", 409


class Conflict(FSError):
    code, status = "conflict", 409


class TooLarge(FSError):
    code, status = "too_large", 413


class BinaryFile(FSError):
    code, status = "binary_file", 415


class IsDirectory(FSError):
    code, status = "is_a_directory", 400


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TreeEntry(BaseModel):
    path: str
    name: str
    kind: FileKind
    size: Optional[int] = None
    mtime_ns: str
    symlink: bool = False


class TreeResponse(BaseModel):
    root_name: str
    entries: list[TreeEntry]
    truncated: bool


class ReadResponse(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"
    size: int
    mtime_ns: str


class WriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str
    expected_mtime_ns: Optional[str] = None


class WriteResponse(BaseModel):
    path: str
    size: int
    mtime_ns: str


class MoveRequest(BaseModel):
    src: str = Field(min_length=1, max_length=1024)
    dst: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _distinct(self):
        if self.src == self.dst:
            raise ValueError("src and dst must differ")
        return self


class MoveResponse(BaseModel):
    src: str
    dst: str
    kind: FileKind


class MkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class MkdirResponse(BaseModel):
    path: str
    mtime_ns: str


class DeleteResponse(BaseModel):
    path: str
    kind: FileKind


# ---------------------------------------------------------------------------
# WorkspaceFS — the service layer
# ---------------------------------------------------------------------------


class _TreeTruncated(Exception):
    """Internal control flow to unwind the tree walk when the entry cap hits."""


class WorkspaceFS:
    """Workspace-confined filesystem operations.

    All methods accept/return workspace-relative POSIX paths.
    Raises FSError subclasses on failure.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"workspace root does not exist: {root}")
        self._tree_skip = DENIED_NAMES | TREE_EXCLUDE_NAMES

    # -- path safety ---------------------------------------------------------

    def safe_resolve(self, rel: str, *, must_exist: bool = False) -> Path:
        """Resolve a client-supplied relative path to a confined absolute path.

        Guarantees on success:
          * result == workspace root, or is strictly inside it
          * no component (at any depth) is in DENIED_NAMES
          * symlink escapes are caught (resolve() follows symlinks, we check
            containment on the RESOLVED path)

        We intentionally do not string-filter "..": resolve() collapses it and
        the containment check does the real work.
        """
        if not rel or "\x00" in rel:
            raise InvalidPath("path is empty or contains a NUL byte")

        if rel.startswith(("/", "\\")) or Path(rel).is_absolute():
            raise InvalidPath(f"absolute paths are not allowed: {rel!r}")

        try:
            resolved = (self.root / rel).resolve(strict=must_exist)
        except FileNotFoundError:
            raise NotFound(f"not found: {rel}")
        except (OSError, RuntimeError) as e:
            raise InvalidPath(f"unresolvable path {rel!r}: {e}")

        if resolved != self.root and self.root not in resolved.parents:
            raise PathOutsideWorkspace(f"path escapes workspace: {rel!r}")

        for part in resolved.relative_to(self.root).parts:
            if part in DENIED_NAMES:
                raise DeniedPath(f"access to {part!r} is denied")

        return resolved

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- tree -----------------------------------------------------------------

    def tree(self) -> TreeResponse:
        """Flat list of entries (frontend builds the hierarchy)."""
        entries: list[TreeEntry] = []
        try:
            self._walk(self.root, "", entries)
        except _TreeTruncated:
            return TreeResponse(root_name=self.root.name, entries=entries, truncated=True)
        return TreeResponse(root_name=self.root.name, entries=entries, truncated=False)

    def _walk(self, dir_path: Path, rel_dir: str, out: list[TreeEntry]) -> None:
        if len(out) >= TREE_MAX_ENTRIES:
            raise _TreeTruncated
        try:
            with os.scandir(dir_path) as it:
                children = sorted(
                    it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())
                )
        except (PermissionError, OSError):
            return

        subdirs: list[tuple[Path, str]] = []
        for entry in children:
            if entry.name in self._tree_skip:
                continue
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_symlink():
                    st = entry.stat(follow_symlinks=False)
                    kind: FileKind = "dir" if entry.is_dir(follow_symlinks=True) else "file"
                    out.append(TreeEntry(
                        path=rel, name=entry.name, kind=kind,
                        size=None if kind == "dir" else st.st_size,
                        mtime_ns=str(st.st_mtime_ns), symlink=True,
                    ))
                elif entry.is_dir(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    out.append(TreeEntry(
                        path=rel, name=entry.name, kind="dir",
                        size=None, mtime_ns=str(st.st_mtime_ns),
                    ))
                    subdirs.append((Path(entry.path), rel))
                else:
                    st = entry.stat(follow_symlinks=False)
                    out.append(TreeEntry(
                        path=rel, name=entry.name, kind="file",
                        size=st.st_size, mtime_ns=str(st.st_mtime_ns),
                    ))
            except OSError:
                continue
            if len(out) >= TREE_MAX_ENTRIES:
                raise _TreeTruncated

        for path, rel in subdirs:
            self._walk(path, rel, out)

    # -- read ------------------------------------------------------------------

    def read(self, rel: str) -> ReadResponse:
        p = self.safe_resolve(rel, must_exist=True)
        if p.is_dir():
            raise IsDirectory(f"is a directory: {rel}")
        if not p.is_file():
            raise NotFound(f"not a regular file: {rel}")

        st = p.stat()
        if st.st_size > READ_MAX_BYTES:
            raise TooLarge(
                f"file exceeds read cap ({READ_MAX_BYTES} bytes)",
                size=st.st_size, limit=READ_MAX_BYTES,
            )

        data = p.read_bytes()
        if len(data) > READ_MAX_BYTES:
            raise TooLarge("file grew past read cap during read",
                           size=len(data), limit=READ_MAX_BYTES)

        # Binary detection: NUL byte in the first 8KB
        if b"\x00" in data[:8192]:
            raise BinaryFile(f"binary file (use /api/files/raw): {rel}", size=len(data))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise BinaryFile(f"not valid UTF-8: {rel}", size=len(data))

        return ReadResponse(path=self._rel(p), content=text, size=len(data),
                            mtime_ns=str(st.st_mtime_ns))

    # -- write -----------------------------------------------------------------

    def write(self, rel: str, content: str, expected_mtime_ns: Optional[str]) -> WriteResponse:
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidPath("content is not encodable as UTF-8")

        if len(data) > WRITE_MAX_BYTES:
            raise TooLarge(
                f"content exceeds write cap ({WRITE_MAX_BYTES} bytes)",
                size=len(data), limit=WRITE_MAX_BYTES,
            )

        p = self.safe_resolve(rel)
        if not p.parent.is_dir():
            raise NotFound(f"parent directory does not exist: {rel}")

        if p.exists():
            if p.is_dir():
                raise IsDirectory(f"is a directory: {rel}")
            current = str(p.stat().st_mtime_ns)
            if expected_mtime_ns is None:
                raise Conflict("expected_mtime_ns is required when overwriting",
                               reason="mtime_required", current_mtime_ns=current)
            if expected_mtime_ns != current:
                raise Conflict("file changed on disk since it was loaded",
                               reason="stale_mtime", current_mtime_ns=current)
        elif expected_mtime_ns is not None:
            raise Conflict("file was deleted since it was loaded", reason="gone")

        # Atomic write: temp file in same directory, then os.replace
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if p.exists():
                shutil.copymode(p, tmp)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        st = p.stat()
        return WriteResponse(path=self._rel(p), size=st.st_size,
                             mtime_ns=str(st.st_mtime_ns))

    # -- move / mkdir / delete --------------------------------------------------

    def move(self, src_rel: str, dst_rel: str) -> MoveResponse:
        src = self.safe_resolve(src_rel, must_exist=True)
        dst = self.safe_resolve(dst_rel)
        if src == self.root:
            raise InvalidPath("cannot move the workspace root")
        if src in dst.parents:
            raise InvalidPath("cannot move a directory into itself")
        if dst.exists() or dst.is_symlink():
            raise AlreadyExists(f"destination exists: {dst_rel}")
        if not dst.parent.is_dir():
            raise NotFound(f"destination directory does not exist: {dst_rel}")

        try:
            os.rename(src, dst)
        except OSError as e:
            if e.errno == errno.EXDEV:
                shutil.move(str(src), str(dst))
            else:
                raise

        kind: FileKind = "dir" if dst.is_dir() else "file"
        return MoveResponse(src=self._rel(src), dst=self._rel(dst), kind=kind)

    def mkdir(self, rel: str) -> MkdirResponse:
        p = self.safe_resolve(rel)
        if p.exists() or p.is_symlink():
            raise AlreadyExists(f"already exists: {rel}")
        p.mkdir(parents=True)
        return MkdirResponse(path=self._rel(p), mtime_ns=str(p.stat().st_mtime_ns))

    def delete(self, rel: str, *, recursive: bool = False) -> DeleteResponse:
        p = self.safe_resolve(rel, must_exist=True)
        if p == self.root:
            raise InvalidPath("cannot delete the workspace root")

        if p.is_dir() and not p.is_symlink():
            if recursive:
                for dirpath, dirnames, filenames in os.walk(p):
                    if DENIED_NAMES & (set(dirnames) | set(filenames)):
                        raise DeniedPath(
                            f"refusing recursive delete: subtree contains a denied name"
                        )
                shutil.rmtree(p)
            else:
                try:
                    p.rmdir()
                except OSError:
                    raise Conflict(f"directory is not empty (pass recursive=true): {rel}",
                                   reason="directory_not_empty")
            return DeleteResponse(path=self._rel(p), kind="dir")

        p.unlink()
        return DeleteResponse(path=self._rel(p), kind="file")

    # -- raw (binary) ----------------------------------------------------------

    def raw(self, rel: str) -> tuple[Path, str, str]:
        p = self.safe_resolve(rel, must_exist=True)
        if not p.is_file():
            raise NotFound(f"not a regular file: {rel}")
        size = p.stat().st_size
        if size > RAW_MAX_BYTES:
            raise TooLarge(f"file exceeds raw cap ({RAW_MAX_BYTES} bytes)",
                           size=size, limit=RAW_MAX_BYTES)
        media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return p, media_type, p.name


# ---------------------------------------------------------------------------
# Service instance + router
# ---------------------------------------------------------------------------

_fs = WorkspaceFS(BASE_DIR)

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/tree", response_model=TreeResponse)
def get_tree():
    return _fs.tree()


@router.get("/file", response_model=ReadResponse)
def read_file(path: str = Query(min_length=1, max_length=1024)):
    return _fs.read(path)


@router.put("/file", response_model=WriteResponse)
def write_file(body: WriteRequest):
    return _fs.write(body.path, body.content, body.expected_mtime_ns)


@router.delete("/file", response_model=DeleteResponse)
def delete_file(
    path: str = Query(min_length=1, max_length=1024),
    recursive: bool = Query(default=False),
):
    return _fs.delete(path, recursive=recursive)


@router.get("/raw")
def get_raw(
    path: str = Query(min_length=1, max_length=1024),
    download: bool = Query(default=False),
):
    abs_path, media_type, filename = _fs.raw(path)
    return FileResponse(
        abs_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="attachment" if download else "inline",
    )


@router.post("/mkdir", response_model=MkdirResponse)
def make_dir(body: MkdirRequest):
    return _fs.mkdir(body.path)


@router.post("/move", response_model=MoveResponse)
def move_entry(body: MoveRequest):
    return _fs.move(body.src, body.dst)


# ---------------------------------------------------------------------------
# Exception handlers — register on the FastAPI app during startup
# ---------------------------------------------------------------------------


def register_exception_handlers(app) -> None:
    @app.exception_handler(FSError)
    def _fs_error_handler(_request: Request, exc: FSError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": str(exc), **exc.context}},
        )

    @app.exception_handler(OSError)
    def _os_error_handler(_request: Request, exc: OSError) -> JSONResponse:
        status = 403 if exc.errno in (errno.EACCES, errno.EPERM) else 500
        return JSONResponse(
            status_code=status,
            content={"detail": {"code": "io_error", "message": "filesystem operation failed"}},
        )
