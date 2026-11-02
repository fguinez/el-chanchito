"""Tarjeta Lider Bci billing: the USD card rate, the latest statement and past ones.

Read from inside the signed-in portal tab with the portal's own read-only API
(api-ssff.retailcard.cl), the same GETs its pages make, authorized with the
bearer token the SPA keeps in its tab's sessionStorage (`token`, next to the
customer id in `user`):

  * `ssff-api-helpers/ssff-api-helpers/api/getTipoCambio/<user>`: the dollar
    the card applies ("Valor del dólar hoy" under "Cupos de tu tarjeta", and
    the "Ver dólar histórico" table), about 30 business days, newest first,
    as `{data: [{fechaDolar: "DD/MM/YYYY", valorDolar: "1000.0000"}]}`. The
    newest becomes the USD card's product-scoped `ScrapedExchangeRate`.
  * `ssff-api-creditcard/api/currentBilling?rut=<user>`: the "Último Periodo
    Facturado" figures the statements and payments pages print: `monthCharges`
    (Cargos del mes), `totalToPay` (Total a pagar), `minimumPayment`,
    `billingDate` (Fecha de facturación) and `payUntil` (Pagar hasta el).
  * `ssff-api-creditcard/informacionPeriodo?rut=<user>`: the statements the
    account-statements page lists (`periodo` YYYYMM), and per period
    `ssff-api-creditcard/estadoCuentaPDF?rut=<user>&periodo=MM/YYYY`: the
    statement PDF, base64. Its text (pypdf, layout mode) gives the billed
    period ("Período Facturado" Desde/Hasta), the due date, the totals, the
    next closing ("Próximo Período de Facturación") and every movement billed
    on it (date and amount).

Closing date: the portal's "Fecha de facturación" (the PDF's "Fecha Estado de
Cuenta") is the day AFTER the billed period ends: the period runs from that
day of the previous month to the day before it. So the closing date stored
(`statement_date`, `next_statement_date` and each billed row's
`statement_date`) is the period's last day ("Hasta"), the day whose
movements are still on the statement; without a readable period it is the
day before the portal's billing date.

Privacy and safety: the in-page script never leaves the portal host, only
GETs, and hands Python only the fields read here (the customer id and the
token stay in the page). The statement PDFs do reach Python, held in memory
only. No payment endpoint is called and the payment gateway
(pasarelapagos.retailcard.cl) is never opened: its "Facturado pendiente"
needs a payment hand-off the portal starts (`ssff-api-payment/.../xmlpayment`),
which is never touched.

Everything here is best-effort: a call or a PDF that fails is a run warning,
never a failed run.
"""

import base64
import binascii
import datetime
import io
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from scrapers.base import ProductRef, ScrapedExchangeRate

logger = logging.getLogger(__name__)

INSTITUTION = "bci_lider"
SOURCE_LABEL = "Valor del dólar de la tarjeta"
# The scraper's single USD card (see `card_products_from_text`).
_CARD_USD = ProductRef(kind="credit_card", currency="USD", external_ref=None)

PORTAL_HOST = "www.liderbciserviciosfinancieros.cl"
API_BASE = "https://api-ssff.retailcard.cl/"
# The account-statements page lists 8; the cap keeps a changed API from
# downloading more PDFs than that.
MAX_STATEMENTS = 8
# A statement is a few pages; anything far larger is not parsed (pypdf can
# spend long on a malformed file).
MAX_PDF_BASE64_CHARS = 8_000_000
MAX_PDF_PAGES = 20
# Per fetch, inside the page (`page.evaluate` itself has no timeout).
FETCH_TIMEOUT_MS = 20000
# The whole script: once spent, the remaining PDFs are skipped (reported as
# not read) so a slow API cannot hold the session.
TOTAL_TIMEOUT_MS = 90000

