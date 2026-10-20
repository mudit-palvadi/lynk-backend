"""Rules for sender-chosen file names.

A sender picks the names in a transfer, and every receiver writes those names
to disk. A name containing a path separator or a parent-directory reference
would let a sender write outside the download directory, so names are checked
once, at the API boundary, before they are stored or served to anyone.

The clients apply the same rules to names that arrive over the peer-to-peer
channel, where the server cannot vouch for the sender.
"""

import re

# The longest name most filesystems accept, in bytes rather than characters:
# a multi-byte name can be far shorter in characters than in bytes.
MAX_FILE_NAME_BYTES = 255

# NUL and the other control characters. They truncate paths on some platforms
# and corrupt them on others.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# Both spellings of a path separator: a receiver may be on POSIX or Windows.
_PATH_SEPARATORS = ("/", "\\")


def validate_file_name(name: str) -> str:
    """Returns `name` unchanged, or raises ValueError when it is unsafe.

    A safe name is a single path segment: no separator, no parent-directory
    reference, no control characters, and short enough for any filesystem.
    """
    if not name or not name.strip():
        raise ValueError("File name must not be empty")
    if _CONTROL_CHARACTERS.search(name):
        raise ValueError("File name must not contain control characters")
    if any(separator in name for separator in _PATH_SEPARATORS):
        raise ValueError("File name must not contain a path separator")
    if ".." in name:
        raise ValueError("File name must not contain '..'")
    if name != name.strip():
        raise ValueError("File name must not start or end with whitespace")
    if len(name.encode("utf-8")) > MAX_FILE_NAME_BYTES:
        raise ValueError(
            f"File name must be at most {MAX_FILE_NAME_BYTES} bytes"
        )
    return name
