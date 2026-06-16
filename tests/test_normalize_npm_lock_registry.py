"""Unit tests for ``scripts/normalize_npm_lock_registry.py``.

The pre-commit fixer rewrites every proxy ``"resolved": "<url>"`` host in
``ap-web/package-lock.json`` to public npm so a developer's local
registry/proxy never leaks into the committed lockfile. These tests pin
that contract: proxy ``resolved`` hosts are normalized (path preserved,
scheme forced to https), npmjs and other non-proxy hosts are left alone,
the fixer is idempotent, and ``main`` signals modifications via its exit
code (1 = changed → commit aborts and re-stages; 0 = clean).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "normalize_npm_lock_registry.py"

# The canonical host the fixer must always produce — kept in the test as
# an independent literal so a change to the script's constant is caught.
_CANONICAL_HOST = "registry.npmjs.org"

# A representative Databricks proxy host the lockfile must never retain.
_PROXY_HOST = "npm-proxy.cloud.databricks.com"

# A package path shared by the proxy/canonical URL pair — identical on the
# proxy mirror and on public npm, so only the host should ever change.
_PKG_PATH = "/@adobe/css-tools/-/css-tools-4.5.0.tgz"


def _resolved(host: str, *, scheme: str = "https", path: str = _PKG_PATH) -> str:
    """Render one ``"resolved": "<url>"`` lockfile line for *host*."""
    return f'      "resolved": "{scheme}://{host}{path}",\n'


def _load_module() -> Any:
    """Import ``scripts/normalize_npm_lock_registry.py`` from its file path.

    ``scripts/`` is not a package on ``sys.path`` (mirrors
    ``tests/test_normalize_uv_lock_registry.py``'s loader), so load it
    directly.

    :returns: The imported module, exposing ``normalize_text``,
        ``_proxy_resolved_urls``, and ``main``.
    """
    spec = importlib.util.spec_from_file_location("scripts_normalize_npm_lock", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Could not locate the script at {_SCRIPT_PATH}."
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()


def test_normalize_text_rewrites_proxy_host() -> None:
    """A Databricks-proxy ``resolved`` host is rewritten to public npm."""
    assert _MOD.normalize_text(_resolved(_PROXY_HOST)) == _resolved(_CANONICAL_HOST)


def test_normalize_text_preserves_package_path() -> None:
    """Only the host changes — the package path is carried over verbatim."""
    path = "/some-scope/pkg/-/pkg-9.9.9-beta.1.tgz"
    result = _MOD.normalize_text(_resolved(_PROXY_HOST, path=path))
    assert result == _resolved(_CANONICAL_HOST, path=path)


def test_normalize_text_forces_https_on_proxy() -> None:
    """An http proxy URL is upgraded to https when normalized."""
    result = _MOD.normalize_text(_resolved(_PROXY_HOST, scheme="http"))
    assert result == _resolved(_CANONICAL_HOST, scheme="https")


def test_normalize_text_rewrites_every_occurrence() -> None:
    """Multiple proxy ``resolved`` entries are all normalized in one pass."""
    text = _resolved(_PROXY_HOST) * 3
    result = _MOD.normalize_text(text)
    assert _PROXY_HOST not in result
    assert result.count(_CANONICAL_HOST) == 3


def test_normalize_text_leaves_npmjs_untouched() -> None:
    """A ``resolved`` URL already on public npm is returned unchanged."""
    text = _resolved(_CANONICAL_HOST)
    assert _MOD.normalize_text(text) == text


def test_normalize_text_leaves_non_proxy_hosts_untouched() -> None:
    """git / GitHub-tarball ``resolved`` sources and other content survive."""
    text = (
        _resolved("github.com", path="/org/repo/archive/abcdef.tar.gz")
        + _resolved("codeload.github.com", path="/org/repo/tar.gz/abcdef")
        + '      "version": "1.2.3",\n'
        + '      "integrity": "sha512-deadbeef==",\n'
    )
    assert _MOD.normalize_text(text) == text


def test_normalize_text_ignores_proxy_url_outside_resolved_field() -> None:
    """A proxy host in a non-``resolved`` field is not touched."""
    text = f'      "funding": "https://{_PROXY_HOST}/sponsors",\n'
    assert _MOD.normalize_text(text) == text


def test_normalize_text_already_canonical_is_noop() -> None:
    """Text already pointing at public npm is returned unchanged."""
    text = _resolved(_CANONICAL_HOST)
    assert _MOD.normalize_text(text) == text


def test_proxy_resolved_urls_lists_offenders() -> None:
    """The check helper reports each proxy ``resolved`` URL, in order."""
    proxy_url = f"https://{_PROXY_HOST}{_PKG_PATH}"
    text = _resolved(_PROXY_HOST) + _resolved(_CANONICAL_HOST) + _resolved(_PROXY_HOST)
    assert _MOD._proxy_resolved_urls(text) == [proxy_url, proxy_url]


def test_proxy_resolved_urls_empty_when_canonical() -> None:
    """A fully-canonical lockfile reports no offenders."""
    assert _MOD._proxy_resolved_urls(_resolved(_CANONICAL_HOST)) == []


def test_main_rewrites_file_and_returns_one_when_changed(tmp_path: Path) -> None:
    """``main`` rewrites a proxy lockfile in place and returns 1 (changed)."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(_resolved(_PROXY_HOST))
    rc = _MOD.main([str(lock)])
    assert rc == 1
    assert lock.read_text() == _resolved(_CANONICAL_HOST)


def test_main_returns_zero_when_already_canonical(tmp_path: Path) -> None:
    """``main`` leaves a canonical lockfile untouched and returns 0 (clean)."""
    lock = tmp_path / "package-lock.json"
    original = _resolved(_CANONICAL_HOST)
    lock.write_text(original)
    rc = _MOD.main([str(lock)])
    assert rc == 0
    assert lock.read_text() == original


def test_main_is_idempotent(tmp_path: Path) -> None:
    """A second run after normalization is a no-op returning 0."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(_resolved(_PROXY_HOST))
    assert _MOD.main([str(lock)]) == 1
    assert _MOD.main([str(lock)]) == 0


def test_main_handles_multiple_files(tmp_path: Path) -> None:
    """Given several files, any change yields exit 1 and each is normalized."""
    proxy = _resolved(_PROXY_HOST)
    canonical = _resolved(_CANONICAL_HOST)
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(proxy)
    b.write_text(canonical)
    rc = _MOD.main([str(a), str(b)])
    assert rc == 1
    assert a.read_text() == canonical
    assert b.read_text() == canonical


def test_main_check_fails_without_writing(tmp_path: Path) -> None:
    """``--check`` returns 1 for a proxy lockfile and does NOT modify it."""
    lock = tmp_path / "package-lock.json"
    original = _resolved(_PROXY_HOST)
    lock.write_text(original)
    rc = _MOD.main(["--check", str(lock)])
    assert rc == 1
    # Check mode must be read-only — the file is left exactly as-is.
    assert lock.read_text() == original


def test_main_check_passes_when_canonical(tmp_path: Path) -> None:
    """``--check`` returns 0 for an already-canonical lockfile."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(_resolved(_CANONICAL_HOST))
    assert _MOD.main(["--check", str(lock)]) == 0


def test_main_check_flag_position_independent(tmp_path: Path) -> None:
    """``--check`` is recognized whether it precedes or follows the file."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(_resolved(_CANONICAL_HOST))
    assert _MOD.main([str(lock), "--check"]) == 0
