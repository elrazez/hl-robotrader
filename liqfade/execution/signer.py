"""The only module that ever reads an agent private key.

CLAUDE.md rule 6: the key is read once from the environment here and is never logged,
printed, or passed around as a string beyond this module. Everything that follows is in
service of that one sentence.

How the key is contained. `Signer` holds an `eth_account` `LocalAccount`, never the hex
string, and it is not a dataclass: a dataclass repr renders every field, which is how a
key ends up in a traceback that gets pasted into a chat window. `__repr__` and
`__str__` are written by hand and name the two public addresses only. The account address
and the agent address are both public (the agent address is what `app.hyperliquid.xyz/API`
shows you) so both are safe to log, and logging them is what makes "signed by the wrong
agent" a diagnosable problem.

`load_signer` is the only constructor. It refuses in four ways before it ever parses a
key, because each refusal is cheaper than the mistake it prevents:

1. **paper has no signer.** Paper simulates every fill. A signer existing in that process
   is a bug, and it is better to raise here than to discover the process could sign.
2. **mainnet needs both gates**, `LIQFADE_ENV=mainnet` and `LIQFADE_LIVE_TRADING=1`, and no
   kill flag. `Settings.live_trading_enabled` already says this and the order path checks
   it too; this is the second, independent refusal CLAUDE.md rule 3 asks for, placed at
   the one point where an order becomes signable at all.
3. **the placeholder address is refused.** `.env.example` ships `HL_TESTNET_ACCOUNT_ADDRESS=0x`
   and a half filled env file is the normal failure, not an exotic one.
4. **a malformed key raises without echoing itself.** `eth_account` puts the offending
   value in its own exception message, so the parse happens inside a handler that throws
   the original away.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from liqfade.config.models import Settings
from liqfade.log import get_logger

if TYPE_CHECKING:
    from eth_account.signers.local import LocalAccount

log = get_logger(__name__)

__all__ = ["ACCOUNT_ENV", "AGENT_KEY_ENV", "Signer", "SignerError", "load_signer"]

# One agent per environment, per docs/HYPERLIQUID.md. Testnet and mainnet keys never
# share a variable, so a mistyped LIQFADE_ENV cannot sign a mainnet order with what the
# owner believed was the testnet agent.
ACCOUNT_ENV = {
    "testnet": "HL_TESTNET_ACCOUNT_ADDRESS",
    "mainnet": "HL_MAINNET_ACCOUNT_ADDRESS",
}
AGENT_KEY_ENV = {
    "testnet": "HL_TESTNET_AGENT_PRIVATE_KEY",
    "mainnet": "HL_MAINNET_AGENT_PRIVATE_KEY",
}

ADDRESS_LEN = 42  # "0x" plus 20 bytes of hex


class SignerError(RuntimeError):
    """The process cannot sign, and says why without quoting anything secret."""


class Signer:
    """A loaded agent key and the two public addresses that go with it.

    Build it with `load_signer`. The constructor is deliberately not a public door: it
    takes an already parsed `LocalAccount` rather than a hex string, so there is no
    signature anywhere in the package that accepts a private key as text.
    """

    __slots__ = ("_account", "account_address", "base_url", "env")

    def __init__(
        self,
        *,
        env: str,
        account_address: str,
        base_url: str,
        account: LocalAccount,
    ) -> None:
        self.env = env
        self.account_address = account_address
        self.base_url = base_url
        self._account = account

    @property
    def agent_address(self) -> str:
        """The agent wallet's own address, derived from the key. Public, safe to log."""
        return str(self._account.address)

    def exchange(self) -> Exchange:
        """A signing client bound to the configured URL.

        The URL comes from config rather than the SDK's default so a testnet run cannot
        drift onto mainnet through a default argument. `account_address` is the main
        wallet; the agent signs for it.
        """
        return Exchange(
            self._account,
            base_url=self.base_url,
            account_address=self.account_address,
        )

    def info(self) -> Info:
        """A read only client on the same URL. No key involved, kept here for symmetry."""
        return Info(base_url=self.base_url, skip_ws=True)

    def __repr__(self) -> str:
        return (
            f"Signer(env={self.env!r}, account_address={self.account_address!r}, "
            f"agent_address={self.agent_address!r})"
        )

    __str__ = __repr__


def load_signer(settings: Settings) -> Signer:
    """Read the environment's agent key and return a `Signer`, or raise `SignerError`.

    Args:
        settings: resolved config. Supplies the environment, the API URL and the kill
            file path.

    Returns:
        A `Signer` for `settings.env`.

    Raises:
        SignerError: in paper mode, when either variable is missing or a placeholder, when
            mainnet's two gates are not both satisfied, when the kill flag exists, or when
            the key does not parse. The message never contains a key.
    """
    env = settings.env
    if env == "paper":
        raise SignerError(
            "paper mode simulates every fill and must not be able to sign. "
            "Set LIQFADE_ENV=testnet to place real testnet orders."
        )
    if env == "mainnet":
        blocked = settings.live_trading_block_reason()
        if blocked is not None:
            raise SignerError(f"refusing to load a mainnet agent key: {blocked}")

    account_var, key_var = ACCOUNT_ENV[env], AGENT_KEY_ENV[env]
    address = os.environ.get(account_var, "").strip()
    key = os.environ.get(key_var, "").strip()

    if not key:
        raise SignerError(f"{key_var} is not set. See .env.example; the key never leaves the box.")
    if len(address) != ADDRESS_LEN or not address.startswith("0x"):
        raise SignerError(
            f"{account_var} is not an account address. It must be the main wallet's "
            f"0x address, {ADDRESS_LEN} characters, not the agent's and not the "
            f"'0x' placeholder from .env.example."
        )

    try:
        account: LocalAccount = Account.from_key(key)
    except Exception as exc:
        # Deliberately not chained and deliberately not quoting `exc`: eth_account puts
        # the offending value in its own message.
        raise SignerError(
            f"{key_var} is not a valid private key ({type(exc).__name__}). "
            f"It is the agent key from app.hyperliquid.xyz/API, 32 bytes of hex."
        ) from None
    finally:
        del key

    log.info(
        "signer_loaded",
        env=env,
        account_address=address,
        agent_address=str(account.address),
        base_url=settings.venues.hyperliquid.api_url,
    )
    return Signer(
        env=env,
        account_address=address,
        base_url=settings.venues.hyperliquid.api_url,
        account=account,
    )
