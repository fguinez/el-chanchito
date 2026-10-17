"""Banco Santander Chile: one headless login, data read from the SPA's own JSON.

The private site (`mibanco.santander.cl`) is an Angular SPA whose data comes
from two JSON APIs, both called by the SPA itself with a bearer token minted at
login:

  * products and balances: `api-dsk.santander.cl/perdsk/datosCliente/
    cruceProductosOnline`, fired automatically by the dashboard right after
    login. Its `MATRIZCAPTACIONES.e1` rows are the deposit products (cuentas
    corrientes, vistas, ...), every field a string.
  * movements: `openbanking.santander.cl/account_balances_transactions_and_
    withholdings_retail/v1/current-accounts/transactions` (the same endpoint
    serves a cuenta vista), fired when the user opens an account's movements
    view from the dashboard.

This module only drives the browser and hands those raw payloads to the pure
parsers below, which are unit-tested with synthetic dicts and never touch a
browser. Nothing is read from the DOM except the controls clicked to log in
and to open the movements view.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --- Errors -------------------------------------------------------------------
#
# Every message is safe for `scraper_runs.error_message` (shown on the
# dashboard): no RUT, password, token, account number, URL with ids or body.


class SantanderError(RuntimeError):
    """A Santander session failed in a way that cost the whole leg."""


class SantanderBlockedError(SantanderError):
    """Santander's edge served its block page; never retried."""


class SantanderLoginError(SantanderError):
    """The login was rejected or never reached the private site."""


class SantanderStepUpError(SantanderError):
    """The login asked for a second factor; the scraper cannot answer it."""


class SantanderPayloadError(SantanderError):
    """A response the scraper depends on had an unrecognisable shape."""


# --- What a session yields ----------------------------------------------------


@dataclass(frozen=True)
class SantanderAccount:
    """One deposit product row of `MATRIZCAPTACIONES`.

    `contract` and `office` are the raw zero-padded strings; together they form
    the movements API's `accountId`. `balance` is the available balance
    (`MONTODISPONIBLE`) in the product's currency.
    """

    contract: str
    office: str
    glosa: str
    currency: str
    balance: Decimal

    @property
    def account_id(self) -> str:
        return f"{self.office}{self.contract}"

    @property
    def display_number(self) -> str:
        """The contract number without its zero padding, for display only."""
        return self.contract.lstrip("0") or "0"


@dataclass(frozen=True)
class ParsedProducts:
    """The CLP cuentas vista of a products payload, plus what was left out."""

    accounts: list[SantanderAccount] = field(default_factory=list)
    ignored: int = 0
    invalid: int = 0


@dataclass(frozen=True)
class SantanderMovement:
    """One movement of the transactions API, normalised.

    `raw_amount`, `raw_balance` and `flag` are kept verbatim because the
    fallback `external_id` hashes them (see `external_id_for`). `new_balance`
    is the running balance after the movement, when reported.
    """

    transaction_date: date
    accounting_date: Optional[date]
    amount: Decimal
    currency: str
    description: str
    movement_number: Optional[str]
    raw_amount: str
    raw_balance: str
    flag: str
    new_balance: Optional[Decimal] = None


@dataclass(frozen=True)
class ParsedMovements:
    """The usable movements of a transactions payload, plus skip counts.

    `zero` counts zero-amount rows (routine, never a warning), `skipped` the
    unusable ones (no date, unparseable amount, a CLP fraction) and
    `sign_mismatches` the rows whose sign and debit/credit flag disagree.
    """

    movements: list[SantanderMovement] = field(default_factory=list)
    skipped: int = 0
    sign_mismatches: int = 0
    zero: int = 0


# --- Pure helpers (unit-tested; no browser) -----------------------------------

_CENTS_RE = re.compile(r"^\s*([+-]?)\s*(\d+)\s*([+-]?)\s*$")
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_DIGITS_RE = re.compile(r"^\d+$")
_SPACES_RE = re.compile(r"\s+")
_CENT = Decimal("0.01")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_cents(raw: Any) -> Optional[Decimal]:
    """A zero-padded string of cents ("000000000250000000") as a Decimal.

    Santander pads every amount with leading zeros and keeps two implied
    decimals: the last two digits are cents. Money out carries a "-" after the
    digits ("000000099999900-"); a leading sign is accepted too. None for
    anything else (blank, letters, a decimal point, two signs).
    """
    if not isinstance(raw, str):
        return None
    match = _CENTS_RE.match(raw)
    if match is None:
        return None
    leading, digits, trailing = match.groups()
    if leading and trailing:
        return None
    value = (Decimal(int(digits)) * _CENT).quantize(_CENT)
    return -value if "-" in (leading, trailing) else value


