#!/usr/bin/env python3
"""Generate Amelia participant PDFs for events on 2026-07-07."""

from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "database_ultimo.csv"
EXPORT_ROOT = ROOT / "exports" / "2026-07-07"
ZIP_PATH = ROOT / "exports" / "eventi_7_luglio_2026_database_ultimo_pdf.zip"
TARGET_DATE = "2026-07-07"
DISPLAY_DATE = "7 Luglio 2026"
EVENT_TITLE = "Menu Deleddiani"
LOCATION = "Nuoro"


def configure_fonts() -> tuple[str, str]:
    regular = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if regular.exists() and bold.exists():
        pdfmetrics.registerFont(TTFont("DejaVuSans", str(regular)))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(bold)))
        return "DejaVuSans", "DejaVuSans-Bold"
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = configure_fonts()


def clean(value: str | None) -> str:
    if value is None or value == "NULL":
        return ""
    return value.strip()


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "evento"


def parse_sections(path: Path) -> dict[str, list[dict[str, str]]]:
    csv.field_size_limit(sys.maxsize)

    def is_header(row: list[str]) -> bool:
        return bool(row) and row[0] == "id" and any(
            marker in row
            for marker in (
                "appointmentId",
                "customerBookingId",
                "parentId",
                "periodStart",
                "firstName",
                "eventTicketId",
                "customFieldId",
            )
        )

    def table_name(header: list[str]) -> str | None:
        h = set(header)
        if {"appointmentId", "customerId", "persons", "qrCodes"} <= h:
            return "customer_bookings"
        if {"customerBookingId", "eventPeriodId"} <= h:
            return "booking_periods"
        if {"parentId", "name", "customLocation", "maxCapacity"} <= h:
            return "events"
        if {"eventId", "periodStart", "periodEnd"} <= h:
            return "periods"
        if {"firstName", "lastName", "email", "phone", "type"} <= h:
            return "users"
        if {"customerBookingId", "eventTicketId", "persons", "price"} <= h:
            return "booking_tickets"
        return None

    sections: dict[str, list[dict[str, str]]] = {}
    current_header: list[str] | None = None
    current_name: str | None = None
    current_rows: list[dict[str, str]] = []

    def flush() -> None:
        nonlocal current_rows
        if current_name:
            sections[current_name] = current_rows
        current_rows = []

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if is_header(row):
                flush()
                current_header = row
                current_name = table_name(row)
                current_rows = []
                continue
            if current_header and current_name and len(row) >= len(current_header):
                current_rows.append(dict(zip(current_header, row)))
        flush()

    required = ["customer_bookings", "booking_periods", "events", "periods", "users"]
    missing = [name for name in required if name not in sections]
    if missing:
        raise RuntimeError(f"Missing required sections in {path.name}: {', '.join(missing)}")
    return sections


def parse_ticket_codes(qr_codes: str) -> list[str]:
    qr_codes = clean(qr_codes)
    if not qr_codes:
        return []
    try:
        parsed = json.loads(qr_codes)
    except json.JSONDecodeError:
        return []
    ticket_codes = [
        clean(item.get("ticketManualCode"))
        for item in parsed
        if isinstance(item, dict) and item.get("type") == "ticket"
    ]
    ticket_codes = [code for code in ticket_codes if code]
    if ticket_codes:
        return ticket_codes
    return [
        clean(item.get("ticketManualCode"))
        for item in parsed
        if isinstance(item, dict) and clean(item.get("ticketManualCode"))
    ]


def parse_info(info: str) -> dict[str, str]:
    info = clean(info)
    if not info:
        return {}
    try:
        data = json.loads(info)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def fmt_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def para(value: object, style: ParagraphStyle) -> Paragraph:
    text = str(value or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("\n", "<br/>")
    return Paragraph(text, style)


@dataclass
class Participant:
    booking_id: str
    customer_id: str
    first_name: str
    last_name: str
    phone: str
    email: str
    persons: int
    ticket_codes: list[str]
    event_id: str
    event_name: str
    period_start: str

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part).strip()

    @property
    def duplicate_key(self) -> str:
        if self.email:
            return "email:" + self.email.lower()
        if self.phone:
            return "phone:" + re.sub(r"\D+", "", self.phone)
        return "name:" + self.full_name.lower()


