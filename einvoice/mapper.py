"""Map a plain invoice dict (the kind an ERP export or a simple JSON/CSV row naturally has) to a UBL 2.1 Invoice,
conformant to EN 16931 and Peppol BIS Billing 3.0.

Design choices (see docs/DESIGN.md):
- Reject rather than guess: a missing required field (seller VAT id, a line's net amount) raises MappingError.
  An invoice is a legal/financial document; a silently wrong one is worse than a rejected one.
- All money math is done in Decimal, quantized to the currency's minor unit, exactly as EN 16931's own rounding
  rules require (BR-CO-17 and friends allow only a small tolerance, not open-ended rounding).
- The function computes line nets, VAT breakdown totals and the payable amount itself from the line items given,
  rather than trusting totals the caller supplies, so arithmetic errors are caught before validation does it anyway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape

PEPPOL_CUSTOMIZATION = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PEPPOL_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
ISO4217 = {"EUR", "USD", "GBP", "SEK", "NOK", "DKK", "PLN", "CHF", "AUD", "CAD", "JPY", "AED", "SGD"}
# EAS (Electronic Address Scheme) codes Peppol commonly uses for party endpoint ids; not exhaustive.
EAS_CODES = {"0088": "GLN", "0096": "DK:CVR", "0106": "NL:KVK", "0184": "DK:P", "0192": "NO:ORGNR", "0198": "DK:SE",
             "0204": "DE:LID", "0208": "BE:EN", "9906": "IT:VAT", "9907": "IT:CF", "9915": "ATU", "9930": "UK:VAT",
             "9944": "NL:VAT", "9957": "FR:VAT"}
VAT_CATEGORIES = {"S": "Standard rate", "Z": "Zero rated", "E": "Exempt", "AE": "Reverse charge", "K": "IC supply",
                   "G": "Export", "O": "Not subject", "L": "Canary Islands", "M": "Ceuta/Melilla"}
UNIT_CODES = {"EA", "C62", "KGM", "LTR", "MTR", "HUR", "DAY", "MON", "XPP", "KWH", "TNE", "MTK", "MTQ", "PCE", "SET"}


class MappingError(ValueError):
    """The invoice cannot be mapped faithfully. Fix the input; do not resend unchanged."""


MAX_PRICE_PLACES = 4      # unit price and quantity may carry up to 4 decimals (e.g. 0.0125 EUR per kWh)
MAX_QUANTITY_PLACES = 4
MAX_MAGNITUDE_DIGITS = 9    # Decimal.adjusted(): values must be below 1e10


def _minor_places(currency: str) -> int:
    """Decimal places of the currency's minor unit (0 for JPY, otherwise 2)."""
    return 0 if currency == "JPY" else 2


def _quantum(currency: str) -> Decimal:
    return Decimal(1).scaleb(-_minor_places(currency))


def _exact(x, where: str, *, min_places: int, max_places: int) -> Decimal:
    """Parse a number WITHOUT silently rounding it.

    More decimals than `max_places` is rejected (reject rather than guess); fewer than `min_places` is padded with
    zeros so the XML is formatted consistently ("100" -> "100.00"). Only line nets, VAT amounts and totals are ever
    rounded, and that happens in the arithmetic below, never on the values the caller supplied.
    """
    try:
        d = Decimal(str(x))
    except Exception as exc:
        raise MappingError(f"{where}: '{x}' is not a valid number") from exc
    if not d.is_finite():
        raise MappingError(f"{where}: '{x}' is not a valid number")
    if d.adjusted() > MAX_MAGNITUDE_DIGITS:
        # Keeps every product and sum inside Decimal's default 28-digit precision, where nothing is rounded silently.
        raise MappingError(f"{where}: {str(x)[:30]!r} is too large (must be below 10 billion)")
    used = max(0, -d.normalize().as_tuple().exponent)      # trailing zeros ("0.0125000") do not count
    if used > max_places:
        raise MappingError(f"{where}: {x!r} has {used} decimal places but at most {max_places} are allowed; "
                           f"refusing to round it silently")
    if -d.as_tuple().exponent < min_places:
        d = d.quantize(Decimal(1).scaleb(-min_places))
    return d