def parse_iso_date(raw: Any) -> Optional[date]:
    """The calendar date at the start of an ISO string, as printed.

    The API reports dates as "YYYY-MM-DD" optionally followed by a time; only
    the printed date is taken, never shifted by a time zone.
    """
    if not isinstance(raw, str):
        return None
    match = _ISO_DATE_RE.match(raw)
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value()


def is_clp_vista(row: dict) -> bool:
    """True for a `MATRIZCAPTACIONES` row that is a cuenta vista in CLP.

    The short label is the only product-type field whose meaning is known
    ("CTA VISTA ..."); `PRODUCTO` / `SUBPRODUCTO` are opaque codes.
    """
    glosa = _text(row.get("GLOSACORTA")).upper()
    currency = _text(row.get("CODIGOMONEDA")).upper()
    return currency == "CLP" and "VISTA" in glosa


def _rows(matrix: Any) -> list:
    """`e1` as a list: SOAP-ish backends send one item as a bare dict."""
    if not isinstance(matrix, dict):
        return []
    rows = matrix.get("e1")
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def parse_products(payload: Any) -> ParsedProducts:
    """The CLP cuentas vista of a `cruceProductosOnline` payload.

    Rows of any other currency or label are counted in `ignored` and dropped:
    the scraper only models a CLP cuenta vista. A vista row whose contract or
    balance is unusable is counted in `invalid`. The personal fields under
    `ESCALARES` are never read.

    The balance is `MONTODISPONIBLE` read as cents (last two digits). That
    interpretation is shared with the movements API's amounts and was checked
    live only against a zero balance, so `fetch_session` cross-checks it with
    the latest movement's running balance and logs a warning when they
    disagree. A CLP balance with a fraction is treated as invalid rather than
    rounded, since it would mean the interpretation is wrong.

    Raises `SantanderPayloadError` when `DATA.OUTPUT` is missing altogether; a
    payload without `MATRIZCAPTACIONES` simply has no accounts.
    """
    output = None
    if isinstance(payload, dict):
        data = payload.get("DATA")
        if isinstance(data, dict):
            output = data.get("OUTPUT")
    if not isinstance(output, dict):
        raise SantanderPayloadError("Santander product list had an unexpected shape")
    matrices = output.get("MATRICES")
    rows = _rows(matrices.get("MATRIZCAPTACIONES")) if isinstance(matrices, dict) else []

    accounts: list[SantanderAccount] = []
    ignored = invalid = 0
    for row in rows:
        if not is_clp_vista(row):
            ignored += 1
            continue
        contract = _text(row.get("NUMEROCONTRATO"))
        office = _text(row.get("OFICINACONTRATO"))
        balance = parse_cents(row.get("MONTODISPONIBLE"))
        if (
            not _DIGITS_RE.match(contract)
            or not _DIGITS_RE.match(office)
            or balance is None
            or not _is_whole(balance)
        ):
            invalid += 1
            continue
        accounts.append(
            SantanderAccount(
                contract=contract,
                office=office,
                glosa=_text(row.get("GLOSACORTA")),
                currency="CLP",
                balance=balance.quantize(Decimal(1)),
            )
        )
    # A stable order (lowest contract first), not the API's, so a holder of
    # several vistas always gets the same one read.
    accounts.sort(key=lambda account: int(account.contract))
    return ParsedProducts(accounts=accounts, ignored=ignored, invalid=invalid)


def _description(row: dict) -> str:
    """The bank's own text for a movement, whitespace collapsed."""
    for key in ("observation", "expandedCode"):
        text = _SPACES_RE.sub(" ", _text(row.get(key)))
        if text:
            return text
    return "Movimiento Santander"


def _movement_number(raw: Any) -> Optional[str]:
    """`movementNumber` without its zero padding; None when absent or all zeros."""
    text = _text(raw)
    if not _DIGITS_RE.match(text):
        return None
    return text.lstrip("0") or None


SKIP_INVALID = "invalid"
SKIP_ZERO = "zero"
SKIP_SIGN_MISMATCH = "sign_mismatch"