# Every `return` is an object literal with only these keys: a `status`, an
# HTTP `code`, and on success the fields named below. The customer id and the
# token never leave the page. The script runs only on the portal host and only
# fetches the portal's own API (`API_BASE`), nothing else.
_BILLING_JS = """
async ({host, api, maxStatements, timeoutMs, totalMs}) => {
  if (location.hostname !== host) return {status: 'off_portal'};
  const deadline = Date.now() + totalMs;
  const token = sessionStorage.getItem('token');
  const user = sessionStorage.getItem('user');
  if (!token || !user) return {status: 'no_session'};
  const id = encodeURIComponent(user);
  const headers = {authorization: 'bearer ' + token, accept: 'application/json'};
  const get = async (path) => {
    const signal = typeof AbortSignal.timeout === 'function'
      ? AbortSignal.timeout(timeoutMs) : undefined;
    try {
      const response = await fetch(api + path, {headers, signal});
      if (!response.ok) return {status: 'http', code: response.status};
      return {status: 'ok', body: await response.json()};
    } catch (error) {
      return {status: error && error.name === 'TimeoutError' ? 'timeout' : 'error'};
    }
  };
  const str = (value) => (typeof value === 'string' ? value : null);
  const num = (value) => (typeof value === 'number' || typeof value === 'string' ? value : null);

  const out = {status: 'ok'};

  const rate = await get('ssff-api-helpers/ssff-api-helpers/api/getTipoCambio/' + id);
  out.rate = {status: rate.status, code: rate.code};
  if (rate.status === 'ok') {
    const data = rate.body && Array.isArray(rate.body.data) ? rate.body.data : [];
    out.rate.data = data.map((row) => ({
      date: str(row && row.fechaDolar), value: num(row && row.valorDolar),
    }));
  }

  const billing = await get('ssff-api-creditcard/api/currentBilling?rut=' + id);
  out.billing = {status: billing.status, code: billing.code};
  if (billing.status === 'ok' && billing.body) {
    const b = billing.body;
    out.billing.data = {
      monthCharges: num(b.monthCharges), totalToPay: num(b.totalToPay),
      minimumPayment: num(b.minimumPayment), billingDate: str(b.billingDate),
      payUntil: str(b.payUntil),
    };
  }

  const periods = await get('ssff-api-creditcard/informacionPeriodo?rut=' + id);
  out.periods = {status: periods.status, code: periods.code};
  out.statements = [];
  if (periods.status === 'ok' && Array.isArray(periods.body)) {
    const listed = periods.body
      .map((row) => str(row && row.periodo))
      .filter((periodo) => periodo && /^\\d{6}$/.test(periodo))
      .sort().reverse().slice(0, maxStatements);
    for (const periodo of listed) {
      if (Date.now() >= deadline) {
        out.statements.push({periodo, status: 'skipped', pdf: null});
        continue;
      }
      const query = periodo.slice(4, 6) + '/' + periodo.slice(0, 4);
      const pdf = await get(
        'ssff-api-creditcard/estadoCuentaPDF?rut=' + id + '&periodo=' + encodeURIComponent(query)
      );
      out.statements.push({
        periodo, status: pdf.status, code: pdf.code,
        pdf: pdf.status === 'ok' && pdf.body ? str(pdf.body.pdfBytesEnBase64) : null,
      });
    }
  }
  return out;
}
"""


# --- Pure helpers (unit-tested; no browser) -----------------------------------

# What `exchange_rates.rate` holds: plain digits with a dot decimal, at most 8
# decimals. The portal sends a dot-decimal string (4 decimals observed).
_RATE_RE = re.compile(r"^\d{1,12}(\.\d{1,8})?$")
_DATE = r"(\d{2}/\d{2}/\d{4})"


def parse_date(raw: Any) -> Optional[datetime.date]:
    """A "DD/MM/YYYY" date; None when it isn't one."""
    if not isinstance(raw, str):
        return None
    match = re.fullmatch(r"\s*" + _DATE + r"\s*", raw)
    if match is None:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_rate(raw: Any) -> Optional[Decimal]:
    """The portal's `valorDolar` as a positive Decimal; None when unusable.

    A dot-decimal string or a JSON number. Anything else (a comma decimal, an
    exponent, NaN, zero, a bool) is None: no rate beats a wrong one.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    text = raw.strip() if isinstance(raw, str) else str(raw)
    if not _RATE_RE.fullmatch(text):
        return None
    rate = Decimal(text)
    return rate if rate > 0 else None


def parse_amount(raw: Any) -> Optional[float]:
    """A billing figure: a JSON number or a plain-digit string; else None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and re.fullmatch(r"\s*-?\d{1,15}(\.\d{1,2})?\s*", raw):
        return float(raw)
    return None


