"""The signer is the only module that reads an agent key, so its tests are about refusal.

Every case here is a way the key or the account could be wrong, plus the one thing that
must never happen whatever else does: the key reaching a log line, a repr or an exception.
"""

from __future__ import annotations

from typing import Any

import pytest
from hyperliquid.info import Info

from liqfade.config import resolve
from liqfade.config.models import Settings
from liqfade.execution.signer import (
    ACCOUNT_ENV,
    AGENT_KEY_ENV,
    Signer,
    SignerError,
    load_signer,
)

# A throwaway key with no funds anywhere, used only so eth_account has something to parse.
FAKE_KEY = "0x" + "11" * 32
FAKE_ADDRESS = "0x" + "ab" * 20


def make_settings(env: str, **dotted: Any) -> Settings:
    merged, _ = resolve(env)
    for key, value in dotted.items():
        node = merged
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return Settings.model_validate(merged)


@pytest.fixture(autouse=True)
def no_inherited_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file may see a real key from the developer's environment."""
    for name in (*ACCOUNT_ENV.values(), *AGENT_KEY_ENV.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LIQFADE_LIVE_TRADING", raising=False)


def test_paper_has_no_signer_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper simulates fills. A signer existing there is a bug, not a convenience."""
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], FAKE_KEY)
    with pytest.raises(SignerError, match="paper"):
        load_signer(make_settings("paper"))


def test_missing_key_names_the_variable_not_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    with pytest.raises(SignerError) as caught:
        load_signer(make_settings("testnet"))
    assert AGENT_KEY_ENV["testnet"] in str(caught.value)


def test_placeholder_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` ships `HL_TESTNET_ACCOUNT_ADDRESS=0x`. Copied as is, it must fail."""
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], "0x")
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], FAKE_KEY)
    with pytest.raises(SignerError, match="account address"):
        load_signer(make_settings("testnet"))


def test_malformed_key_is_refused_without_echoing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], "0xnotakey")
    with pytest.raises(SignerError) as caught:
        load_signer(make_settings("testnet"))
    assert "0xnotakey" not in str(caught.value)


def test_testnet_signer_carries_the_public_half_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], FAKE_KEY)
    signer = load_signer(make_settings("testnet"))

    assert signer.account_address.lower() == FAKE_ADDRESS
    assert signer.env == "testnet"
    assert signer.base_url == "https://api.hyperliquid-testnet.xyz"
    # The agent address is derived from the key and is public, so it may be logged: it is
    # what `app.hyperliquid.xyz/API` shows and what a "does this agent exist" check needs.
    assert signer.agent_address.startswith("0x")
    assert signer.agent_address.lower() != FAKE_ADDRESS


def test_the_key_is_in_no_repr_no_str_and_no_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one invariant that matters. A dataclass repr is how a key reaches a traceback."""
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], FAKE_KEY)
    signer = load_signer(make_settings("testnet"))

    bare = FAKE_KEY.removeprefix("0x")
    for rendered in (repr(signer), str(signer), f"{signer}"):
        assert FAKE_KEY not in rendered
        assert bare not in rendered
    # __slots__ and no __dict__, so there is nothing for a generic dumper to walk either.
    assert not hasattr(signer, "__dict__")


def test_mainnet_refuses_without_the_live_trading_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md rule 3, enforced a second time here. Two independent refusals."""
    monkeypatch.setenv(ACCOUNT_ENV["mainnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["mainnet"], FAKE_KEY)
    with pytest.raises(SignerError, match="LIQFADE_LIVE_TRADING"):
        load_signer(make_settings("mainnet"))


def test_mainnet_refuses_while_the_kill_flag_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv(ACCOUNT_ENV["mainnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["mainnet"], FAKE_KEY)
    monkeypatch.setenv("LIQFADE_LIVE_TRADING", "1")
    settings = make_settings("mainnet", **{"paths.data_dir": str(tmp_path)})
    (tmp_path / "KILL").write_text("killed_at: now\n")
    with pytest.raises(SignerError, match="kill"):
        load_signer(settings)


def test_exchange_is_built_against_the_configured_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL comes from config, never from the SDK's default, so testnet stays testnet."""
    monkeypatch.setenv(ACCOUNT_ENV["testnet"], FAKE_ADDRESS)
    monkeypatch.setenv(AGENT_KEY_ENV["testnet"], FAKE_KEY)
    # The SDK's Exchange builds an Info, which fetches meta and spotMeta over the network
    # from whatever base_url it was given. Standing those two in keeps this a unit test:
    # it ran against live testnet and cost 2.1s, and it would have failed offline.
    monkeypatch.setattr(Info, "meta", lambda self, dex="": {"universe": []})
    monkeypatch.setattr(Info, "spot_meta", lambda self: {"tokens": [], "universe": []})
    signer = load_signer(make_settings("testnet"))
    exchange = signer.exchange()
    assert exchange.base_url == "https://api.hyperliquid-testnet.xyz"
    assert exchange.account_address.lower() == FAKE_ADDRESS


def test_signer_cannot_be_built_by_hand_with_a_key_argument() -> None:
    """`load_signer` is the only door. A constructor taking a key is a second one."""
    with pytest.raises(TypeError):
        Signer(env="testnet", account_address=FAKE_ADDRESS, private_key=FAKE_KEY)  # type: ignore[call-arg]