def parse_movement(
    row: Any, currency: str = "CLP"
) -> tuple[Optional[SantanderMovement], Optional[str]]:
    """One raw movement, or None and the reason it must be skipped.

    Returns `(movement, None)` or `(None, reason)`. The sign comes from the
    trailing "-" of `movementAmount` and is cross-checked with
    `chargePaymentFlag` ("D" is money out, "H" money in): a row where the two
    disagree is `SKIP_SIGN_MISMATCH`, since either reading could be wrong. A
    zero amount is `SKIP_ZERO`; no date, an unparseable amount or a CLP amount
    with a fraction is `SKIP_INVALID`.
    """
    if not isinstance(row, dict):
        return None, SKIP_INVALID
    transaction_date = parse_iso_date(row.get("transactionDate"))
    accounting_date = parse_iso_date(row.get("accountingDate"))
    if transaction_date is None:
        transaction_date = accounting_date
    raw_amount = _text(row.get("movementAmount"))
    amount = parse_cents(raw_amount)
    if transaction_date is None or amount is None:
        return None, SKIP_INVALID
    if amount == 0:
        return None, SKIP_ZERO
    if currency == "CLP":
        if not _is_whole(amount):
            return None, SKIP_INVALID
        amount = amount.quantize(Decimal(1))
    flag = _text(row.get("chargePaymentFlag")).upper()
    if (flag == "D" and amount > 0) or (flag == "H" and amount < 0):
        return None, SKIP_SIGN_MISMATCH
    raw_balance = _text(row.get("newBalance"))
    return (
        SantanderMovement(
            transaction_date=transaction_date,
            accounting_date=accounting_date,
            amount=amount,
            currency=currency,
            description=_description(row),
            movement_number=_movement_number(row.get("movementNumber")),
            raw_amount=raw_amount,
            raw_balance=raw_balance,
            flag=flag,
            new_balance=parse_cents(raw_balance),
        ),
        None,
    )


def parse_movements(payload: Any, currency: str = "CLP") -> ParsedMovements:
    """Every usable movement of a transactions payload.

    Raises `SantanderPayloadError` when `movements` is not a list; an empty
    list is a valid, quiet account. Skipped rows are counted, not logged one
    by one (see `parse_movement` for the rules).
    """
    rows = payload.get("movements") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SantanderPayloadError("Santander movements had an unexpected shape")
    movements: list[SantanderMovement] = []
    counts = {SKIP_INVALID: 0, SKIP_ZERO: 0, SKIP_SIGN_MISMATCH: 0}
    for row in rows:
        movement, reason = parse_movement(row, currency)
        if movement is not None:
            movements.append(movement)
        else:
            counts[reason] += 1
    return ParsedMovements(
        movements=movements,
        skipped=counts[SKIP_INVALID],
        sign_mismatches=counts[SKIP_SIGN_MISMATCH],
        zero=counts[SKIP_ZERO],
    )


def external_id_for(movement: SantanderMovement) -> str:
    """The `external_id` of one Santander movement.

    ``san_mov_<accountingDate>_<movementNumber>``
        The bank's own movement number, without its zero padding, scoped by
        the posting date: the number may be a per-posting-day sequence, so the
        date it is scoped by must be the one the bank numbers by. It was
        identical across two separate logins.

    ``san_fp_<md5[:16]>``
        The fallback when a movement has no usable number or no posting date
        (the occurrence date is never substituted for it): a hash of the
        description-free fields only (currency, both dates, the raw amount,
        the raw running balance and the debit/credit flag). The running
        balance keeps two same-day movements of the same amount apart. The
        description (`observation`, `expandedCode`) is never hashed: banks
        reword it.
    """
    if movement.movement_number and movement.accounting_date:
        return f"san_mov_{movement.accounting_date.isoformat()}_{movement.movement_number}"
    parts = (
        movement.currency,
        movement.transaction_date.isoformat(),
        movement.accounting_date.isoformat() if movement.accounting_date else "",
        movement.raw_amount,
        movement.raw_balance,
        movement.flag,
    )
    digest = hashlib.md5("|".join(parts).encode()).hexdigest()[:16]
    return f"san_fp_{digest}"


def latest_balance(movements: list[SantanderMovement]) -> Optional[Decimal]:
    """The running balance after the most recent movement, when reported.

    "Most recent" is the latest posting (else occurrence) date, then the
    highest movement number; movements without a balance are ignored.
    """
    def key(movement: SantanderMovement) -> tuple:
        day = movement.accounting_date or movement.transaction_date
        number = int(movement.movement_number) if movement.movement_number else -1
        return (day, number)

    candidates = [m for m in movements if m.new_balance is not None]
    if not candidates:
        return None
    return max(candidates, key=key).new_balance


def transactions_request_body(account: SantanderAccount, opening: date, closing: date) -> dict:
    """The body the SPA sends to the transactions API for one account."""
    return {
        "accountId": account.account_id,
        "currency": account.currency,
        "commercialGroup": "",
        "openingDate": opening.isoformat(),
        "closingDate": closing.isoformat(),
    }
