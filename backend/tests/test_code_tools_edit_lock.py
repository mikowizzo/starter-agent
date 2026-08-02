"""
Regression test: parallel edit calls must not clobber each other.

CodeTools.edit/edit_batch/write each do a whole-file read-modify-write.
When several run concurrently (the model firing parallel tool calls),
their writes raced and edits silently vanished. Every mutator now runs
under _edit_lock (threading.Lock + fcntl.flock), serializing calls so
each one sees the latest file state.

This test fires 8 edits at the same file from 8 threads, repeatedly.
Without the lock it fails almost every rerun; with it, all land.
"""

import threading
from pathlib import Path

from app.tools.code_tools import CodeTools

_LINES = "abcdefgh"
_RERUNS = 25


def test_parallel_edits_do_not_clobber(tmp_path: Path) -> None:
    for _ in range(_RERUNS):
        target = tmp_path / "x.txt"
        target.write_text("".join(f"{c}\n" for c in _LINES))
        tools = CodeTools(base_dir=str(tmp_path))

        barrier = threading.Barrier(len(_LINES))

        def do_edit(ch: str) -> None:
            barrier.wait()  # fire all edits at the same instant
            tools.edit("x.txt", search=f"{ch}\n", replace=f"{ch.upper()}\n")

        threads = [threading.Thread(target=do_edit, args=(c,)) for c in _LINES]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert target.read_text() == "".join(f"{c.upper()}\n" for c in _LINES), (
            "parallel edits lost updates — the edit lock is not working"
        )


def test_parallel_write_and_edit_do_not_clobber(tmp_path: Path) -> None:
    """A write racing an edit is the same hazard; both must serialize."""
    for _ in range(10):
        target = tmp_path / "x.txt"
        target.write_text("a\nb\n")
        tools = CodeTools(base_dir=str(tmp_path))
        barrier = threading.Barrier(2)

        def writer() -> None:
            barrier.wait()
            tools.write("x.txt", "1\n2\n3\n")

        def editor() -> None:
            barrier.wait()
            tools.edit("x.txt", search="b\n", replace="B\n")

        ts = [threading.Thread(target=writer), threading.Thread(target=editor)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        final = target.read_text()
        # Either ordering is valid, but the file must be internally consistent:
        # the write's full content OR the edit applied on top of it.
        assert final in ("1\n2\n3\n", "1\n2\n3\n"), final.replace("1\n2\n3\n", "")
