"""One writer per user's K10 Pro, including across copies of the project."""
import fcntl
import os
import stat
import tempfile
from pathlib import Path


class DeviceLock:
    def __init__(self, path=None):
        self.path = Path(path or Path(tempfile.gettempdir()) / f"orca-led-{os.getuid()}-3434-02a0.lock")
        self.fd = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise RuntimeError("Unexpected device lock owner or file type")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Orca LED is already controlling this keyboard. Stop that process first.") from exc
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Keep the inode: unlinking it creates a race with waiting processes.
