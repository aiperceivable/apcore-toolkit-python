"""Token persistence: the ``TokenStore`` protocol and a portable file store.

The toolkit deliberately ships **only** the file-backed half. OS keychain
access is the least portable thing in the whole design (three platforms, three
unrelated libraries, three failure modes), and cross-language behavioural
parity — the property this repository's conformance corpus exists to enforce —
is not achievable there. A consumer wanting a keychain implements
:class:`TokenStore`; the flow neither knows nor cares which store it was given.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from apcore_toolkit.auth.errors import CredentialPermissionError
from apcore_toolkit.auth.tokens import TokenSet

logger = logging.getLogger("apcore_toolkit")

#: Bits that must be clear on a POSIX credentials file. Anything readable or
#: writable by group or other is refused rather than read.
_FORBIDDEN_MODE_BITS = 0o077


def default_credentials_path() -> Path:
    """The platform-conventional credentials location.

    Documented normatively because other tools need to know about it: ``apexe``
    maintains a list of credential-bearing paths (``~/.ssh``, ``~/.aws``, …)
    and cannot add a location that varies per SDK.

    macOS deliberately uses the XDG-style path rather than
    ``~/Library/Application Support`` so a developer's dotfile conventions and
    any cross-platform tooling see one location.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "apcore" / "credentials.json"
    if sys.platform == "darwin":
        return Path.home() / ".config" / "apcore" / "credentials.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "apcore" / "credentials.json"


def store_key(issuer: str, client_id: str) -> str:
    """The canonical store key, ``"<issuer>|<client_id>"``.

    Keying on the issuing authorization server means credentials for several
    providers coexist in one file without collision, and that a credential is
    never reused across a change of authorization server.
    """
    return f"{issuer}|{client_id}"


@runtime_checkable
class TokenStore(Protocol):
    """Where a :class:`~apcore_toolkit.auth.tokens.TokenSet` is kept.

    ``load`` never raises on a missing store; it returns ``None``. ``save``
    must be atomic. ``clear`` is idempotent.
    """

    def load(self, key: str) -> TokenSet | None:
        """Return the stored credential for ``key``, or ``None``."""
        ...

    def save(self, key: str, tokens: TokenSet) -> None:
        """Persist ``tokens`` under ``key``, replacing any previous record."""
        ...

    def clear(self, key: str) -> None:
        """Remove ``key``; clearing an absent key is not an error."""
        ...


class MemoryTokenStore:
    """An in-process store. Useful for tests and for short-lived daemons.

    Nothing here is persisted, so nothing here needs permissions.
    """

    def __init__(self) -> None:
        self._records: dict[str, TokenSet] = {}

    def load(self, key: str) -> TokenSet | None:
        return self._records.get(key)

    def save(self, key: str, tokens: TokenSet) -> None:
        self._records[key] = tokens

    def clear(self, key: str) -> None:
        self._records.pop(key, None)


class FileTokenStore:
    """A portable ``0600`` JSON file holding one record per issuer/client pair.

    Two properties are load-bearing and both are specified rather than
    incidental:

    * **Created ``0600`` at open time**, never chmod'd afterwards — a
      create-then-chmod sequence leaves a window in which the file is
      world-readable.
    * **Atomic replace** — written to a temporary file *in the same directory*
      and then renamed, so a reader never observes a half-written file and a
      lost refresh race costs a redundant re-login rather than a corrupted
      store.

    The file is *not* encrypted. A key stored next to its ciphertext is
    theatre; real protection is an OS keychain, which is a consumer concern.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_credentials_path()

    def load(self, key: str) -> TokenSet | None:
        records = self._read_all()
        record = records.get(key)
        if not isinstance(record, dict):
            return None
        try:
            return TokenSet.from_dict(record)
        except ValueError:
            logger.warning("credentials file %s holds an unreadable record for key %r", self.path, key)
            return None

    def save(self, key: str, tokens: TokenSet) -> None:
        records = self._read_all()
        records[key] = tokens.to_dict()
        self._write_all(records)

    def clear(self, key: str) -> None:
        if not self.path.exists():
            return
        records = self._read_all()
        if key not in records:
            return
        del records[key]
        self._write_all(records)

    # -- internals ---------------------------------------------------------

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        self._assert_permissions()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("credentials file %s is not readable JSON (%s); treating it as empty", self.path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("credentials file %s is not a JSON object; treating it as empty", self.path)
            return {}
        return data

    def _assert_permissions(self) -> None:
        """Refuse a credentials file other local users can read.

        Windows files inherit the ACL of ``%APPDATA%``, which is already
        user-scoped, so there is no POSIX mode to inspect there.
        """
        if os.name != "posix":
            return
        mode = self.path.stat().st_mode & 0o777
        if mode & _FORBIDDEN_MODE_BITS:
            raise CredentialPermissionError(
                f"credentials file {self.path} has mode {mode:04o}, which is readable by other users; "
                f"refusing to read it. Fix with: chmod 600 {self.path}"
            )

    def _write_all(self, records: dict[str, Any]) -> None:
        directory = self.path.parent
        # Only tighten a directory this store created. An existing one is the
        # operator's to configure — silently re-chmod'ing it on every save
        # would override a deliberate choice.
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if created and os.name == "posix":
            try:
                directory.chmod(0o700)
            except OSError:  # pragma: no cover - non-owned directory
                logger.warning("could not tighten permissions on %s", directory)

        payload = json.dumps(records, indent=2, sort_keys=True) + "\n"
        # ``mkstemp`` opens with O_CREAT|O_EXCL and mode 0600, so the file is
        # never world-readable even momentarily. The temp file lives in the
        # *same* directory so the rename below is atomic (a cross-filesystem
        # rename is not).
        fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".credentials-", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