def _require(d: dict, key: str, where: str) -> object:
    v = d.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise MappingError(f"{where}: '{key}' is required")
    return v


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date(value: str, where: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise MappingError(f"{where}: date must be YYYY-MM-DD, got {value!r}")
    import datetime
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise MappingError(f"{where}: {exc}") from exc
    return value


def _xml(s) -> str:
    return escape(str(s))


@dataclass
class MapConfig:
    currency_allowlist: set[str] = field(default_factory=lambda: set(ISO4217))
    require_buyer_reference_or_order: bool = True   # BT-10/BT-13: Peppol BIS3 requires one of these


def party_block(tag: str, party: dict, where: str, cfg: MapConfig) -> str:
    name = _require(party, "name", where)
    endpoint = party.get("endpoint_id")
    scheme = party.get("endpoint_scheme")
    parts = [f"<cac:{tag}Party><cbc:EndpointID"]
    if endpoint:
        if not scheme:
            raise MappingError(f"{where}: endpoint_id given without endpoint_scheme (an EAS code, e.g. '9944')")
        parts[-1] += f' schemeID="{_xml(scheme)}">{_xml(endpoint)}</cbc:EndpointID>'
    else:
        parts = [f"<cac:{tag}Party>"]
    vat = party.get("vat_id")
    legal_reg = party.get("legal_registration_id")
    address = party.get("address") or {}
    country = _require(address, "country", f"{where}.address")
    if not re.fullmatch(r"[A-Z]{2}", str(country)):
        raise MappingError(f"{where}.address.country must be a 2-letter ISO code, got {country!r}")
    addr_lines = []
    if address.get("line1"):
        addr_lines.append(f"<cbc:StreetName>{_xml(address['line1'])}</cbc:StreetName>")
    if address.get("city"):
        addr_lines.append(f"<cbc:CityName>{_xml(address['city'])}</cbc:CityName>")
    if address.get("postal_code"):
        addr_lines.append(f"<cbc:PostalZone>{_xml(address['postal_code'])}</cbc:PostalZone>")
    addr_lines.append(f"<cac:Country><cbc:IdentificationCode>{_xml(country)}</cbc:IdentificationCode></cac:Country>")
    legal_block = f"<cbc:RegistrationName>{_xml(name)}</cbc:RegistrationName>"
    if legal_reg:
        legal_block += f'<cbc:CompanyID schemeID="{_xml(legal_reg.get("scheme", "0002"))}">{_xml(legal_reg["id"])}</cbc:CompanyID>'
    body = "".join(parts) + (
        f"<cac:PartyName><cbc:Name>{_xml(name)}</cbc:Name></cac:PartyName>"
        f"<cac:PostalAddress>{''.join(addr_lines)}</cac:PostalAddress>"
        + (f'<cac:PartyTaxScheme><cbc:CompanyID>{_xml(vat)}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>' if vat else "")
        + f"<cac:PartyLegalEntity>{legal_block}</cac:PartyLegalEntity></cac:{tag}Party>")
    return body


def contact_block(tag: str, party: dict) -> str:
    contact = party.get("contact")
    if not contact:
        return ""
    bits = []
    if contact.get("name"):
        bits.append(f"<cbc:Name>{_xml(contact['name'])}</cbc:Name>")
    if contact.get("phone"):
        bits.append(f"<cbc:Telephone>{_xml(contact['phone'])}</cbc:Telephone>")
    if contact.get("email"):
        bits.append(f"<cbc:ElectronicMail>{_xml(contact['email'])}</cbc:ElectronicMail>")
    return f"<cac:Contact>{''.join(bits)}</cac:Contact>" if bits else ""


@dataclass
class LineResult:
    xml: str
    net: Decimal
    vat_category: str
    vat_rate: Decimal
    vat_amount: Decimal


def map_line(i: int, line: dict, currency: str, cfg: MapConfig) -> LineResult:
    where = f"lines[{i}]"
    qty = _exact(_require(line, "quantity", where), f"{where}.quantity", min_places=2, max_places=MAX_QUANTITY_PLACES)
    if qty <= 0:
        raise MappingError(f"{where}: quantity must be positive")
    # Price and quantity keep their own precision; only the line net below is rounded to the currency's minor unit.
    unit_price = _exact(_require(line, "unit_price", where), f"{where}.unit_price",
                        min_places=_minor_places(currency), max_places=MAX_PRICE_PLACES)
    unit_code = line.get("unit_code", "EA")
    if unit_code not in UNIT_CODES:
        raise MappingError(f"{where}: unit_code {unit_code!r} is not in the supported UN/ECE recommendation 20 subset {sorted(UNIT_CODES)}")
    name = _require(line, "name", where)
    vat_category = str(_require(line, "vat_category", where)).upper()
    if vat_category not in VAT_CATEGORIES:
        raise MappingError(f"{where}: vat_category must be one of {sorted(VAT_CATEGORIES)}, got {vat_category!r}")
    vat_rate = (_exact(line.get("vat_rate", 0), f"{where}.vat_rate", min_places=2, max_places=2)
                if vat_category not in ("E", "O", "G") else Decimal("0.00"))
    if vat_category in ("S",) and vat_rate <= 0:
        raise MappingError(f"{where}: vat_category S (standard rate) needs a positive vat_rate")
    quantum = _quantum(currency)
    net = (unit_price * qty).quantize(quantum, rounding=ROUND_HALF_UP)
    vat_amount = (net * vat_rate / Decimal("100")).quantize(quantum, rounding=ROUND_HALF_UP)
    xml = (
        f"<cac:InvoiceLine><cbc:ID>{i}</cbc:ID>"
        f'<cbc:InvoicedQuantity unitCode="{_xml(unit_code)}">{qty}</cbc:InvoicedQuantity>'
        f"<cbc:LineExtensionAmount currencyID=\"{currency}\">{net}</cbc:LineExtensionAmount>"
        f"<cac:Item><cbc:Name>{_xml(name)}</cbc:Name>"
        f'<cac:ClassifiedTaxCategory><cbc:ID>{vat_category}</cbc:ID><cbc:Percent>{vat_rate}</cbc:Percent>'
        f"<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>"
        f'<cac:Price><cbc:PriceAmount currencyID="{currency}">{unit_price}</cbc:PriceAmount></cac:Price>'
        f"</cac:InvoiceLine>")
    return LineResult(xml, net, vat_category, vat_rate, vat_amount)


def convert(invoice: dict, cfg: MapConfig | None = None) -> str:
    cfg = cfg or MapConfig()
    inv_id = _require(invoice, "invoice_number", "invoice")
    issue_date = _date(_require(invoice, "issue_date", "invoice"), "invoice")
    due_date = _date(invoice["due_date"], "invoice") if invoice.get("due_date") else None
    currency = str(_require(invoice, "currency", "invoice")).upper()
    if currency not in cfg.currency_allowlist:
        raise MappingError(f"currency {currency!r} is not in the allowlist {sorted(cfg.currency_allowlist)}")
    doc_type = {"invoice": "380", "credit_note": "381"}.get(invoice.get("type", "invoice"))
    if doc_type is None:
        raise MappingError("invoice.type must be 'invoice' or 'credit_note'")
    seller = _require(invoice, "seller", "invoice")
    buyer = _require(invoice, "buyer", "invoice")
    buyer_ref = invoice.get("buyer_reference")
    order_ref = invoice.get("order_reference")
    if cfg.require_buyer_reference_or_order and not buyer_ref and not order_ref:
        raise MappingError("Peppol BIS3 requires buyer_reference or order_reference (BT-10 / BT-13)")

    lines_in = invoice.get("lines")
    if not lines_in:
        raise MappingError("invoice must have at least one line")
    results = [map_line(i + 1, ln, currency, cfg) for i, ln in enumerate(lines_in)]
    places = _quantum(currency)
    minor = _minor_places(currency)

    line_total = sum((r.net for r in results), Decimal("0")).quantize(places)

    # Document-level charges/allowances must be parsed BEFORE the VAT breakdown: each one is taxed at its own
    # category/rate and its amount has to be folded into that category's taxable amount (BR-S-08 and siblings),
    # not just added to the grand total.
    allowances_xml = ""
    charge_total = Decimal("0")
    allowance_total = Decimal("0")
    doc_adjust_by_cat: dict[tuple[str, Decimal], Decimal] = {}
    for i, ch in enumerate(invoice.get("charges", []), 1):
        amt = _exact(_require(ch, "amount", f"charges[{i}]"), f"charges[{i}].amount", min_places=minor, max_places=minor)
        if amt <= 0:
            raise MappingError(f"charges[{i}]: amount must be positive")
        is_charge = bool(ch.get("is_charge", True))
        reason, reason_code = ch.get("reason"), ch.get("reason_code")
        if not reason and not reason_code:
            raise MappingError(f"charges[{i}]: needs a 'reason' (free text) or 'reason_code' (UNCL5189/7161)")
        cat = str(ch.get("vat_category", "S")).upper()
        if cat not in VAT_CATEGORIES:
            raise MappingError(f"charges[{i}]: vat_category must be one of {sorted(VAT_CATEGORIES)}")
        rate = _exact(ch.get("vat_rate", 0), f"charges[{i}].vat_rate", min_places=2, max_places=2)
        key = (cat, rate)
        doc_adjust_by_cat[key] = doc_adjust_by_cat.get(key, Decimal("0")) + (amt if is_charge else -amt)
        charge_total, allowance_total = (charge_total + amt, allowance_total) if is_charge else (charge_total, allowance_total + amt)
        reason_xml = (f"<cbc:AllowanceChargeReason>{_xml(reason)}</cbc:AllowanceChargeReason>" if reason else "") + \
                     (f"<cbc:AllowanceChargeReasonCode>{_xml(reason_code)}</cbc:AllowanceChargeReasonCode>" if reason_code else "")
        allowances_xml += (
            f'<cac:AllowanceCharge><cbc:ChargeIndicator>{"true" if is_charge else "false"}</cbc:ChargeIndicator>'
            f"{reason_xml}"
            f'<cbc:Amount currencyID="{currency}">{amt}</cbc:Amount>'
            f"<cac:TaxCategory><cbc:ID>{cat}</cbc:ID><cbc:Percent>{rate}</cbc:Percent>"
            f"<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:AllowanceCharge>")

    by_cat: dict[tuple[str, Decimal], list[LineResult]] = {}
    for r in results:
        by_cat.setdefault((r.vat_category, r.vat_rate), []).append(r)
    for key in doc_adjust_by_cat:                 # a charge/allowance can introduce a category no line used
        by_cat.setdefault(key, [])
    vat_total = Decimal("0")
    breakdown_xml = []
    default_reasons = {"Z": "Zero rated goods", "E": "Exempt from VAT", "AE": "Reverse charge",
                        "K": "Intra-Community supply", "G": "Export outside the EU", "O": "Not subject to VAT",
                        "L": "Canary Islands general indirect tax", "M": "Tax for production, services and importation in Ceuta and Melilla"}
    for (cat, rate), items in sorted(by_cat.items()):
        cat_net = (sum((i.net for i in items), Decimal("0")) + doc_adjust_by_cat.get((cat, rate), Decimal("0"))).quantize(places)
        cat_vat = (cat_net * rate / Decimal("100")).quantize(places, rounding=ROUND_HALF_UP)
        vat_total += cat_vat
        exempt_reason = ""
        if cat not in ("S", "Z"):   # EN 16931: Standard and Zero rated must NOT carry an exemption reason; the others must
            reason = invoice.get("vat_exemption_reason") or default_reasons.get(cat, "Not subject to VAT")
            exempt_reason = f"<cbc:TaxExemptionReason>{_xml(reason)}</cbc:TaxExemptionReason>"
        breakdown_xml.append(
            f'<cac:TaxSubtotal><cbc:TaxableAmount currencyID="{currency}">{cat_net}</cbc:TaxableAmount>'
            f'<cbc:TaxAmount currencyID="{currency}">{cat_vat}</cbc:TaxAmount>'
            f"<cac:TaxCategory><cbc:ID>{cat}</cbc:ID><cbc:Percent>{rate}</cbc:Percent>{exempt_reason}"
            f"<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>")
    vat_total = vat_total.quantize(places)

    tax_exclusive = (line_total + charge_total - allowance_total).quantize(places)
    tax_inclusive = (tax_exclusive + vat_total).quantize(places)
    prepaid = _exact(invoice.get("prepaid_amount", 0), "invoice.prepaid_amount", min_places=minor, max_places=minor)
    payable = (tax_inclusive - prepaid).quantize(places)

    root = "CreditNote" if doc_type == "381" else "Invoice"
    quantity_tag = "CreditedQuantity" if doc_type == "381" else "InvoicedQuantity"
    if doc_type == "381":
        for r in results:
            r.xml = r.xml.replace("InvoicedQuantity", quantity_tag).replace("<cac:InvoiceLine>", "<cac:CreditNoteLine>").replace("</cac:InvoiceLine>", "</cac:CreditNoteLine>")

    due_xml = f"<cbc:DueDate>{due_date}</cbc:DueDate>" if due_date else ""
    buyer_ref_xml = f"<cbc:BuyerReference>{_xml(buyer_ref)}</cbc:BuyerReference>" if buyer_ref else ""
    order_xml = f"<cac:OrderReference><cbc:ID>{_xml(order_ref)}</cbc:ID></cac:OrderReference>" if order_ref else ""
    note_xml = f"<cbc:Note>{_xml(invoice['note'])}</cbc:Note>" if invoice.get("note") else ""
    payment_means = ""
    pm = invoice.get("payment_means")
    if pm:
        iban = pm.get("iban")
        payment_means = (
            f'<cac:PaymentMeans><cbc:PaymentMeansCode>{_xml(pm.get("code", "58"))}</cbc:PaymentMeansCode>'
            + (f"<cbc:PaymentID>{_xml(pm['reference'])}</cbc:PaymentID>" if pm.get("reference") else "")
            + (f'<cac:PayeeFinancialAccount><cbc:ID>{_xml(iban)}</cbc:ID></cac:PayeeFinancialAccount>' if iban else "")
            + "</cac:PaymentMeans>")
    payment_terms = f"<cac:PaymentTerms><cbc:Note>{_xml(invoice['payment_terms'])}</cbc:Note></cac:PaymentTerms>" if invoice.get("payment_terms") else ""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>{PEPPOL_CUSTOMIZATION}</cbc:CustomizationID>
  <cbc:ProfileID>{PEPPOL_PROFILE}</cbc:ProfileID>
  <cbc:ID>{_xml(inv_id)}</cbc:ID>
  <cbc:IssueDate>{issue_date}</cbc:IssueDate>
  {due_xml}
  <cbc:{"CreditNoteTypeCode" if doc_type == "381" else "InvoiceTypeCode"}>{doc_type}</cbc:{"CreditNoteTypeCode" if doc_type == "381" else "InvoiceTypeCode"}>
  {note_xml}
  <cbc:DocumentCurrencyCode>{currency}</cbc:DocumentCurrencyCode>
  {buyer_ref_xml}
  {order_xml}
  <cac:AccountingSupplierParty><cac:Party>{party_block_inner(seller, "invoice.seller", cfg)}{contact_block("Accounting Supplier", seller)}</cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>{party_block_inner(buyer, "invoice.buyer", cfg)}{contact_block("Accounting Customer", buyer)}</cac:Party></cac:AccountingCustomerParty>
  {payment_means}
  {payment_terms}
  {allowances_xml}
  <cac:TaxTotal><cbc:TaxAmount currencyID="{currency}">{vat_total}</cbc:TaxAmount>{"".join(breakdown_xml)}</cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{currency}">{line_total}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{currency}">{tax_exclusive}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{currency}">{tax_inclusive}</cbc:TaxInclusiveAmount>
    {f'<cbc:AllowanceTotalAmount currencyID="{currency}">{allowance_total.quantize(places)}</cbc:AllowanceTotalAmount>' if allowance_total else ""}
    {f'<cbc:ChargeTotalAmount currencyID="{currency}">{charge_total.quantize(places)}</cbc:ChargeTotalAmount>' if charge_total else ""}
    {f'<cbc:PrepaidAmount currencyID="{currency}">{prepaid}</cbc:PrepaidAmount>' if prepaid else ""}
    <cbc:PayableAmount currencyID="{currency}">{payable}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {"".join(r.xml for r in results)}
</{root}>
"""
    return xml


def party_block_inner(party: dict, where: str, cfg: MapConfig) -> str:
    """Same content as party_block but without the outer <cac:XParty> wrapper (caller already wraps in <cac:Party>)."""
    full = party_block("X", party, where, cfg)
    return full[len("<cac:XParty>"):-len("</cac:XParty>")]