def exchange_rate_from_rows(rows: Any) -> Optional[ScrapedExchangeRate]:
    """The USD card's rate from the newest dated, parseable history row."""
    if not isinstance(rows, list):
        return None
    best: Optional[tuple[datetime.date, Decimal]] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = parse_date(row.get("date"))
        rate = parse_rate(row.get("value"))
        if day is None or rate is None:
            continue
        if best is None or day > best[0]:
            best = (day, rate)
    if best is None:
        return None
    return ScrapedExchangeRate(
        institution=INSTITUTION,
        base_currency="USD",
        quote_currency="CLP",
        rate=best[1],
        product=_CARD_USD,
        source=SOURCE_LABEL,
        reported_as_of=best[0],
    )


@dataclass(frozen=True)
class CurrentBilling:
    """The portal's "Último Periodo Facturado" figures (currentBilling)."""

    billing_date: Optional[datetime.date]
    payment_due_date: Optional[datetime.date]
    total_to_pay: Optional[float]
    month_charges: Optional[float]
    minimum_payment: Optional[float]


def current_billing_from_data(data: Any) -> Optional[CurrentBilling]:
    """Shape the currentBilling fields; None when nothing in it is usable."""
    if not isinstance(data, dict):
        return None
    billing = CurrentBilling(
        billing_date=parse_date(data.get("billingDate")),
        payment_due_date=parse_date(data.get("payUntil")),
        total_to_pay=parse_amount(data.get("totalToPay")),
        month_charges=parse_amount(data.get("monthCharges")),
        minimum_payment=parse_amount(data.get("minimumPayment")),
    )
    if all(value is None for value in vars(billing).values()):
        return None
    return billing


@dataclass(frozen=True)
class LiderStatement:
    """One statement (estado de cuenta) read off its PDF text.

    `issued_on` is "Fecha Estado de Cuenta" (the portal's "Fecha de
    facturación"); `period_start`/`period_end` the billed period, whose last
    day is the closing date. `movements` are (date, amount) pairs signed like
    the scraper's movements: a charge negative, a payment or credit positive.
    """

    issued_on: Optional[datetime.date]
    period_start: Optional[datetime.date]
    period_end: Optional[datetime.date]
    payment_due_date: Optional[datetime.date]
    billed_amount: Optional[float]
    minimum_payment: Optional[float]
    next_period_end: Optional[datetime.date]
    movements: list[tuple[datetime.date, int]] = field(default_factory=list)

    @property
    def closing_date(self) -> Optional[datetime.date]:
        """The statement's last billed day: the period's end, else the day
        before the issue date (the period ends the day before it is issued)."""
        if self.period_end is not None:
            return self.period_end
        return closing_from_issue_date(self.issued_on)


def closing_from_issue_date(issued_on: Optional[datetime.date]) -> Optional[datetime.date]:
    """The closing date of a statement issued on `issued_on` (the portal's
    "Fecha de facturación"): its billed period ends the day before."""
    return issued_on - datetime.timedelta(days=1) if issued_on is not None else None


_ISSUED_RE = re.compile(r"Fecha\s+Estado\s+de\s+Cuenta\s+" + _DATE, re.I)
_PERIOD_RE = re.compile(r"Per[ií]odo\s+Facturado\s+" + _DATE + r"\s+" + _DATE, re.I)
_DUE_RE = re.compile(r"Pagar\s+Hasta\s+" + _DATE, re.I)
_BILLED_RE = re.compile(r"Monto\s+Total\s+Facturado\s+\$\s*(-?[\d.]{1,15})", re.I)
_MINIMUM_RE = re.compile(r"Monto\s+M[ií]nimo\s+a\s+Pagar\s+\$\s*(-?[\d.]{1,15})", re.I)
_NEXT_RE = re.compile(
    r"Pr[oó]ximo\s+Per[ií]odo\s+de\s+Facturaci[oó]n\s+" + _DATE + r"\s+" + _DATE, re.I
)
# The movements run from "Período Actual" (section II.2) to the totals block.
_DETAIL_RE = re.compile(r"Per[ií]odo\s+Actual", re.I)
_DETAIL_END_RE = re.compile(r"Monto\s+Total\s+Facturado", re.I)
# One movement line: [place] DATE  DESCRIPTION  $  AMOUNT [more columns]. The
# layout text keeps each row on one line, so nothing here crosses a newline.
# Only the first "$" figure is read (the operation amount); a minus sign may
# sit on either side of the "$".
_ROW_RE = re.compile(
    r"^[^\n]*?" + _DATE + r"[ \t]{2,}\S[^\n]*?[ \t](-?)\$[ \t]*(-?\d{1,3}(?:\.\d{3})*)\b",
    re.M,
)