def build_dataset() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], list[Participant]]:
    sections = parse_sections(DATABASE)
    events = {row["id"]: row for row in sections["events"]}
    periods = {row["id"]: row for row in sections["periods"]}
    users = {row["id"]: row for row in sections["users"]}

    july_periods = {
        row["id"]: row
        for row in sections["periods"]
        if clean(row.get("periodStart")).startswith(TARGET_DATE)
    }
    links_by_booking: dict[str, list[str]] = defaultdict(list)
    for row in sections["booking_periods"]:
        links_by_booking[row["customerBookingId"]].append(row["eventPeriodId"])

    participants: list[Participant] = []
    for booking in sections["customer_bookings"]:
        if clean(booking.get("status")) != "approved":
            continue
        period_ids = [pid for pid in links_by_booking.get(booking["id"], []) if pid in july_periods]
        if not period_ids:
            continue
        user = users.get(booking.get("customerId", ""), {})
        info = parse_info(booking.get("info", ""))
        first_name = clean(user.get("firstName")) or clean(info.get("firstName"))
        last_name = clean(user.get("lastName")) or clean(info.get("lastName"))
        phone = clean(user.get("phone")) or clean(info.get("phone"))
        email = clean(user.get("email"))
        try:
            persons = int(clean(booking.get("persons")) or "0")
        except ValueError:
            persons = 0
        ticket_codes = parse_ticket_codes(booking.get("qrCodes", ""))
        for period_id in period_ids:
            period = july_periods[period_id]
            event_id = period["eventId"]
            participants.append(
                Participant(
                    booking_id=booking["id"],
                    customer_id=booking.get("customerId", ""),
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    email=email,
                    persons=persons,
                    ticket_codes=ticket_codes,
                    event_id=event_id,
                    event_name=clean(events.get(event_id, {}).get("name")),
                    period_start=clean(period.get("periodStart")),
                )
            )
    return events, july_periods, participants


def base_styles() -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=sample["Title"],
            fontName=FONT_BOLD,
            fontSize=14,
            leading=18,
            alignment=TA_CENTER,
            spaceAfter=5,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=sample["Normal"],
            fontName=FONT,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
        ),
        "event": ParagraphStyle(
            "event",
            parent=sample["Heading2"],
            fontName=FONT_BOLD,
            fontSize=13,
            leading=16,
            alignment=TA_CENTER,
            spaceBefore=6,
            spaceAfter=8,
        ),
        "normal": ParagraphStyle(
            "normal",
            parent=sample["Normal"],
            fontName=FONT,
            fontSize=7.2,
            leading=9,
            alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "small",
            parent=sample["Normal"],
            fontName=FONT,
            fontSize=6.2,
            leading=7.5,
            alignment=TA_LEFT,
        ),
        "header": ParagraphStyle(
            "header",
            parent=sample["Normal"],
            fontName=FONT_BOLD,
            fontSize=7.2,
            leading=9,
            alignment=TA_LEFT,
            textColor=colors.white,
        ),
    }


def add_page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(FONT, 7)
    canvas.drawRightString(landscape(A4)[0] - 1.0 * cm, 0.55 * cm, f"Pagina {doc.page}")
    canvas.restoreState()


def table_style() -> TableStyle:
    return TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTNAME", (0, 1), (-1, -1), FONT),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bdbdbd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )


def write_event_pdf(event: dict[str, str], rows: list[Participant], path: Path) -> None:
    styles = base_styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        rightMargin=0.8 * cm,
        leftMargin=0.8 * cm,
        topMargin=0.7 * cm,
        bottomMargin=0.9 * cm,
    )
    rows = sorted(rows, key=lambda item: (item.last_name.lower(), item.first_name.lower(), int(item.booking_id)))
    total_tickets = sum(row.persons for row in rows)
    first_period = rows[0].period_start if rows else TARGET_DATE

    story = [
        Paragraph("Dalla Terra alla Tavola - Confesercenti", styles["title"]),
        Paragraph(f"Evento: {EVENT_TITLE}", styles["subtitle"]),
        Paragraph(f"Localita: {LOCATION} &nbsp;&nbsp; Data evento: {DISPLAY_DATE}", styles["subtitle"]),
        Paragraph(clean(event.get("name")), styles["event"]),
        Paragraph(
            f"Partecipanti: {len(rows)} prenotazioni approvate - Ticket totali: {total_tickets} - Orario: {fmt_date(first_period)}",
            styles["subtitle"],
        ),
        Spacer(1, 0.25 * cm),
    ]

    data = [
        [
            para("Partecipante", styles["header"]),
            para("Telefono", styles["header"]),
            para("Mail", styles["header"]),
            para("N. ticket", styles["header"]),
            para("Codici ticket", styles["header"]),
            para("Booking ID", styles["header"]),
        ]
    ]
    for item in rows:
        data.append(
            [
                para(item.full_name, styles["normal"]),
                para(item.phone, styles["normal"]),
                para(item.email, styles["normal"]),
                para(str(item.persons), styles["normal"]),
                para(", ".join(item.ticket_codes), styles["small"]),
                para(item.booking_id, styles["normal"]),
            ]
        )
    table = Table(data, repeatRows=1, colWidths=[5.4 * cm, 3.4 * cm, 6.2 * cm, 1.6 * cm, 9.2 * cm, 2.0 * cm])
    table.setStyle(table_style())
    story.append(table)
    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)


