"""Private durable runtime checkpoints; never log their contents.

Same-UID data is not a security boundary against that UID, but symlinks,
unexpected paths and changed components must not become cleanup authority.
"""
import json
import os
from pathlib import Path
import socket
import stat
import tempfile


def read_private(path, max_bytes=65536):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or not 0 < info.st_size <= max_bytes:
            raise ValueError('Invalid private runtime checkpoint')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def write_private(path, value):
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Invalid private checkpoint parent')
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, name = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def notify(message):
    endpoint = os.environ.get('NOTIFY_SOCKET')
    if not endpoint:
        return
    if endpoint.startswith('@'):
        endpoint = '\0' + endpoint[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as client:
        client.connect(endpoint)
        client.sendall(message.encode('ascii'))


def state_directory(parent, value):
    name = value.get('directory')
    if not isinstance(name, str) or not name.startswith('trial-') or Path(name).name != name:
        raise ValueError('Invalid owned runtime directory')
    directory = parent / name
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Invalid owned runtime directory')
    return directory