def _pesos(raw: Optional[str]) -> Optional[int]:
    """A "1.234.567" / "-999.999" statement figure as whole pesos."""
    if raw is None:
        return None
    try:
        return int(raw.replace(".", ""))
    except ValueError:
        return None


def statement_from_text(text: Optional[str]) -> Optional[LiderStatement]:
    """Read one statement's figures and movements off its PDF layout text.

    Returns None when the text names neither an issue date nor a billed period
    (not a statement). A figure that does not parse is None. Movements of 0
    (an informative fee line) are skipped.
    """
    if not text:
        return None

    def date_of(pattern: re.Pattern, group: int = 1) -> Optional[datetime.date]:
        match = pattern.search(text)
        return parse_date(match.group(group)) if match else None

    def pesos_of(pattern: re.Pattern) -> Optional[float]:
        match = pattern.search(text)
        value = _pesos(match.group(1)) if match else None
        return float(value) if value is not None else None

    issued_on = date_of(_ISSUED_RE)
    period_start = date_of(_PERIOD_RE, 1)
    period_end = date_of(_PERIOD_RE, 2)
    if issued_on is None and period_end is None:
        return None

    detail = _DETAIL_RE.search(text)
    region = text[detail.end():] if detail else ""
    end = _DETAIL_END_RE.search(region)
    if end:
        region = region[: end.start()]
    movements: list[tuple[datetime.date, int]] = []
    for match in _ROW_RE.finditer(region):
        day = parse_date(match.group(1))
        sign, figure = match.group(2), match.group(3)
        charged = _pesos(figure.lstrip("-"))
        if day is None or not charged:
            continue
        if sign or figure.startswith("-"):
            charged = -charged
        # The statement lists what was charged; the movement is its opposite.
        movements.append((day, -charged))

    return LiderStatement(
        issued_on=issued_on,
        period_start=period_start,
        period_end=period_end,
        payment_due_date=date_of(_DUE_RE),
        billed_amount=pesos_of(_BILLED_RE),
        minimum_payment=pesos_of(_MINIMUM_RE),
        next_period_end=date_of(_NEXT_RE, 2),
        movements=movements,
    )