def write_duplicates_pdf(groups: list[list[Participant]], path: Path) -> None:
    styles = base_styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=landscape(A4),
        rightMargin=0.8 * cm,
        leftMargin=0.8 * cm,
        topMargin=0.7 * cm,
        bottomMargin=0.9 * cm,
    )

    story = [
        Paragraph("Controllo prenotazioni doppie - 7 Luglio 2026", styles["title"]),
        Paragraph(
            "Sono elencate le persone con piu di una prenotazione approvata sugli eventi del 7 luglio.",
            styles["subtitle"],
        ),
        Spacer(1, 0.35 * cm),
    ]

    if not groups:
        story.append(Paragraph("Nessuna prenotazione doppia trovata.", styles["event"]))
    else:
        data = [
            [
                para("Partecipante", styles["header"]),
                para("Telefono", styles["header"]),
                para("Mail", styles["header"]),
                para("Prenotazioni", styles["header"]),
                para("Ticket tot.", styles["header"]),
                para("Eventi / booking", styles["header"]),
                para("Codici ticket", styles["header"]),
            ]
        ]
        for group in groups:
            sample = group[0]
            event_lines = []
            codes = []
            for item in sorted(group, key=lambda row: (row.event_name.lower(), int(row.booking_id))):
                event_lines.append(f"{item.event_name} (booking {item.booking_id}, ticket {item.persons})")
                codes.extend(item.ticket_codes)
            data.append(
                [
                    para(sample.full_name, styles["normal"]),
                    para(sample.phone, styles["normal"]),
                    para(sample.email, styles["normal"]),
                    para(str(len(group)), styles["normal"]),
                    para(str(sum(item.persons for item in group)), styles["normal"]),
                    para("<br/>".join(event_lines), styles["small"]),
                    para(", ".join(codes), styles["small"]),
                ]
            )
        table = Table(
            data,
            repeatRows=1,
            colWidths=[4.0 * cm, 3.0 * cm, 4.8 * cm, 2.0 * cm, 1.7 * cm, 8.0 * cm, 5.2 * cm],
        )
        table.setStyle(table_style())
        story.append(table)

    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)


def write_duplicates_csv(groups: list[list[Participant]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "partecipante",
                "telefono",
                "email",
                "prenotazioni",
                "ticket_totali",
                "eventi_booking",
                "codici_ticket",
            ]
        )
        for group in groups:
            sample = group[0]
            events = [
                f"{item.event_name} (booking {item.booking_id}, ticket {item.persons})"
                for item in sorted(group, key=lambda row: (row.event_name.lower(), int(row.booking_id)))
            ]
            codes = [code for item in group for code in item.ticket_codes]
            writer.writerow(
                [
                    sample.full_name,
                    sample.phone,
                    sample.email,
                    len(group),
                    sum(item.persons for item in group),
                    " | ".join(events),
                    ", ".join(codes),
                ]
            )


def main() -> None:
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    for old_pdf in EXPORT_ROOT.glob("*.pdf"):
        old_pdf.unlink()
    for old_csv in EXPORT_ROOT.glob("*.csv"):
        old_csv.unlink()

    events, _periods, participants = build_dataset()
    participants_by_event: dict[str, list[Participant]] = defaultdict(list)
    for participant in participants:
        participants_by_event[participant.event_id].append(participant)

    generated: list[Path] = []
    for event_id in sorted(participants_by_event, key=lambda value: int(value)):
        event = events[event_id]
        pdf_path = EXPORT_ROOT / f"{slugify(clean(event.get('name')))}.pdf"
        write_event_pdf(event, participants_by_event[event_id], pdf_path)
        generated.append(pdf_path)

    groups_by_person: dict[str, list[Participant]] = defaultdict(list)
    for participant in participants:
        groups_by_person[participant.duplicate_key].append(participant)
    duplicate_groups = [
        group
        for group in groups_by_person.values()
        if len(group) > 1
    ]
    duplicate_groups.sort(
        key=lambda group: (
            -len(group),
            group[0].last_name.lower(),
            group[0].first_name.lower(),
            group[0].email.lower(),
        )
    )

    duplicates_pdf = EXPORT_ROOT / "controllo_prenotazioni_doppie_7_luglio_2026.pdf"
    write_duplicates_pdf(duplicate_groups, duplicates_pdf)
    generated.append(duplicates_pdf)

    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(generated):
            archive.write(path, arcname=path.relative_to(ROOT / "exports"))

    total_tickets = sum(item.persons for item in participants)
    print(f"PDF eventi generati: {len(participants_by_event)}")
    print(f"Prenotazioni approvate 7 luglio: {len(participants)}")
    print(f"Ticket totali 7 luglio: {total_tickets}")
    print(f"Gruppi duplicati: {len(duplicate_groups)}")
    print(f"ZIP: {ZIP_PATH}")


if __name__ == "__main__":
    main()
