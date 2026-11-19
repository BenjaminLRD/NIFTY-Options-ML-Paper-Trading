"""Publish a complete file under a NEW name without ever replacing anything (event logs and anchor records use this).

Steps: create `.<name>.<pid>.partial` in the destination folder exclusively, write, flush and fsync, read it back and compare, run the caller's `check` on the
bytes, then publish with a hard link, which fails if the destination exists (on Windows, if hard links are unavailable, a rename, which also refuses an
existing destination; elsewhere there is no safe fallback and the publish is refused). The temporary file is removed on every path; an existing
destination or a foreign partial file is never touched. Publishing is atomic on local NTFS and POSIX file systems; on POSIX the folder is flushed
afterwards, on Windows that is not possible, so a power loss just after a reported success can still lose the file. Network and unusual file systems are
not covered.
"""
import os
from pathlib import Path


class PublishError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def publish_new_file(data, destination, check=None):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PublishError("DESTINATION_EXISTS", "an existing file is never overwritten")
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(partial, flags, 0o600)
    except FileExistsError:
        raise PublishError("PARTIAL_FILE_EXISTS", f"{partial.name} is left from an earlier attempt; check it and remove it yourself") from None
    except OSError as error:
        raise PublishError("CANNOT_CREATE", error.__class__.__name__) from None
    try:
        try:
            handle = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)                                     # never leave the temporary file open (it could not then be removed on Windows)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if partial.read_bytes() != data:
            raise PublishError("READBACK_MISMATCH", "the file on disk differs from what was written")
        if check is not None:
            check(data)
        _link(partial, destination)
    except PublishError:
        _discard(partial)
        raise
    except OSError as error:
        _discard(partial)
        raise PublishError("WRITE_FAILED", error.__class__.__name__) from None
    _discard(partial)


def _link(partial, destination):
    try:
        os.link(partial, destination)
    except FileExistsError:
        raise PublishError("DESTINATION_EXISTS", "an existing file is never overwritten") from None
    except OSError:
        if os.name != "nt":
            raise PublishError("PUBLISH_UNSUPPORTED", "hard links are unavailable here and a plain rename could replace a file") from None
        try:
            os.rename(partial, destination)                          # on Windows rename refuses an existing destination
        except FileExistsError:
            raise PublishError("DESTINATION_EXISTS", "an existing file is never overwritten") from None
    else:
        if os.name != "nt":
            try:
                handle = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
            except OSError:
                pass


def _discard(partial):
    try:
        if partial.exists():
            partial.unlink()
    except OSError:
        pass