def statement_text_from_pdf(data: bytes) -> str:
    """The statement PDF's text, row by row (pypdf's layout mode)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("statement PDF has too many pages")
    return "\n".join(
        page.extract_text(extraction_mode="layout") or "" for page in reader.pages
    )


def billing_metrics(
    current: Optional[CurrentBilling], statements: list[LiderStatement]
) -> dict[str, Any]:
    """The CLP card's statement fields of `CreditCardMetrics` (only those known).

    The closing dates come from the newest statement PDF when it is the one
    the portal calls its last billed period (same issue date, or no portal
    figures to compare), else the day before the portal's billing date stands
    in (its period ends the day before it is issued). The amounts
    and the due date prefer the portal's figures and fall back to the PDF's.
    `billed_amount` is "Total a pagar" (the statement's "Monto Total
    Facturado").
    """
    latest = max(
        (s for s in statements if s.issued_on or s.period_end),
        key=lambda s: s.issued_on or s.period_end,
        default=None,
    )
    if latest is not None and current is not None and current.billing_date is not None:
        same = (
            latest.issued_on == current.billing_date
            if latest.issued_on is not None
            else latest.period_end == closing_from_issue_date(current.billing_date)
        )
        if not same:
            latest = None

    fields: dict[str, Any] = {}
    if latest is not None:
        fields.update(
            statement_date=latest.closing_date,
            next_statement_date=latest.next_period_end,
            payment_due_date=latest.payment_due_date,
            billed_amount=latest.billed_amount,
            minimum_payment=latest.minimum_payment,
        )
    if current is not None:
        if latest is None:
            fields["statement_date"] = closing_from_issue_date(current.billing_date)
        billed = (
            current.total_to_pay
            if current.total_to_pay is not None
            else current.month_charges
        )
        for name, value in (
            ("payment_due_date", current.payment_due_date),
            ("billed_amount", billed),
            ("minimum_payment", current.minimum_payment),
        ):
            if value is not None:
                fields[name] = value
    return {name: value for name, value in fields.items() if value is not None}


# --- Browser plumbing ---------------------------------------------------------


@dataclass
class BillingRead:
    """What one billing read got: each part may be missing (see `warnings`)."""

    exchange_rates: list[ScrapedExchangeRate] = field(default_factory=list)
    current: Optional[CurrentBilling] = None
    statements: list[LiderStatement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _failure(part: Any) -> str:
    """A part's status and HTTP code alone; never a payload value."""
    if not isinstance(part, dict):
        return "missing"
    status = part.get("status")
    status = status if status in {"ok", "http", "timeout", "error"} else "unexpected"
    code = part.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return f"{status} {code}"
    return status


def billing_from_result(result: Any) -> BillingRead:
    """Interpret the in-page script's result (pure but for the PDF parse)."""
    read = BillingRead()
    if not isinstance(result, dict) or result.get("status") != "ok":
        status = result.get("status") if isinstance(result, dict) else None
        reason = status if status in {"off_portal", "no_session"} else "no result"
        read.warnings.append(f"BciLider: billing not read ({reason})")
        return read

    rate_part = result.get("rate")
    rate = None
    if isinstance(rate_part, dict) and rate_part.get("status") == "ok":
        rate = exchange_rate_from_rows(rate_part.get("data"))
    if rate is not None:
        read.exchange_rates.append(rate)
    else:
        read.warnings.append(f"BciLider: no USD rate ({_failure(rate_part)})")

    billing_part = result.get("billing")
    if isinstance(billing_part, dict) and billing_part.get("status") == "ok":
        read.current = current_billing_from_data(billing_part.get("data"))
    if read.current is None:
        read.warnings.append(f"BciLider: no current billing ({_failure(billing_part)})")

    periods = result.get("periods")
    if not (isinstance(periods, dict) and periods.get("status") == "ok"):
        read.warnings.append(f"BciLider: no statement list ({_failure(periods)})")
    statements = result.get("statements")
    unread = 0
    for entry in statements if isinstance(statements, list) else []:
        statement = _statement_from_entry(entry)
        if statement is None:
            unread += 1
        else:
            read.statements.append(statement)
    if unread:
        read.warnings.append(f"BciLider: {unread} statement(s) not read")
    if read.statements and not any(s.movements for s in read.statements):
        read.warnings.append("BciLider: no movements read from any statement")
    return read


def _statement_from_entry(entry: Any) -> Optional[LiderStatement]:
    """Decode and parse one statement PDF; None when anything fails."""
    if not isinstance(entry, dict) or entry.get("status") != "ok":
        return None
    encoded = entry.get("pdf")
    if not isinstance(encoded, str) or not encoded:
        return None
    if len(encoded) > MAX_PDF_BASE64_CHARS:
        logger.info("BciLider: a statement PDF is too large to parse")
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
        text = statement_text_from_pdf(data)
    except (binascii.Error, ValueError):
        logger.info("BciLider: a statement PDF did not decode")
        return None
    except Exception:
        logger.info("BciLider: a statement PDF did not parse", exc_info=True)
        return None
    return statement_from_text(text)


def read_billing(page) -> BillingRead:
    """Run the in-page billing script on a signed-in portal tab and read it."""
    try:
        result = page.evaluate(
            _BILLING_JS,
            {
                "host": PORTAL_HOST,
                "api": API_BASE,
                "maxStatements": MAX_STATEMENTS,
                "timeoutMs": FETCH_TIMEOUT_MS,
                "totalMs": TOTAL_TIMEOUT_MS,
            },
        )
    except Exception as exc:
        logger.info("BciLider: billing script failed", exc_info=True)
        read = BillingRead()
        read.warnings.append(f"BciLider: billing script failed: {type(exc).__name__}")
        return read
    read = billing_from_result(result)
    logger.info(
        "BciLider: billing read (rate %s, current billing %s, %d statement(s))",
        "yes" if read.exchange_rates else "no",
        "yes" if read.current else "no",
        len(read.statements),
    )
    return read
