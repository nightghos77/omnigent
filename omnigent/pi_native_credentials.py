"""Translate the omnigent-configured model provider into native Pi config.

A native Pi session launches the ``pi`` CLI, which authenticates from its own
config directory (``~/.pi/agent``). Without help, a user who ran ``omnigent
setup`` would still have to run ``pi`` ``/login`` separately — unlike
claude-native / codex-native, which route through the provider that ``omnigent
setup`` configured.

This module closes that gap. It resolves the provider configured for the Pi
surface (``~/.omnigent/config.yaml``) and writes a per-session ``models.json``
into a *managed* Pi config dir (selected via ``PI_CODING_AGENT_DIR``), so the
runner-owned ``pi`` process authenticates exactly like the configured harness —
mirroring how codex-native routes through the Databricks AI Gateway.

The managed config dir is per-session (like codex-native's managed
``CODEX_HOME``), so this never mutates the user's global ``~/.pi/agent``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    CHAT_WIRE_API,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    OPENAI_FAMILY,
    PI_SURFACE,
    ProviderEntry,
    get_default_provider,
    load_config,
)

_LOGGER = logging.getLogger(__name__)

# Env var the ``pi`` CLI reads to relocate its config dir (default
# ``~/.pi/agent``). Setting it per session gives Pi a managed, isolated
# config dir we own — the analog of codex-native's ``CODEX_HOME``.
PI_CODING_AGENT_DIR_ENV_VAR = "PI_CODING_AGENT_DIR"

# Provider id registered in the generated ``models.json``. Stable so
# ``--provider`` can select it.
_PI_PROVIDER_ID = "omnigent"

# Default model for the Databricks AI Gateway's Anthropic surface — the same
# default the in-process Databricks executor pins. Used when the session
# carries no explicit model override.
_DATABRICKS_PI_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"

# Databricks AI Gateway Anthropic Messages surface. Pi speaks this protocol
# natively (``api: anthropic-messages``); the gateway authenticates with a
# workspace bearer token, so we set ``authHeader`` (Authorization: Bearer).
_DATABRICKS_ANTHROPIC_GATEWAY_PATH = "/ai-gateway/anthropic"

# The Databricks AI Gateway exposes one surface per protocol under the same
# workspace origin: Codex/OpenAI-Responses at ``/codex/v1`` and Anthropic
# Messages at ``/anthropic``. ``isaac configure codex`` writes the Codex
# base_url; pi-native rewrites it to the Anthropic surface Pi speaks natively.
_DATABRICKS_GATEWAY_CODEX_SUFFIX = "/codex/v1"
_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX = "/anthropic"


@dataclass(frozen=True)
class PiProviderConfig:
    """A resolved native-Pi provider, ready to render into ``models.json``.

    :param provider_id: Provider id used in ``models.json`` and ``--provider``.
    :param base_url: Endpoint base URL the ``pi`` CLI talks to.
    :param api: Pi API type, e.g. ``"anthropic-messages"`` or
        ``"openai-responses"``.
    :param model: Model id to select, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param api_key: Credential value for ``models.json`` ``apiKey`` — a literal
        key, an env-var name, or a ``"!command"`` shell form (resolved by Pi at
        request time, used for short-lived gateway tokens).
    :param auth_header: When ``True``, Pi sends ``Authorization: Bearer
        <apiKey>`` (gateways) instead of a provider-native key header.
    """

    provider_id: str
    base_url: str
    api: str
    model: str
    api_key: str
    auth_header: bool

    def to_models_config(self) -> dict[str, Any]:
        """Render this provider as a Pi ``models.json`` mapping."""
        provider: dict[str, Any] = {
            "baseUrl": self.base_url,
            "api": self.api,
            "apiKey": self.api_key,
            "models": [{"id": self.model}],
        }
        if self.auth_header:
            provider["authHeader"] = True
        return {"providers": {self.provider_id: provider}}


def _databricks_pi_provider(entry: ProviderEntry, *, model: str | None) -> PiProviderConfig | None:
    """Resolve a Databricks-profile provider into Pi gateway config.

    :param entry: The resolved default provider entry (``kind="databricks"``).
    :param model: Session model override, or ``None`` to use the default.
    :returns: The Pi provider config, or ``None`` when the profile's host
        can't be resolved (caller falls back to Pi's own login).
    """
    # Imported lazily: codex_executor pulls in heavy inner deps, and this
    # module is imported on the runner's session-create path.
    from omnigent.inner.codex_executor import _databricks_codex_auth_command
    from omnigent.inner.databricks_executor import _read_databrickscfg_host

    host = _read_databrickscfg_host(entry.profile)
    if not host:
        return None
    host = host.rstrip("/")
    auth_command = _databricks_codex_auth_command(host, entry.profile)
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=f"{host}{_DATABRICKS_ANTHROPIC_GATEWAY_PATH}",
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token is refreshed per request (the auth command itself
        # force-refreshes), matching codex-native's refresh semantics.
        api_key=f"!{auth_command}",
        auth_header=True,
    )


def _gateway_anthropic_base_url(codex_base_url: str) -> str:
    """Rewrite a Codex gateway base URL to the Anthropic Messages surface.

    The Databricks AI Gateway serves each protocol under the same workspace
    origin: ``.../codex/v1`` (OpenAI Responses) and ``.../anthropic``
    (Anthropic Messages). ``isaac configure codex`` records the Codex URL;
    Pi speaks Anthropic Messages natively, so we point it at ``/anthropic``.

    :param codex_base_url: The provider table's ``base_url``, e.g.
        ``"https://<workspace>.ai-gateway.cloud.databricks.com/codex/v1"``.
    :returns: The Anthropic-surface base URL, e.g.
        ``"https://<workspace>.ai-gateway.cloud.databricks.com/anthropic"``.
    """
    trimmed = codex_base_url.rstrip("/")
    if trimmed.endswith(_DATABRICKS_GATEWAY_CODEX_SUFFIX):
        trimmed = trimmed[: -len(_DATABRICKS_GATEWAY_CODEX_SUFFIX)]
    if trimmed.endswith(_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX):
        return trimmed
    return f"{trimmed}{_DATABRICKS_GATEWAY_ANTHROPIC_SUFFIX}"


def _cli_config_pi_provider(entry: ProviderEntry, *, model: str | None) -> PiProviderConfig | None:
    """Resolve a Codex ``cli-config`` Databricks-gateway provider into Pi config.

    The common enterprise setup: ``isaac configure codex`` writes a custom
    ``[model_providers.X]`` table (base_url + token-printing ``auth`` command)
    into ``~/.codex/config.toml`` and ``omnigent setup`` adopts it as a
    ``cli-config`` provider. Codex-native routes through that table; pi-native
    used to return ``None`` here — silently falling back to Pi's own
    ``/login`` (often stale creds) — which is the bug this fixes.

    We read the *transport* (base URL + bearer-token command) from the codex
    config table the entry pins, rewrite the base URL to the gateway's
    Anthropic Messages surface (Pi speaks it natively), and emit a ``!command``
    apiKey so Pi refreshes the gateway token per request — exactly like the
    ``databricks`` kind path. The workspace-specific base URL and token path
    are read from config, never hardcoded.

    :param entry: The resolved default provider (``kind="cli-config"``,
        ``cli="codex"``), carrying the ``model_provider`` id and display name.
    :param model: Session model override, or ``None`` to use the default.
    :returns: The Pi provider config, or ``None`` when the entry is not a
        Databricks gateway, its codex provider table can't be resolved, or it
        carries no token command (caller falls back to Pi's own login).
    """
    # Only codex cli-config providers are model_provider-shaped today; a
    # claude analog would be a different mechanism entirely.
    if entry.cli != "codex" or not entry.model_provider:
        return None
    # Imported lazily: ambient pulls in onboarding-only deps, and this module
    # is imported on the runner's session-create hot path.
    from omnigent.onboarding.ambient import (
        _codex_config_path,
        codex_config_provider_transport,
    )

    transport = codex_config_provider_transport(_codex_config_path(), entry.model_provider)
    if transport is None:
        _LOGGER.info(
            "pi-native: cli-config provider %r (model_provider %r) has no resolvable "
            "[model_providers.%s] base_url in ~/.codex/config.toml; Pi will use its own login.",
            entry.name,
            entry.model_provider,
            entry.model_provider,
        )
        return None
    # Identify the Databricks AI Gateway robustly (not by workspace id): the
    # codex base_url points at the AI Gateway host. We accept any gateway-shaped
    # base_url — the canonical Databricks form is "*.ai-gateway.*databricks*".
    host = transport.base_url
    is_databricks_gateway = "databricks" in host.lower() and "ai-gateway" in host.lower()
    if not is_databricks_gateway:
        _LOGGER.info(
            "pi-native: cli-config provider %r (model_provider %r, base_url %r) is not a "
            "recognized Databricks AI Gateway; Pi will use its own login.",
            entry.name,
            entry.model_provider,
            transport.base_url,
        )
        return None
    if not transport.auth_command:
        _LOGGER.info(
            "pi-native: Databricks cli-config provider %r carries no [model_providers.%s.auth] "
            "token command; Pi will use its own login.",
            entry.name,
            entry.model_provider,
        )
        return None
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=_gateway_anthropic_base_url(transport.base_url),
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token (the codex auth command prints it) is refreshed per
        # request — matching codex-native's refresh semantics.
        api_key=f"!{transport.auth_command}",
        auth_header=True,
    )


def _inline_family_pi_provider(
    entry: ProviderEntry, *, model: str | None
) -> PiProviderConfig | None:
    """Resolve a key/gateway/local provider into Pi config from its family.

    Prefers the Anthropic family (Pi speaks ``anthropic-messages`` natively),
    falling back to the OpenAI family via the Responses API.

    :param entry: The resolved default provider entry.
    :param model: Session model override, or ``None`` to use the family default.
    :returns: The Pi provider config, or ``None`` when no usable family with a
        base URL and credential is configured.
    """
    for family_name in ("anthropic", "openai"):
        family = entry.family(family_name)
        if family is None or not family.base_url:
            continue
        # Determine the API type based on family and wire_api setting.
        if family_name == "anthropic":
            api = "anthropic-messages"
        elif family.wire_api == CHAT_WIRE_API:
            api = "openai-completions"
        else:
            api = "openai-responses"
        # A static key (or $VAR) — Pi reads a literal/env apiKey directly; an
        # auth_command becomes a "!command" Pi resolves at request time.
        if family.api_key:
            api_key = family.api_key
            auth_header = False
        elif family.auth_command:
            api_key = f"!{family.auth_command}"
            auth_header = True
        else:
            continue
        resolved_model = model or entry.family_default_model(family_name)
        if not resolved_model:
            continue
        return PiProviderConfig(
            provider_id=_PI_PROVIDER_ID,
            base_url=family.base_url,
            api=api,
            model=resolved_model,
            api_key=api_key,
            auth_header=auth_header,
        )
    return None


def resolve_pi_native_provider(
    *,
    model: str | None = None,
    config_loader: Callable[[], dict[str, Any]] = load_config,
) -> PiProviderConfig | None:
    """Resolve the omnigent-configured provider for a native Pi session.

    Reads the default provider for the Pi surface from
    ``~/.omnigent/config.yaml`` and translates it into Pi ``models.json``
    config. Returns ``None`` — leaving Pi to use its own ``/login`` — when no
    usable provider is configured, or the default is a subscription / CLI-login
    provider (a CLI's own login can't be reused outside that CLI).

    :param model: Session model override (``model_override``), or ``None`` to
        use the provider's default model.
    :param config_loader: Injection seam for tests; defaults to
        :func:`load_config`.
    :returns: The resolved provider config, or ``None`` to fall back to Pi's
        own credentials.
    """
    try:
        config = config_loader()
        # Pi is multi-family; ``omnigent setup`` marks defaults per family, not
        # for ``pi``. Prefer an explicit pi default, then Anthropic (Pi's native
        # surface), then OpenAI.
        entry = (
            get_default_provider(config, PI_SURFACE)
            or get_default_provider(config, ANTHROPIC_FAMILY)
            or get_default_provider(config, OPENAI_FAMILY)
        )
        if entry is None:
            _LOGGER.info(
                "pi-native: no omnigent-configured provider for the pi/anthropic/openai "
                "surface; Pi will use its own login."
            )
            return None
        if entry.kind == DATABRICKS_KIND:
            resolved = _databricks_pi_provider(entry, model=model)
        elif entry.kind == CLI_CONFIG_KIND:
            # A Codex cli-config provider whose [model_providers.X] table is the
            # Databricks AI Gateway IS reusable by Pi (the gateway exposes an
            # Anthropic surface Pi speaks). Translate it rather than dropping to
            # Pi's own login — the bug this module fixes.
            resolved = _cli_config_pi_provider(entry, model=model)
        elif entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
            resolved = _inline_family_pi_provider(entry, model=model)
        else:
            # subscription (a CLI's own login can't be reused outside that CLI):
            # let Pi use its own login.
            _LOGGER.info(
                "pi-native: configured provider %r (kind %r) cannot drive Pi; "
                "Pi will use its own login.",
                entry.name,
                entry.kind,
            )
            return None
        if resolved is None:
            # The provider matched a translatable kind but its details could not
            # be resolved (e.g. a Databricks gateway whose codex config table is
            # missing). Don't swallow it silently — a future user mystified by an
            # "OpenRouter auth error despite configuring Databricks" needs this.
            _LOGGER.warning(
                "pi-native: configured provider %r (kind %r) could not be translated "
                "into native Pi config; Pi will use its own login (which may hold "
                "unrelated/stale credentials).",
                entry.name,
                entry.kind,
            )
        return resolved
    except Exception:  # noqa: BLE001 — any resolution failure must not break launch
        # Any failure (malformed config, duplicate per-family default, or an
        # unresolved ``api_key: $VAR``) falls back to Pi's own login rather than
        # failing the terminal launch.
        _LOGGER.warning(
            "pi-native: failed to resolve the omnigent-configured provider; Pi will "
            "use its own login.",
            exc_info=True,
        )
        return None


def write_pi_models_config(agent_dir: Path, provider: PiProviderConfig) -> Path:
    """Write *provider* as ``models.json`` into a managed Pi config dir.

    :param agent_dir: The managed Pi config dir (``PI_CODING_AGENT_DIR``).
    :param provider: The resolved provider config to render.
    :returns: Path to the written ``models.json``.
    """
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_dir, 0o700)
    models_path = agent_dir / "models.json"
    # 0o600: the apiKey may be a literal token (key-kind providers).
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(provider.to_models_config(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return models_path


def pi_native_provider_launch(
    agent_dir: Path, provider: PiProviderConfig
) -> tuple[dict[str, str], list[str]]:
    """Write the managed config and return the launch env + CLI args for Pi.

    :param agent_dir: The managed Pi config dir for this session.
    :param provider: The resolved provider config.
    :returns: ``(env, args)`` — the env vars to merge into the terminal spec
        (relocating Pi's config dir) and the ``--provider``/``--model`` args to
        append to the Pi command.
    """
    write_pi_models_config(agent_dir, provider)
    env = {PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    args = ["--provider", provider.provider_id, "--model", provider.model]
    return env, args
