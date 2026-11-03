import hmac
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent.parent
DATABASE = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "loans.sqlite3"))
EXCEL_LEDGER = Path(os.environ.get("EXCEL_LEDGER_PATH", ROOT / "data" / "loan_ledger.xlsx"))
FORMS_DIRECTORY = Path(os.environ.get("FORMS_DIRECTORY", ROOT / "data" / "forms"))
CLIENT_LEDGER_DIRECTORY = Path(
    os.environ.get("CLIENT_LEDGER_DIRECTORY", Path.home() / "MASH" / "LEDGERS")
)
PAYBILL = os.environ.get("MPESA_PAYBILL", "")
CALLBACK_TOKEN = os.environ.get("MPESA_CALLBACK_TOKEN", "")
STAFF_USERNAME = os.environ.get("STAFF_USERNAME", "admin")
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "admin123")
WEBHOOK_URL = os.environ.get("NOTIFICATION_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("NOTIFICATION_WEBHOOK_TOKEN", "")
STAFF_LOCATION_MAP = {
    "staff1": ("NJORO", "PIAVE NJORO"),
    "staff2": ("ELBURGON & MOLO", "MAILI TATU"),
    "staff3": ("EGERTON", "EGERTON  2", "KIHINGO & STOO"),
}
LOCATION_ALIASES = {
    "ELBURGON": "ELBURGON & MOLO",
    "MOLO": "ELBURGON & MOLO",
    "KIHINGO": "KIHINGO & STOO",
    "STOO": "KIHINGO & STOO",
}
DASHBOARD_LOCATIONS = (
    "NJORO",
    "PIAVE NJORO",
    "ELBURGON & MOLO",
    "MAILI TATU",
    "EGERTON",
    "EGERTON 2",
    "KIHINGO & STOO",
    "NJOKERIO",
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-development-key-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

if os.environ.get("APP_ENV") == "production":
    if app.secret_key == "local-development-key-change-me" or STAFF_PASSWORD == "admin123":
        raise RuntimeError("Set FLASK_SECRET_KEY and STAFF_PASSWORD before production startup.")
    if not PAYBILL or not CALLBACK_TOKEN:
        raise RuntimeError("Set MPESA_PAYBILL and MPESA_CALLBACK_TOKEN before production startup.")


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    national_id TEXT NOT NULL UNIQUE,
    business_type TEXT NOT NULL,
    business_location TEXT NOT NULL,
    residence_location TEXT NOT NULL,
    guarantor_name TEXT NOT NULL,
    guarantor_phone TEXT NOT NULL DEFAULT '',
    guarantor_national_id TEXT NOT NULL,
    guarantor_business_type TEXT NOT NULL,
    guarantor_business_location TEXT NOT NULL,
    guarantor_residence_location TEXT NOT NULL,
    form_filename TEXT NOT NULL DEFAULT '',
    form_original_name TEXT NOT NULL DEFAULT '',
    assigned_staff_id INTEGER REFERENCES staff_users(id),
    account_status TEXT NOT NULL DEFAULT 'active' CHECK(account_status IN ('active', 'paused')),
    account_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    loan_number TEXT NOT NULL DEFAULT '',
    principal_cents INTEGER NOT NULL CHECK(principal_cents > 0),
    interest_rate REAL NOT NULL CHECK(interest_rate >= 0),
    interest_cents INTEGER NOT NULL CHECK(interest_cents >= 0),
    total_due_cents INTEGER NOT NULL CHECK(total_due_cents > 0),
    term_days INTEGER NOT NULL CHECK(term_days BETWEEN 25 AND 30),
    issued_on TEXT NOT NULL,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'settled')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    loan_id INTEGER NOT NULL REFERENCES loans(id),
    transaction_id TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    phone TEXT NOT NULL DEFAULT '',
    paid_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    loan_id INTEGER REFERENCES loans(id),
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'failed')),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(loan_id, kind)
);
CREATE TABLE IF NOT EXISTS staff_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'staff')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff_locations (
    staff_id INTEGER NOT NULL REFERENCES staff_users(id),
    location TEXT NOT NULL,
    PRIMARY KEY (staff_id, location),
    UNIQUE (location)
);
CREATE TABLE IF NOT EXISTS ledger_report_scans (
    report_date TEXT NOT NULL,
    source_path TEXT NOT NULL,
    modified_ns INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (report_date, source_path)
);
CREATE TABLE IF NOT EXISTS ledger_report_payments (
    report_date TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    location TEXT NOT NULL,
    client_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    balance_cents INTEGER,
    PRIMARY KEY (report_date, source_path, source_row)
);
CREATE TABLE IF NOT EXISTS renewal_requests (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    source_loan_id INTEGER NOT NULL REFERENCES loans(id),
    requested_by INTEGER NOT NULL REFERENCES staff_users(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'denied', 'issued')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER REFERENCES staff_users(id)
);
CREATE TABLE IF NOT EXISTS ledger_edit_requests (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff_users(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'granted', 'denied', 'revoked')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by INTEGER REFERENCES staff_users(id)
);
"""


def normalize_location(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def location_for_business_address(value):
    normalized = normalize_location(value)
    if normalized in {normalize_location(key) for key in LOCATION_ALIASES}:
        return LOCATION_ALIASES[next(
            key for key in LOCATION_ALIASES
            if normalize_location(key) == normalized
        )]
    candidates = sorted(
        (location for locations in STAFF_LOCATION_MAP.values() for location in locations),
        key=len,
        reverse=True,
    )
    for location in candidates:
        location_normalized = normalize_location(location)
        if normalized == location_normalized or location_normalized in normalized:
            return location
    if "njoro" in normalized:
        return "NJORO"
    if "elburgon" in normalized or "molo" in normalized:
        return "ELBURGON & MOLO"
    if "kihingo" in normalized or "stoo" in normalized:
        return "KIHINGO & STOO"
    if "njokerio" in normalized:
        return "NJOKERIO"
    return None


def staff_for_business_location(db, value):
    location = location_for_business_address(value)
    if location is None:
        return None
    for row in db.execute("SELECT location, staff_id FROM staff_locations").fetchall():
        if normalize_location(row["location"]) == normalize_location(location):
            return row["staff_id"]
    return None


def connect_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def initialize_database():
    with connect_db() as db:
        db.executescript(SCHEMA)
        client_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(clients)").fetchall()
        }
        if "guarantor_phone" not in client_columns:
            db.execute("ALTER TABLE clients ADD COLUMN guarantor_phone TEXT NOT NULL DEFAULT ''")
        if "form_filename" not in client_columns:
            db.execute("ALTER TABLE clients ADD COLUMN form_filename TEXT NOT NULL DEFAULT ''")
        if "form_original_name" not in client_columns:
            db.execute("ALTER TABLE clients ADD COLUMN form_original_name TEXT NOT NULL DEFAULT ''")
        if "assigned_staff_id" not in client_columns:
            db.execute("ALTER TABLE clients ADD COLUMN assigned_staff_id INTEGER REFERENCES staff_users(id)")
        loan_columns = {row["name"] for row in db.execute("PRAGMA table_info(loans)").fetchall()}
        if "loan_number" not in loan_columns:
            db.execute("ALTER TABLE loans ADD COLUMN loan_number TEXT NOT NULL DEFAULT ''")
        db.execute(
            "UPDATE loans SET loan_number = 'LN-' || printf('%06d', id) WHERE loan_number = ''"
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS loans_loan_number ON loans(loan_number)")
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS active_renewal_request_per_loan
               ON renewal_requests(source_loan_id)
               WHERE status IN ('pending', 'approved', 'issued')"""
        )
        db.execute(
            """INSERT OR IGNORE INTO staff_users
               (username, full_name, password_hash, role, created_at)
               VALUES (?, 'Administrator', ?, 'admin', ?)""",
            (
                STAFF_USERNAME,
                generate_password_hash(STAFF_PASSWORD),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        for username, locations in STAFF_LOCATION_MAP.items():
            staff_user = db.execute(
                "SELECT id FROM staff_users WHERE username = ? COLLATE NOCASE AND role = 'staff'",
                (username,),
            ).fetchone()
            if staff_user is None:
                continue
            for location in locations:
                db.execute(
                    "INSERT OR IGNORE INTO staff_locations (staff_id, location) VALUES (?, ?)",
                    (staff_user["id"], location),
                )
        for client in db.execute(
            "SELECT id, business_location FROM clients WHERE assigned_staff_id IS NULL"
        ).fetchall():
            location = location_for_business_address(client["business_location"])
            if location is None:
                continue
            owner = next(
                (
                    username for username, locations in STAFF_LOCATION_MAP.items()
                    if location in locations
                ),
                None,
            )
            if owner:
                db.execute(
                    """UPDATE clients SET assigned_staff_id = (
                         SELECT id FROM staff_users
                         WHERE username = ? COLLATE NOCASE AND role = 'staff'
                       ) WHERE id = ?""",
                    (owner, client["id"]),
                )


initialize_database()


def error(message, status=400):
    return jsonify({"error": message}), status


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return error("Staff login required.", 401)
        user = get_db().execute(
            "SELECT id, username, full_name, role, active FROM staff_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None or not user["active"]:
            session.clear()
            return error("Staff login required.", 401)
        g.staff_user = user
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @staff_required
    def wrapped(*args, **kwargs):
        if g.staff_user["role"] != "admin":
            return error("Administrator access required.", 403)
        return view(*args, **kwargs)

    return wrapped


def money_to_cents(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid monetary amount.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(amount * 100)


def format_money(cents):
    return f"{cents / 100:.2f}"


def client_row(row):
    return dict(row)


def loan_row(db, loan):
    data = dict(loan)
    paid = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE loan_id = ?",
        (loan["id"],),
    ).fetchone()[0]
    principal_paid = allocated_principal_cents(
        loan["principal_cents"], loan["total_due_cents"], paid
    )
    data.update(
        principal=format_money(loan["principal_cents"]),
        interest=format_money(loan["interest_cents"]),
        total_due=format_money(loan["total_due_cents"]),
        paid=format_money(paid),
        balance=format_money(max(0, loan["total_due_cents"] - paid)),
        principal_paid=format_money(principal_paid),
        interest_earned=format_money(max(0, paid - principal_paid)),
        principal_balance=format_money(max(0, loan["principal_cents"] - principal_paid)),
        daily_installment=format_money(
            (Decimal(loan["total_due_cents"]) / loan["term_days"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        ),
    )
    return data


def allocated_principal_cents(principal_cents, total_due_cents, cumulative_paid_cents):
    paid_toward_total = min(max(cumulative_paid_cents, 0), total_due_cents)
    return min(
        principal_cents,
        (paid_toward_total * principal_cents + total_due_cents // 2) // total_due_cents,
    )


def deliver_notification(notification_id):
    db = connect_db()
    try:
        row = db.execute(
            """SELECT n.*, c.phone FROM notifications n
               JOIN clients c ON c.id = n.client_id WHERE n.id = ?""",
            (notification_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Notification {notification_id} was not found.")

        if not WEBHOOK_URL:
            db.execute(
                "UPDATE notifications SET status = 'pending', error = ? WHERE id = ?",
                ("No notification webhook is configured.", notification_id),
            )
            db.commit()
            return

        headers = {"Content-Type": "application/json"}
        if WEBHOOK_TOKEN:
            headers["Authorization"] = f"Bearer {WEBHOOK_TOKEN}"
        try:
            response = requests.post(
                WEBHOOK_URL,
                json={"to": row["phone"], "message": row["message"]},
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            db.execute(
                "UPDATE notifications SET status = 'failed', error = ? WHERE id = ?",
                (str(exc)[:500], notification_id),
            )
            db.commit()
            return

        db.execute(
            "UPDATE notifications SET status = 'sent', error = '', sent_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), notification_id),
        )
        db.commit()
    finally:
        db.close()


def queue_notification(db, client_id, loan_id, kind, message):
    cursor = db.execute(
        """INSERT OR IGNORE INTO notifications (client_id, loan_id, kind, message, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (client_id, loan_id, kind, message, datetime.now(timezone.utc).isoformat()),
    )
    if cursor.rowcount:
        notification_id = cursor.lastrowid
        db.commit()
        deliver_notification(notification_id)
        return notification_id
    return None


def required_text(payload, key, label):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required.")
    return value.strip()


def parse_payment_time(value):
    if not value:
        return datetime.now().replace(microsecond=0)
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Payment timestamp must be ISO format or M-Pesa YYYYMMDDHHMMSS.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def append_spreadsheet_row(sheet, values):
    sheet.append(values)
    for cell, value in zip(sheet[sheet.max_row], values):
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            cell.data_type = "s"


def export_excel_ledger():
    db = connect_db()
    workbook = Workbook()
    clients_sheet = workbook.active
    clients_sheet.title = "Client Accounts"
    payments_sheet = workbook.create_sheet("Payment History")
    clients_sheet.append(
        [
            "Client Name", "Client Phone", "Client National ID", "Business Type",
            "Business Location", "Residence Location", "Guarantor Name", "Guarantor Phone",
            "Guarantor National ID", "Guarantor Business Type", "Guarantor Business Location",
            "Guarantor Residence Location", "Word Form", "Account Status", "Loan ID", "Loan Number",
            "Principal (KES)", "Interest (KES)", "Original Total Issued (KES)",
            "Total Amount Paid (KES)", "Remaining Balance (KES)", "Term (Days)",
            "Issue Date", "Due Date", "Loan Status",
        ]
    )
    payments_sheet.append(
        [
            "Transaction ID", "Client Name", "Client National ID", "Client Phone",
            "Loan ID", "Principal (KES)", "Interest (KES)", "Original Total Issued (KES)",
            "Amount Paid (KES)", "Principal Repaid (KES)", "Interest Earned (KES)",
            "Cumulative Amount Paid (KES)", "Remaining Principal (KES)", "Total Balance (KES)",
            "Day", "Date", "Month", "Year", "Time",
        ]
    )
    try:
        clients = db.execute(
            """SELECT c.*, l.id AS loan_id, l.loan_number, l.principal_cents, l.interest_cents,
                      l.total_due_cents, l.term_days, l.issued_on, l.due_on, l.status AS loan_status,
                      COALESCE(p.total_paid, 0) AS total_paid
               FROM clients c
               LEFT JOIN loans l ON l.client_id = c.id
               LEFT JOIN (
                   SELECT loan_id, SUM(amount_cents) AS total_paid
                   FROM payments GROUP BY loan_id
               ) p ON p.loan_id = l.id
               ORDER BY c.id, l.id"""
        ).fetchall()
        for row in clients:
            has_loan = row["loan_id"] is not None
            values = [
                row["name"], row["phone"], row["national_id"], row["business_type"],
                row["business_location"], row["residence_location"], row["guarantor_name"],
                row["guarantor_phone"], row["guarantor_national_id"],
                row["guarantor_business_type"], row["guarantor_business_location"],
                row["guarantor_residence_location"], row["form_original_name"],
                row["account_status"], row["loan_id"], row["loan_number"],
                row["principal_cents"] / 100 if has_loan else None,
                row["interest_cents"] / 100 if has_loan else None,
                row["total_due_cents"] / 100 if has_loan else None,
                row["total_paid"] / 100 if has_loan else None,
                max(0, row["total_due_cents"] - row["total_paid"]) / 100 if has_loan else None,
                row["term_days"], row["issued_on"], row["due_on"], row["loan_status"],
            ]
            append_spreadsheet_row(clients_sheet, values)
            if row["form_filename"]:
                form_cell = clients_sheet.cell(row=clients_sheet.max_row, column=13)
                form_cell.hyperlink = f"forms/{row['form_filename']}"
                form_cell.style = "Hyperlink"

        payment_rows = db.execute(
            """SELECT p.*, l.id AS loan_id, l.principal_cents, l.interest_cents,
                      l.total_due_cents, c.name AS client_name, c.national_id, c.phone
               FROM payments p
               JOIN loans l ON l.id = p.loan_id
               JOIN clients c ON c.id = l.client_id
               ORDER BY p.id"""
        ).fetchall()
        cumulative = {}
        for row in payment_rows:
            paid_at = parse_payment_time(row["paid_at"])
            previous_paid = cumulative.get(row["loan_id"], 0)
            cumulative[row["loan_id"]] = previous_paid + row["amount_cents"]
            principal_paid = (
                allocated_principal_cents(
                    row["principal_cents"], row["total_due_cents"], cumulative[row["loan_id"]]
                )
                - allocated_principal_cents(
                    row["principal_cents"], row["total_due_cents"], previous_paid
                )
            )
            values = [
                row["transaction_id"], row["client_name"], row["national_id"], row["phone"],
                row["loan_id"], row["principal_cents"] / 100, row["interest_cents"] / 100,
                row["total_due_cents"] / 100, row["amount_cents"] / 100,
                principal_paid / 100, (row["amount_cents"] - principal_paid) / 100,
                cumulative[row["loan_id"]] / 100,
                max(0, row["principal_cents"] - allocated_principal_cents(
                    row["principal_cents"], row["total_due_cents"], cumulative[row["loan_id"]]
                )) / 100,
                max(0, row["total_due_cents"] - cumulative[row["loan_id"]]) / 100,
                paid_at.strftime("%A"), paid_at.date(), paid_at.strftime("%B"),
                paid_at.year, paid_at.time(),
            ]
            append_spreadsheet_row(payments_sheet, values)

        for sheet in workbook.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="ED8240")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            sheet.row_dimensions[1].height = 32
            for column in sheet.columns:
                letter = column[0].column_letter
                width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 34)
                sheet.column_dimensions[letter].width = width
        for row in payments_sheet.iter_rows(min_row=2):
            row[15].number_format = "yyyy-mm-dd"
            row[18].number_format = "hh:mm:ss"
            for cell in row[5:15]:
                cell.number_format = '#,##0.00'
        for row in clients_sheet.iter_rows(min_row=2):
            for cell in row[16:21]:
                cell.number_format = '#,##0.00'

        EXCEL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".xlsx", dir=EXCEL_LEDGER.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        workbook.save(temporary_path)
        os.replace(temporary_path, EXCEL_LEDGER)
    except Exception:
        if "temporary_path" in locals():
            temporary_path.unlink(missing_ok=True)
        raise
    finally:
        db.close()


def client_ledger_files():
    if not CLIENT_LEDGER_DIRECTORY.is_dir():
        raise FileNotFoundError(f"Client ledger directory not found: {CLIENT_LEDGER_DIRECTORY}")
    root = CLIENT_LEDGER_DIRECTORY.resolve()
    files = []
    for path in CLIENT_LEDGER_DIRECTORY.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() != ".xlsx"
            or path.name.startswith(("~$", "."))
        ):
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        relative_path = path.relative_to(CLIENT_LEDGER_DIRECTORY).as_posix()
        ledger_id = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]
        location = (
            ""
            if path.parent == CLIENT_LEDGER_DIRECTORY
            else path.parent.relative_to(CLIENT_LEDGER_DIRECTORY).as_posix()
        )
        files.append(
            {
                "id": ledger_id,
                "name": path.stem.strip(),
                "location": location,
                "relative_path": relative_path,
            }
        )
    return sorted(files, key=lambda item: (item["name"].casefold(), item["location"].casefold()))


def visible_client_ledger_files():
    files = client_ledger_files()
    if g.staff_user["role"] == "admin":
        return files

    assigned_locations = {
        normalize_location(row["location"])
        for row in get_db().execute(
            "SELECT location FROM staff_locations WHERE staff_id = ?",
            (g.staff_user["id"],),
        ).fetchall()
    }
    return [
        item
        for item in files
        if normalize_location(item["location"].split("/", 1)[0]) in assigned_locations
    ]


def staff_can_edit_ledgers(staff_id):
    row = get_db().execute(
        """SELECT status FROM ledger_edit_requests
           WHERE staff_id = ? ORDER BY id DESC LIMIT 1""",
        (staff_id,),
    ).fetchone()
    return row is not None and row["status"] == "granted"


def parse_ledger_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    for date_format in (
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d-%m-%y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    return None


def ledger_number_cents(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def read_ledger_payments(path, location, report_date):
    workbook = load_workbook(path, data_only=True, read_only=True)
    payments_found = []
    client_name = path.stem.strip()
    balance_columns = set()
    try:
        for worksheet in workbook.worksheets:
            if not hasattr(worksheet, "iter_rows"):
                continue
            seen_data_rows = False
            for row_number, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                values = list(row)
                row_date = parse_ledger_date(values[0]) if values else None
                if not seen_data_rows and row_date is None:
                    if row_number <= 8 and values:
                        first_cell = str(values[0] or "").strip().rstrip(".").casefold()
                        if first_cell.startswith("name") and len(values) > 1 and values[1]:
                            client_name = str(values[1]).strip().lstrip(":").strip() or client_name
                        for column_index, value in enumerate(values):
                            if isinstance(value, str) and "balance" in value.casefold():
                                balance_columns.add(column_index)
                elif row_date is not None:
                    seen_data_rows = True
                if not values or row_date != report_date:
                    continue
                text_values = [value.casefold() for value in values if isinstance(value, str)]
                if any("loan granted" in value or "loaned" in value for value in text_values):
                    continue
                numeric_values = [
                    amount
                    for amount in (ledger_number_cents(value) for value in values[1:])
                    if amount is not None
                ]
                if len(numeric_values) < 2:
                    continue
                if len(numeric_values) < 2:
                    continue
                last_balance = numeric_values[-1]
                previous_value = numeric_values[-2]
                paired_balances = len(balance_columns) >= 2 or (
                    len(numeric_values) >= 3
                    and (
                        (previous_value == 0 and last_balance == 0 and len(numeric_values) >= 5)
                        or (
                            previous_value > 0
                            and last_balance >= previous_value
                            and last_balance <= previous_value * 2.5
                        )
                    )
                )
                amount_index = -3 if paired_balances else -2
                if len(numeric_values) < abs(amount_index):
                    continue
                amount_cents = numeric_values[amount_index]
                balance_cents = last_balance
                if amount_cents <= 0:
                    continue
                payments_found.append(
                    {
                        "source_row": row_number,
                        "location": location or "Unassigned",
                        "client_name": client_name,
                        "amount_cents": amount_cents,
                        "balance_cents": balance_cents,
                    }
                )
    finally:
        workbook.close()
    return payments_found


def sync_ledger_report(db, report_date):
    files = client_ledger_files()
    current_paths = set()
    for item in files:
        relative_path = item["relative_path"]
        current_paths.add(relative_path)
        path = CLIENT_LEDGER_DIRECTORY / relative_path
        try:
            file_stat = path.stat()
        except OSError as exc:
            app.logger.warning("Could not stat client ledger %s: %s", relative_path, exc)
            continue
        cached = db.execute(
            """SELECT modified_ns, file_size FROM ledger_report_scans
               WHERE report_date = ? AND source_path = ?""",
            (report_date.isoformat(), relative_path),
        ).fetchone()
        if (
            cached
            and cached["modified_ns"] == file_stat.st_mtime_ns
            and cached["file_size"] == file_stat.st_size
        ):
            continue

        db.execute(
            """DELETE FROM ledger_report_payments
               WHERE report_date = ? AND source_path = ?""",
            (report_date.isoformat(), relative_path),
        )
        scan_error = ""
        try:
            records = read_ledger_payments(path, item["location"], report_date)
            db.executemany(
                """INSERT INTO ledger_report_payments
                   (report_date, source_path, source_row, location, client_name,
                    amount_cents, balance_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        report_date.isoformat(),
                        relative_path,
                        record["source_row"],
                        record["location"],
                        record["client_name"],
                        record["amount_cents"],
                        record["balance_cents"],
                    )
                    for record in records
                ],
            )
        except (OSError, ValueError, InvalidFileException, zipfile.BadZipFile) as exc:
            scan_error = str(exc)[:500]
            app.logger.warning("Could not read client ledger %s: %s", relative_path, exc)
        db.execute(
            """INSERT INTO ledger_report_scans
               (report_date, source_path, modified_ns, file_size, error)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(report_date, source_path) DO UPDATE SET
                 modified_ns = excluded.modified_ns,
                 file_size = excluded.file_size,
                 error = excluded.error""",
            (
                report_date.isoformat(),
                relative_path,
                file_stat.st_mtime_ns,
                file_stat.st_size,
                scan_error,
            ),
        )

    stale_scans = db.execute(
        "SELECT source_path FROM ledger_report_scans WHERE report_date = ?",
        (report_date.isoformat(),),
    ).fetchall()
    for stale in stale_scans:
        if stale["source_path"] not in current_paths:
            db.execute(
                "DELETE FROM ledger_report_payments WHERE report_date = ? AND source_path = ?",
                (report_date.isoformat(), stale["source_path"]),
            )
            db.execute(
                "DELETE FROM ledger_report_scans WHERE report_date = ? AND source_path = ?",
                (report_date.isoformat(), stale["source_path"]),
            )
    db.commit()


def ledger_cell_value(value):
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


@app.get("/api/client-ledgers")
@staff_required
def list_client_ledgers():
    try:
        files = visible_client_ledger_files()
    except OSError as exc:
        app.logger.exception("Client ledger directory could not be read")
        return error(f"Could not read the client ledger directory: {exc}", 500)
    return jsonify(
        [
            {key: value for key, value in item.items() if key != "relative_path"}
            for item in files
        ]
    )


@app.get("/api/ledger-edit-requests")
@staff_required
def list_ledger_edit_requests():
    if g.staff_user["role"] == "admin":
        rows = get_db().execute(
            """SELECT r.*, u.full_name AS staff_name
               FROM ledger_edit_requests r
               JOIN staff_users u ON u.id = r.staff_id
               WHERE r.id = (
                   SELECT MAX(latest.id) FROM ledger_edit_requests latest
                   WHERE latest.staff_id = r.staff_id
               )
               ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.created_at DESC"""
        ).fetchall()
    else:
        rows = get_db().execute(
            """SELECT r.*, u.full_name AS staff_name
               FROM ledger_edit_requests r
               JOIN staff_users u ON u.id = r.staff_id
               WHERE r.staff_id = ?
               ORDER BY r.id DESC LIMIT 1""",
            (g.staff_user["id"],),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/ledger-edit-requests")
@staff_required
def request_ledger_edit_access():
    if g.staff_user["role"] != "staff":
        return error("Only staff can request ledger edit access.", 403)
    db = get_db()
    latest = db.execute(
        """SELECT status FROM ledger_edit_requests
           WHERE staff_id = ? ORDER BY id DESC LIMIT 1""",
        (g.staff_user["id"],),
    ).fetchone()
    if latest and latest["status"] in {"pending", "granted"}:
        return error(
            "Your ledger edit access request is already pending."
            if latest["status"] == "pending"
            else "You already have permission to edit ledgers.",
            409,
        )
    cursor = db.execute(
        """INSERT INTO ledger_edit_requests (staff_id, status, created_at)
           VALUES (?, 'pending', ?)""",
        (g.staff_user["id"], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return jsonify({"id": cursor.lastrowid, "status": "pending"}), 201


@app.patch("/api/ledger-edit-requests/<int:request_id>")
@admin_required
def review_ledger_edit_request(request_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("Expected a ledger edit decision.")
    decision = payload.get("decision")
    if decision not in {"grant", "deny", "revoke"}:
        return error("Decision must be grant, deny, or revoke.")

    db = get_db()
    ledger_request = db.execute(
        """SELECT r.*, u.active AS staff_active FROM ledger_edit_requests r
           JOIN staff_users u ON u.id = r.staff_id
           WHERE r.id = ? AND r.id = (
               SELECT MAX(latest.id) FROM ledger_edit_requests latest
               WHERE latest.staff_id = r.staff_id
           )""",
        (request_id,),
    ).fetchone()
    if ledger_request is None:
        return error("Ledger edit request not found.", 404)
    if decision == "grant" and not ledger_request["staff_active"]:
        return error("Cannot grant ledger edit access to an inactive staff account.", 409)
    expected_status = "granted" if decision == "revoke" else "pending"
    if ledger_request["status"] != expected_status:
        return error("This ledger edit request has already been reviewed.", 409)

    new_status = {"grant": "granted", "deny": "denied", "revoke": "revoked"}[decision]
    db.execute(
        """UPDATE ledger_edit_requests
           SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?""",
        (
            new_status,
            datetime.now(timezone.utc).isoformat(),
            g.staff_user["id"],
            request_id,
        ),
    )
    db.commit()
    return jsonify({"id": request_id, "status": new_status})


@app.get("/api/client-ledgers/<ledger_id>")
@staff_required
def read_client_ledger(ledger_id):
    try:
        ledger = next(
            (item for item in visible_client_ledger_files() if item["id"] == ledger_id),
            None,
        )
        if ledger is None:
            return error("Client ledger not found.", 404)

        ledger_path = CLIENT_LEDGER_DIRECTORY / ledger["relative_path"]
        revision = str(ledger_path.stat().st_mtime_ns)
        workbook = load_workbook(ledger_path, data_only=True, read_only=True)
        formula_workbook = None
        try:
            formula_workbook = load_workbook(ledger_path, data_only=False, read_only=True)
            sheets = []
            for worksheet, formula_worksheet in zip(workbook.worksheets, formula_workbook.worksheets):
                sheet_rows = []
                row_numbers = []
                formula_cells = [
                    [cell.row, cell.column]
                    for row in formula_worksheet.iter_rows()
                    for cell in row
                    if cell.data_type == "f"
                ]
                header_row_index = None
                metadata = []
                for row_index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                    values = [ledger_cell_value(value) for value in row]
                    while values and values[-1] is None:
                        values.pop()
                    if not values:
                        continue
                    if row_index <= 3:
                        for index in range(0, len(values) - 1, 2):
                            label, value = values[index], values[index + 1]
                            if label is not None and value is not None:
                                if isinstance(value, str):
                                    value = value.strip().lstrip(";").strip()
                                metadata.append({"label": str(label).strip().rstrip(";"), "value": value})
                        for value in values:
                            if isinstance(value, str) and value.upper().startswith("ID;"):
                                metadata.append({"label": "ID", "value": value.split(";", 1)[1].strip()})
                    if header_row_index is None and {
                        str(value).strip().upper() for value in values if value is not None
                    }.issuperset({"DATE", "DETAILS"}):
                        header_row_index = row_index
                        headers = values
                        continue
                    if header_row_index is not None and row_index > header_row_index:
                        sheet_rows.append(values)
                        row_numbers.append(row_index)
                if header_row_index is not None:
                    column_count = max(
                        len(headers),
                        max((len(row) for row in sheet_rows), default=0),
                    )
                    headers = [
                        str(headers[index]).strip()
                        if index < len(headers) and headers[index] is not None
                        else f"Column {index + 1}"
                        for index in range(column_count)
                    ]
                    normalized_rows = [
                        row + [None] * (column_count - len(row)) for row in sheet_rows
                    ]
                    sheets.append(
                        {
                            "name": worksheet.title,
                            "metadata": metadata,
                            "headers": headers,
                            "rows": normalized_rows,
                            "row_numbers": row_numbers,
                            "formula_cells": formula_cells,
                        }
                    )
                elif sheet_rows:
                    sheets.append(
                        {
                            "name": worksheet.title,
                            "metadata": metadata,
                            "headers": [],
                            "rows": sheet_rows,
                            "row_numbers": row_numbers,
                            "formula_cells": formula_cells,
                        }
                    )
            client_national_id = next(
                (
                    str(item["value"]).strip()
                    for sheet in sheets
                    for item in sheet["metadata"]
                    if item["label"].strip().upper() in {"ID", "NATIONAL ID"}
                ),
                None,
            )
            app_payments = []
            app_loans = []
            if client_national_id:
                db = get_db()
                app_client = db.execute(
                    "SELECT id FROM clients WHERE national_id = ?", (client_national_id,)
                ).fetchone()
                if app_client:
                    loan_rows = db.execute(
                        """SELECT l.*, c.name AS client_name, c.national_id, c.phone,
                                  c.account_status
                           FROM loans l JOIN clients c ON c.id = l.client_id
                           WHERE c.id = ? ORDER BY l.id DESC""",
                        (app_client["id"],),
                    ).fetchall()
                    app_loans = [loan_row(db, loan) for loan in loan_rows]
                    payment_rows = db.execute(
                        """SELECT p.*, c.name AS client_name, c.national_id,
                                  l.principal_cents, l.total_due_cents,
                                  SUM(p.amount_cents) OVER (
                                    PARTITION BY p.loan_id ORDER BY p.id
                                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                                  ) AS cumulative_paid_cents
                           FROM payments p
                           JOIN loans l ON l.id = p.loan_id
                           JOIN clients c ON c.id = l.client_id
                           WHERE c.id = ?
                           ORDER BY p.id""",
                        (app_client["id"],),
                    ).fetchall()
                    app_payments = [payment_row(row) for row in payment_rows]
            return jsonify(
                {
                    "name": ledger["name"],
                    "location": ledger["location"],
                    "sheets": sheets,
                    "app_loans": app_loans,
                    "app_payments": app_payments,
                    "editable": (
                        g.staff_user["role"] == "admin"
                        or staff_can_edit_ledgers(g.staff_user["id"])
                    ),
                    "revision": revision,
                }
            )
        finally:
            workbook.close()
            if formula_workbook is not None:
                formula_workbook.close()
    except (OSError, ValueError, InvalidFileException, zipfile.BadZipFile) as exc:
        app.logger.exception("Client ledger could not be opened")
        return error(f"Could not open this client ledger: {exc}", 500)


def parse_ledger_edit_value(value, current_value):
    if value == "":
        return None
    if isinstance(current_value, bool):
        if value.casefold() not in {"true", "false"}:
            raise ValueError("Boolean cells accept only true or false.")
        return value.casefold() == "true"
    if isinstance(current_value, int):
        try:
            number = Decimal(value.replace(",", "").strip())
        except InvalidOperation as exc:
            raise ValueError("Enter a valid whole number for this cell.") from exc
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("Enter a valid whole number for this cell.")
        return int(number)
    if isinstance(current_value, float):
        try:
            number = Decimal(value.replace(",", "").strip())
        except InvalidOperation as exc:
            raise ValueError("Enter a valid number for this cell.") from exc
        if not number.is_finite():
            raise ValueError("Enter a valid number for this cell.")
        return float(number)
    if isinstance(current_value, datetime):
        for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), date_format)
            except ValueError:
                continue
        raise ValueError("Enter a date as DD/MM/YYYY or YYYY-MM-DD.")
    if isinstance(current_value, date):
        for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value.strip(), date_format).date()
            except ValueError:
                continue
        raise ValueError("Enter a date as DD/MM/YYYY or YYYY-MM-DD.")
    if isinstance(current_value, time):
        for time_format in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value.strip(), time_format).time()
            except ValueError:
                continue
        raise ValueError("Enter a time as HH:MM or HH:MM:SS.")
    return value


@app.put("/api/client-ledgers/<ledger_id>")
@staff_required
def update_client_ledger(ledger_id):
    if g.staff_user["role"] != "admin" and not staff_can_edit_ledgers(g.staff_user["id"]):
        return error("Administrator approval is required to edit client ledgers.", 403)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("revision"), str):
        return error("A valid workbook revision and cell edits are required.")
    edits = payload.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > 500:
        return error("Provide between 1 and 500 cell edits.")

    try:
        ledger = next(
            (item for item in visible_client_ledger_files() if item["id"] == ledger_id),
            None,
        )
    except OSError as exc:
        app.logger.exception("Client ledger directory could not be read while saving")
        return error(f"Could not read the client ledger directory: {exc}", 500)
    if ledger is None:
        return error("Client ledger not found.", 404)

    ledger_path = CLIENT_LEDGER_DIRECTORY / ledger["relative_path"]
    try:
        if str(ledger_path.stat().st_mtime_ns) != payload["revision"]:
            return error("This workbook has changed. Reopen it before saving your edits.", 409)
    except OSError as exc:
        app.logger.exception("Client ledger could not be checked before saving")
        return error(f"Could not access this client ledger: {exc}", 500)

    workbook = None
    temporary_path = None
    edit_access_db = None
    try:
        workbook = load_workbook(ledger_path, data_only=False)
        seen_cells = set()
        table_bounds = {}
        for edit in edits:
            if not isinstance(edit, dict):
                return error("Each cell edit must include a sheet, row, column, and text value.")
            sheet_name = edit.get("sheet")
            row_number = edit.get("row")
            column_number = edit.get("column")
            value = edit.get("value")
            if (
                not isinstance(sheet_name, str)
                or type(row_number) is not int
                or type(column_number) is not int
                or row_number < 1
                or column_number < 1
                or not isinstance(value, str)
                or len(value) > 2000
            ):
                return error("Each edit must use a valid sheet, cell address, and value of at most 2,000 characters.")
            if sheet_name not in workbook.sheetnames:
                return error("A requested worksheet was not found.")
            cell_key = (sheet_name, row_number, column_number)
            if cell_key in seen_cells:
                return error("A cell cannot be edited more than once in the same save.")
            seen_cells.add(cell_key)

            worksheet = workbook[sheet_name]
            if sheet_name not in table_bounds:
                header_row = None
                column_count = 0
                data_rows = set()
                for index, row in enumerate(worksheet.iter_rows(values_only=True), start=1):
                    if header_row is None:
                        if {"DATE", "DETAILS"}.issubset(
                            {str(item).strip().upper() for item in row if item is not None}
                        ):
                            header_row = index
                            column_count = max(
                                (column for column, item in enumerate(row, start=1) if item is not None),
                                default=0,
                            )
                        continue
                    if any(item is not None for item in row):
                        data_rows.add(index)
                        column_count = max(
                            column_count,
                            max(
                                (column for column, item in enumerate(row, start=1) if item is not None),
                                default=0,
                            ),
                        )
                table_bounds[sheet_name] = (header_row, column_count, data_rows)
            header_row, column_count, data_rows = table_bounds[sheet_name]
            if (
                header_row is None
                or row_number <= header_row
                or row_number not in data_rows
                or column_number > column_count
            ):
                return error("Only cells in the client ledger table can be edited.")
            cell = worksheet.cell(row=row_number, column=column_number)
            if cell.data_type == "f":
                return error("Formula cells cannot be edited from the ledger viewer.")
            try:
                cell.value = parse_ledger_edit_value(value, cell.value)
            except ValueError as exc:
                return error(str(exc))
            if isinstance(cell.value, str) and cell.value.startswith(("=", "+", "-", "@", "\t", "\r")):
                cell.data_type = "s"

        with tempfile.NamedTemporaryFile(
            suffix=".xlsx", dir=ledger_path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        workbook.save(temporary_path)
        workbook.close()
        workbook = None
        if str(ledger_path.stat().st_mtime_ns) != payload["revision"]:
            return error("This workbook changed while your edits were being saved. Reopen it before trying again.", 409)
        edit_access_status = None
        if g.staff_user["role"] == "staff":
            edit_access_db = get_db()
            cursor = edit_access_db.execute(
                """UPDATE ledger_edit_requests
                   SET status = 'revoked', reviewed_at = ?, reviewed_by = NULL
                   WHERE id = (
                       SELECT MAX(id) FROM ledger_edit_requests
                       WHERE staff_id = ? AND status = 'granted'
                   )""",
                (datetime.now(timezone.utc).isoformat(), g.staff_user["id"]),
            )
            if cursor.rowcount != 1:
                app.logger.error(
                    "Ledger save for staff user %s did not consume an edit approval",
                    g.staff_user["id"],
                )
                edit_access_db.rollback()
                return error("Could not revoke ledger edit access after saving. Contact an administrator.", 500)
            edit_access_status = "revoked"
        os.replace(temporary_path, ledger_path)
        temporary_path = None
        if edit_access_db is not None:
            edit_access_db.commit()
        return jsonify(
            {
                "saved": True,
                "revision": str(ledger_path.stat().st_mtime_ns),
                "edit_access_status": edit_access_status,
            }
        )
    except (OSError, InvalidFileException, zipfile.BadZipFile) as exc:
        if edit_access_db is not None:
            edit_access_db.rollback()
        app.logger.exception("Client ledger edits could not be saved")
        return error(f"Could not save this client ledger: {exc}", 500)
    finally:
        if workbook is not None:
            workbook.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_docx(upload):
    if upload is None or not upload.filename:
        return "", ""
    original_name = Path(upload.filename).name
    if Path(original_name).suffix.lower() != ".docx":
        raise ValueError("Registration form must be a Microsoft Word .docx file.")
    content = upload.read()
    if not content:
        raise ValueError("The Word form is empty.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if "[Content_Types].xml" not in archive.namelist() or "word/document.xml" not in archive.namelist():
                raise ValueError("The uploaded file is not a valid .docx Word document.")
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid .docx Word document.") from exc

    FORMS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.docx"
    (FORMS_DIRECTORY / stored_name).write_bytes(content)
    return stored_name, original_name


@app.get("/api/session")
def session_status():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"authenticated": False})
    user = get_db().execute(
        "SELECT id, username, full_name, role, active FROM staff_users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user is None or not user["active"]:
        session.clear()
        return jsonify({"authenticated": False})
    return jsonify(
        {
            "id": user["id"],
            "authenticated": True,
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
        }
    )


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "")
    password = payload.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        return error("Username and password are required.")
    user = get_db().execute(
        "SELECT * FROM staff_users WHERE username = ? COLLATE NOCASE AND active = 1",
        (username.strip(),),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return error("Invalid username or password.", 401)
    session.clear()
    session["user_id"] = user["id"]
    return jsonify(
        {
            "id": user["id"],
            "authenticated": True,
            "username": user["username"],
            "full_name": user["full_name"],
            "role": user["role"],
        }
    )


@app.post("/api/logout")
@staff_required
def logout():
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/staff")
@admin_required
def staff_list():
    rows = get_db().execute(
        """SELECT id, username, full_name, role, active, created_at
           FROM staff_users ORDER BY role, full_name"""
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/staff")
@admin_required
def create_staff():
    payload = request.get_json(silent=True) or {}
    try:
        username = required_text(payload, "username", "Username")
        full_name = required_text(payload, "full_name", "Full name")
        password = required_text(payload, "password", "Password")
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
            return error("Username must be 3–40 characters using letters, numbers, dots, underscores or hyphens.")
        if len(password) < 10:
            return error("Staff passwords must be at least 10 characters.")
        cursor = get_db().execute(
            """INSERT INTO staff_users (username, full_name, password_hash, role, created_at)
               VALUES (?, ?, ?, 'staff', ?)""",
            (
                username,
                full_name,
                generate_password_hash(password),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        get_db().commit()
        return jsonify({"id": cursor.lastrowid}), 201
    except ValueError as exc:
        return error(str(exc))
    except sqlite3.IntegrityError:
        return error("That username is already in use.", 409)


@app.patch("/api/staff/<int:staff_id>")
@admin_required
def update_staff_status(staff_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or type(payload.get("active")) is not bool:
        return error("A valid active status is required.")
    db = get_db()
    staff_user = db.execute(
        "SELECT id, role FROM staff_users WHERE id = ?", (staff_id,)
    ).fetchone()
    if staff_user is None:
        return error("Staff account not found.", 404)
    if staff_user["role"] != "staff":
        return error("Administrator accounts cannot be changed here.", 403)
    db.execute(
        "UPDATE staff_users SET active = ? WHERE id = ?",
        (int(payload["active"]), staff_id),
    )
    db.commit()
    return jsonify({"id": staff_id, "active": payload["active"]})


@app.get("/api/ledger.xlsx")
@staff_required
def download_excel_ledger():
    try:
        export_excel_ledger()
    except (OSError, sqlite3.Error) as exc:
        app.logger.exception("Excel ledger generation failed")
        return error(f"Could not generate the Excel ledger: {exc}", 500)
    return send_file(
        EXCEL_LEDGER,
        as_attachment=True,
        download_name="Nama-Traders-Accounts-and-Payments.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/dashboard")
@staff_required
def dashboard():
    db = get_db()
    scope = request.args.get("scope", "mine" if g.staff_user["role"] == "staff" else "company")
    today = date.today().isoformat()
    if g.staff_user["role"] == "staff":
        my_client_filter = "c.assigned_staff_id = ?"
        params = (g.staff_user["id"],)
        company_filter = "1 = 1"
        company_params = ()
    else:
        my_client_filter = "1 = 1"
        params = ()
        company_filter = "1 = 1"
        company_params = ()
    summary = db.execute(
        f"""SELECT
             (SELECT COUNT(*) FROM clients c WHERE {my_client_filter}) AS my_client_count,
             (SELECT COUNT(*) FROM clients c WHERE {company_filter}) AS company_client_count,
             (SELECT COUNT(*) FROM loans l JOIN clients c ON c.id = l.client_id WHERE l.status = 'active' AND {my_client_filter}) AS my_active_loans,
             (SELECT COUNT(*) FROM loans l JOIN clients c ON c.id = l.client_id WHERE l.status = 'active' AND {company_filter}) AS company_active_loans,
             (SELECT COALESCE(SUM(l.total_due_cents - COALESCE(p.paid, 0)), 0)
              FROM loans l LEFT JOIN
                (SELECT loan_id, SUM(amount_cents) AS paid FROM payments GROUP BY loan_id) p
              ON p.loan_id = l.id JOIN clients c ON c.id = l.client_id
              WHERE l.status = 'active' AND {my_client_filter}) AS my_outstanding_cents,
             (SELECT COALESCE(SUM(l.total_due_cents - COALESCE(p.paid, 0)), 0)
              FROM loans l LEFT JOIN
                (SELECT loan_id, SUM(amount_cents) AS paid FROM payments GROUP BY loan_id) p
              ON p.loan_id = l.id JOIN clients c ON c.id = l.client_id
              WHERE l.status = 'active' AND {company_filter}) AS company_outstanding_cents,
             (SELECT COUNT(*) FROM notifications n JOIN clients c ON c.id = n.client_id WHERE n.status = 'failed' AND {my_client_filter}) AS my_failed_notifications,
             (SELECT COUNT(*) FROM notifications n JOIN clients c ON c.id = n.client_id WHERE n.status = 'failed' AND {company_filter}) AS company_failed_notifications""",
        params * 4 + company_params,
    ).fetchone()
    due_today = db.execute(
        f"""SELECT COUNT(*) FROM loans l
           JOIN clients c ON c.id = l.client_id
           LEFT JOIN (SELECT loan_id, SUM(amount_cents) AS paid FROM payments GROUP BY loan_id) p
             ON p.loan_id = l.id
           WHERE l.status = 'active' AND l.due_on <= ? AND l.total_due_cents > COALESCE(p.paid, 0) AND {my_client_filter if scope == 'mine' else company_filter}""",
        (today,) + (params if scope == 'mine' else company_params),
    ).fetchone()[0]
    my_paid_today = db.execute(
        """SELECT COALESCE(SUM(p.amount_cents), 0)
           FROM payments p JOIN loans l ON l.id = p.loan_id
           JOIN clients c ON c.id = l.client_id
           WHERE date(p.paid_at) = ? AND c.assigned_staff_id = ?""",
        (today, g.staff_user["id"]),
    ).fetchone()[0]
    company_paid_today = db.execute(
        """SELECT COALESCE(SUM(p.amount_cents), 0)
           FROM payments p JOIN loans l ON l.id = p.loan_id
           JOIN clients c ON c.id = l.client_id
           WHERE date(p.paid_at) = ?""",
        (today,),
    ).fetchone()[0]
    if g.staff_user["role"] == "admin":
        visible_locations = list(DASHBOARD_LOCATIONS)
    else:
        visible_locations = [
            row["location"]
            for row in db.execute(
                """SELECT location FROM staff_locations
                   WHERE staff_id = ? ORDER BY location COLLATE NOCASE""",
                (g.staff_user["id"],),
            ).fetchall()
        ]
    assigned_location_keys = {normalize_location(location) for location in visible_locations}
    location_summaries = {
        location: {
            "location": location,
            "client_ids": set(),
            "outstanding": 0,
            "recovered": 0,
            "principal_recovered": 0,
            "interest_recovered": 0,
        }
        for location in DASHBOARD_LOCATIONS
    }
    all_client_ids = set()
    overall_totals = {
        "outstanding": 0,
        "recovered": 0,
        "principal_recovered": 0,
        "interest_recovered": 0,
    }
    location_rows = db.execute(
        """SELECT c.id AS client_id, c.business_location,
                  l.principal_cents, l.total_due_cents,
                  COALESCE(SUM(p.amount_cents), 0) AS paid_cents
           FROM clients c
           LEFT JOIN loans l ON l.client_id = c.id
           LEFT JOIN payments p ON p.loan_id = l.id
           GROUP BY c.id, l.id"""
    ).fetchall()
    for row in location_rows:
        location = location_for_business_address(row["business_location"])
        canonical_location = next(
            (item for item in DASHBOARD_LOCATIONS
             if location is not None
             and normalize_location(item) == normalize_location(location)),
            None,
        )
        if canonical_location is None:
            summary_row = None
        else:
            summary_row = location_summaries[canonical_location]
            summary_row["client_ids"].add(row["client_id"])
        all_client_ids.add(row["client_id"])
        if row["total_due_cents"] is None:
            continue
        principal_cents = row["principal_cents"]
        total_due_cents = row["total_due_cents"]
        paid_cents = min(max(row["paid_cents"], 0), total_due_cents)
        principal_recovered_cents = allocated_principal_cents(
            principal_cents, total_due_cents, paid_cents
        )
        loan_totals = {
            "outstanding": max(0, total_due_cents - row["paid_cents"]),
            "recovered": paid_cents,
            "principal_recovered": principal_recovered_cents,
            "interest_recovered": paid_cents - principal_recovered_cents,
        }
        for field, amount in loan_totals.items():
            overall_totals[field] += amount
            if summary_row is not None:
                summary_row[field] += amount

    location_summaries = list(location_summaries.values())
    for summary_row in location_summaries:
        summary_row["clients"] = len(summary_row.pop("client_ids"))
        summary_row["assigned_to_staff"] = (
            normalize_location(summary_row["location"]) in assigned_location_keys
            if g.staff_user["role"] == "staff"
            else False
        )
        for field in ("outstanding", "recovered", "principal_recovered", "interest_recovered"):
            summary_row[field] = format_money(summary_row[field])
    location_summaries.sort(key=lambda item: not item["assigned_to_staff"])

    overall_location_summary = {
        "location": "All locations",
        "clients": len(all_client_ids),
        **{
            field: format_money(amount)
            for field, amount in overall_totals.items()
        },
    }
    selected_count = summary["my_client_count"] if scope == "mine" and g.staff_user["role"] == "staff" else summary["company_client_count"]
    selected_outstanding = summary["my_outstanding_cents"] if scope == "mine" and g.staff_user["role"] == "staff" else summary["company_outstanding_cents"]
    selected_failed = summary["my_failed_notifications"] if scope == "mine" and g.staff_user["role"] == "staff" else summary["company_failed_notifications"]
    return jsonify(
        {
            "clients": selected_count,
            "active_loans": summary["my_active_loans"] if scope == "mine" and g.staff_user["role"] == "staff" else summary["company_active_loans"],
            "outstanding": format_money(selected_outstanding),
            "failed_notifications": selected_failed,
            "overdue_loans": due_today,
            "my_clients": summary["my_client_count"],
            "company_clients": summary["company_client_count"],
            "my_paid_today": format_money(my_paid_today),
            "company_paid_today": format_money(company_paid_today),
            "scope": scope,
            "locations": visible_locations,
            "location_summaries": location_summaries,
            "overall_location_summary": overall_location_summary,
        }
    )


@app.get("/api/clients")
@staff_required
def clients():
    scope = request.args.get("scope", "mine" if g.staff_user["role"] == "staff" else "company")
    query = """SELECT c.*,
             s.full_name AS assigned_staff_name,
             (SELECT l.id FROM loans l WHERE l.client_id = c.id AND l.status = 'active'
              ORDER BY l.id DESC LIMIT 1) AS active_loan_id,
             active_loan.principal_cents AS active_principal_cents,
             active_loan.interest_cents AS active_interest_cents,
             active_loan.total_due_cents AS active_total_due_cents,
             COALESCE(active_payments.total_paid_cents, 0) AS active_total_paid_cents
           FROM clients c
           LEFT JOIN staff_users s ON s.id = c.assigned_staff_id
           LEFT JOIN loans active_loan ON active_loan.id = (
             SELECT l.id FROM loans l WHERE l.client_id = c.id AND l.status = 'active'
             ORDER BY l.id DESC LIMIT 1
           )
           LEFT JOIN (
             SELECT loan_id, SUM(amount_cents) AS total_paid_cents
             FROM payments GROUP BY loan_id
           ) active_payments ON active_payments.loan_id = active_loan.id"""
    parameters = []
    if g.staff_user["role"] == "staff":
        if scope == "mine":
            query += " WHERE c.assigned_staff_id = ?"
            parameters.append(g.staff_user["id"])
        else:
            query += " WHERE 1 = 1"
    query += " ORDER BY c.created_at DESC"
    rows = get_db().execute(query, parameters).fetchall()
    return jsonify(
        [
            {
                **client_row(row),
                "assigned_staff_name": row["assigned_staff_name"],
                "assigned_staff_id": row["assigned_staff_id"],
                "active_total_balance": format_money(
                    max(0, (row["active_total_due_cents"] or 0) - row["active_total_paid_cents"])
                ),
                "active_principal_balance": format_money(
                    max(
                        0,
                        (row["active_principal_cents"] or 0)
                        - allocated_principal_cents(
                            row["active_principal_cents"] or 0,
                            row["active_total_due_cents"] or 1,
                            row["active_total_paid_cents"],
                        ),
                    )
                ),
                "active_interest_earned": format_money(
                    max(
                        0,
                        row["active_total_paid_cents"]
                        - allocated_principal_cents(
                            row["active_principal_cents"] or 0,
                            row["active_total_due_cents"] or 1,
                            row["active_total_paid_cents"],
                        ),
                    )
                ),
            }
            for row in rows
        ]
    )


@app.post("/api/clients")
@staff_required
def create_client():
    payload = request.form if request.mimetype == "multipart/form-data" else (
        request.get_json(silent=True) or {}
    )
    fields = {
        "name": "Client name",
        "phone": "Phone number",
        "national_id": "National ID",
        "business_type": "Business type",
        "business_location": "Business location",
        "residence_location": "Residence location",
        "guarantor_name": "Guarantor name",
        "guarantor_phone": "Guarantor phone number",
        "guarantor_national_id": "Guarantor national ID",
        "guarantor_business_type": "Guarantor business type",
        "guarantor_business_location": "Guarantor business location",
        "guarantor_residence_location": "Guarantor residence location",
    }
    try:
        values = [required_text(payload, key, label) for key, label in fields.items()]
        phone = values[1]
        if not re.fullmatch(r"\+?[0-9]{9,15}", phone):
            return error("Enter a valid phone number using 9 to 15 digits, optionally prefixed with +.")
        guarantor_phone = values[7]
        if not re.fullmatch(r"\+?[0-9]{9,15}", guarantor_phone):
            return error("Enter a valid guarantor phone number using 9 to 15 digits, optionally prefixed with +.")
        assigned_staff_id = payload.get("assigned_staff_id")
        if g.staff_user["role"] == "staff":
            assigned_staff_id = g.staff_user["id"]
        elif assigned_staff_id == "unassigned":
            assigned_staff_id = None
        elif assigned_staff_id not in (None, "", "0"):
            assigned_staff_id = int(assigned_staff_id)
            staff_member = get_db().execute(
                "SELECT id FROM staff_users WHERE id = ? AND active = 1",
                (assigned_staff_id,),
            ).fetchone()
            if staff_member is None:
                return error("Selected staff member is not active.", 400)
        else:
            assigned_staff_id = staff_for_business_location(
                get_db(), values[4]
            )
        form_file = request.files.get("form_file")
        form_filename, form_original_name = save_docx(form_file)
        db = get_db()
        try:
            cursor = db.execute(
                """INSERT INTO clients (
                     name, phone, national_id, business_type, business_location, residence_location,
                     guarantor_name, guarantor_phone, guarantor_national_id, guarantor_business_type,
                     guarantor_business_location, guarantor_residence_location, form_filename,
                     form_original_name, assigned_staff_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    *values[:7],
                    guarantor_phone,
                    *values[8:],
                    form_filename,
                    form_original_name,
                    assigned_staff_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            if form_filename:
                (FORMS_DIRECTORY / form_filename).unlink(missing_ok=True)
            return error("A client with that national ID already exists.", 409)
        try:
            export_excel_ledger()
        except (OSError, sqlite3.Error) as exc:
            app.logger.exception("Excel ledger update failed after client registration")
            return error(f"Client was saved, but the Excel ledger could not be updated: {exc}", 500)
        return jsonify({"id": cursor.lastrowid}), 201
    except (TypeError, ValueError) as exc:
        return error(str(exc))


@app.get("/api/clients/<int:client_id>/form")
@staff_required
def download_client_form(client_id):
    row = get_db().execute(
        "SELECT form_filename, form_original_name FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if row is None:
        return error("Client not found.", 404)
    if not row["form_filename"]:
        return error("No Word registration form is attached to this client.", 404)
    return send_from_directory(
        FORMS_DIRECTORY,
        row["form_filename"],
        as_attachment=True,
        download_name=row["form_original_name"],
    )


@app.patch("/api/clients/<int:client_id>/account")
@staff_required
def update_account(client_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in {"active", "paused"}:
        return error("Account status must be active or paused.")
    note = payload.get("note", "")
    if not isinstance(note, str):
        return error("Account note must be text.")
    db = get_db()
    cursor = db.execute(
        "UPDATE clients SET account_status = ?, account_note = ? WHERE id = ?",
        (status, note.strip(), client_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        return error("Client not found.", 404)
    return jsonify({"status": status})


@app.get("/api/loans")
@staff_required
def loans():
    db = get_db()
    rows = db.execute(
        """SELECT l.*, c.name AS client_name, c.national_id, c.phone, c.account_status
           FROM loans l JOIN clients c ON c.id = l.client_id ORDER BY l.id DESC"""
    ).fetchall()
    return jsonify([loan_row(db, row) for row in rows])


@app.get("/api/renewal-requests")
@staff_required
def list_renewal_requests():
    query = """SELECT r.*, c.name AS client_name, c.national_id,
                      l.loan_number AS source_loan_number, l.total_due_cents AS source_total_due_cents,
                      u.full_name AS requester_name
               FROM renewal_requests r
               JOIN clients c ON c.id = r.client_id
               JOIN loans l ON l.id = r.source_loan_id
               JOIN staff_users u ON u.id = r.requested_by"""
    parameters = ()
    if g.staff_user["role"] != "admin":
        query += " WHERE r.requested_by = ?"
        parameters = (g.staff_user["id"],)
    query += " ORDER BY r.created_at DESC, r.id DESC"
    rows = get_db().execute(query, parameters).fetchall()
    return jsonify(
        [
            {
                **dict(row),
                "source_total_due": format_money(row["source_total_due_cents"]),
            }
            for row in rows
        ]
    )


@app.post("/api/renewal-requests")
@staff_required
def create_renewal_request():
    if g.staff_user["role"] != "staff":
        return error("Only staff can request a loan renewal.", 403)
    payload = request.get_json(silent=True) or {}
    try:
        source_loan_id = int(payload.get("source_loan_id", 0))
    except (ValueError, TypeError):
        return error("Choose a cleared loan to renew.")

    db = get_db()
    loan = db.execute(
        """SELECT l.*, c.account_status,
                  COALESCE(SUM(p.amount_cents), 0) AS total_paid
           FROM loans l JOIN clients c ON c.id = l.client_id
           LEFT JOIN payments p ON p.loan_id = l.id
           WHERE l.id = ? GROUP BY l.id""",
        (source_loan_id,),
    ).fetchone()
    if loan is None:
        return error("Loan not found.", 404)
    if loan["status"] != "settled" or loan["total_paid"] < loan["total_due_cents"]:
        return error("A renewal can only be requested after the loan is fully paid.", 409)
    if loan["account_status"] != "active":
        return error("This client account is paused.", 409)
    if db.execute(
        "SELECT 1 FROM loans WHERE client_id = ? AND status = 'active'",
        (loan["client_id"],),
    ).fetchone():
        return error("This client already has an active loan.", 409)
    if db.execute(
        """SELECT 1 FROM renewal_requests WHERE source_loan_id = ?
           AND status IN ('pending', 'approved', 'issued')""",
        (source_loan_id,),
    ).fetchone():
        return error("A renewal request already exists for this cleared loan.", 409)

    try:
        cursor = db.execute(
            """INSERT INTO renewal_requests
               (client_id, source_loan_id, requested_by, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                loan["client_id"],
                source_loan_id,
                g.staff_user["id"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except sqlite3.IntegrityError:
        db.rollback()
        return error("A renewal request already exists for this cleared loan.", 409)
    db.commit()
    return jsonify({"id": cursor.lastrowid, "status": "pending"}), 201


@app.patch("/api/renewal-requests/<int:request_id>")
@admin_required
def review_renewal_request(request_id):
    payload = request.get_json(silent=True) or {}
    decision = payload.get("decision")
    if decision not in {"approved", "denied"}:
        return error("Decision must be approved or denied.")
    db = get_db()
    cursor = db.execute(
        """UPDATE renewal_requests
           SET status = ?, reviewed_at = ?, reviewed_by = ?
           WHERE id = ? AND status = 'pending'""",
        (
            decision,
            datetime.now(timezone.utc).isoformat(),
            g.staff_user["id"],
            request_id,
        ),
    )
    if cursor.rowcount == 0:
        return error("Pending renewal request not found.", 404)
    db.commit()
    return jsonify({"id": request_id, "status": decision})


@app.post("/api/loans")
@staff_required
def create_loan():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    if g.staff_user["role"] == "staff":
        try:
            renewal_request_id = int(payload.get("renewal_request_id", 0))
        except (ValueError, TypeError):
            return error("Administrator approval is required before issuing a renewal.", 403)
        renewal = db.execute(
            """SELECT * FROM renewal_requests
               WHERE id = ? AND requested_by = ? AND status = 'approved'""",
            (renewal_request_id, g.staff_user["id"]),
        ).fetchone()
        if renewal is None:
            return error("Administrator approval is required before issuing this renewal.", 403)
        try:
            loan_number = required_text(payload, "loan_number", "Loan number")
            principal_cents = money_to_cents(payload.get("principal"))
            total_due_cents = money_to_cents(payload.get("total_due"))
            issued_on_text = required_text(payload, "issued_on", "Issue date")
            due_on_text = required_text(payload, "due_on", "Due date")
            issued_on = date.fromisoformat(issued_on_text)
            due_on = date.fromisoformat(due_on_text)
            if issued_on.isoformat() != issued_on_text or due_on.isoformat() != due_on_text:
                raise ValueError("Dates must use YYYY-MM-DD format.")
            if len(loan_number) > 40 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/-]{0,39}", loan_number
            ):
                return error("Loan number must be 1–40 letters, numbers, dots, underscores, slashes or hyphens.")
            if total_due_cents < principal_cents:
                return error("Total amount to be paid cannot be less than the amount issued.")
            term_days = (due_on - issued_on).days
            if term_days not in range(25, 31):
                return error("The due date must be 25–30 days after the issue date.")
        except (ValueError, TypeError):
            return error("Enter a valid authorized renewal, amount, loan number, and dates.")

        client = db.execute(
            "SELECT * FROM clients WHERE id = ?", (renewal["client_id"],)
        ).fetchone()
        if client is None or client["account_status"] != "active":
            return error("This client account is not available for a renewal.", 409)
        if db.execute(
            "SELECT 1 FROM loans WHERE client_id = ? AND status = 'active'",
            (renewal["client_id"],),
        ).fetchone():
            return error("This client already has an active loan.", 409)
        interest_cents = total_due_cents - principal_cents
        interest_rate = float(Decimal(interest_cents) * 100 / Decimal(principal_cents))
        try:
            cursor = db.execute(
                """INSERT INTO loans
                   (client_id, loan_number, principal_cents, interest_rate, interest_cents,
                    total_due_cents, term_days, issued_on, due_on, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    client["id"], loan_number, principal_cents, interest_rate, interest_cents,
                    total_due_cents, term_days, issued_on_text, due_on_text,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.execute(
                "UPDATE renewal_requests SET status = 'issued' WHERE id = ?",
                (renewal_request_id,),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return error("That loan number is already in use.", 409)
        try:
            export_excel_ledger()
        except (OSError, sqlite3.Error) as exc:
            app.logger.exception("Excel ledger update failed after renewal issue")
            return error(f"Renewal was saved, but the Excel ledger could not be updated: {exc}", 500)
        return jsonify({"id": cursor.lastrowid, "total_due": format_money(total_due_cents)}), 201

    try:
        client_id = int(payload.get("client_id", 0))
        principal_cents = money_to_cents(payload.get("principal"))
        term_days = int(payload.get("term_days", 0))
        interest_rate = Decimal(str(payload.get("interest_rate", "")))
        if not interest_rate.is_finite() or interest_rate < 0 or interest_rate > 1000:
            return error("Interest rate must be between 0 and 1000 percent.")
        if term_days not in range(25, 31):
            return error("Loan term must be between 25 and 30 days.")
    except (ValueError, InvalidOperation, TypeError):
        return error("Enter a valid client, principal, interest rate, and term.")

    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        return error("Client not found.", 404)
    if client["account_status"] != "active":
        return error("This client account is paused.", 409)
    active = db.execute(
        "SELECT 1 FROM loans WHERE client_id = ? AND status = 'active'", (client_id,)
    ).fetchone()
    if active:
        return error("This client already has an active loan.", 409)

    interest_cents = int(
        (Decimal(principal_cents) * interest_rate / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    total_due_cents = principal_cents + interest_cents
    issued_on = date.today()
    cursor = db.execute(
        """INSERT INTO loans
           (client_id, loan_number, principal_cents, interest_rate, interest_cents, total_due_cents,
            term_days, issued_on, due_on, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            client_id,
            f"TEMP-{uuid.uuid4().hex}",
            principal_cents,
            float(interest_rate),
            interest_cents,
            total_due_cents,
            term_days,
            issued_on.isoformat(),
            (issued_on + timedelta(days=term_days)).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    loan_number = f"LN-{cursor.lastrowid:06d}"
    db.execute("UPDATE loans SET loan_number = ? WHERE id = ?", (loan_number, cursor.lastrowid))
    db.commit()
    try:
        export_excel_ledger()
    except (OSError, sqlite3.Error) as exc:
        app.logger.exception("Excel ledger update failed after loan issue")
        return error(f"Loan was saved, but the Excel ledger could not be updated: {exc}", 500)
    return jsonify({"id": cursor.lastrowid, "total_due": format_money(total_due_cents)}), 201


@app.get("/api/payments")
@staff_required
def payments():
    requested_date = request.args.get("date", "").strip()
    search = request.args.get("search", "").strip()
    if requested_date:
        try:
            datetime.strptime(requested_date, "%Y-%m-%d")
        except ValueError:
            return error("Payment date must use YYYY-MM-DD format.")
    if len(search) > 100:
        return error("Payment search text must be 100 characters or fewer.")

    filters = []
    parameters = []
    if requested_date:
        filters.append("date(payment_history.paid_at) = ?")
        parameters.append(requested_date)
    if search:
        filters.append(
            "(instr(lower(payment_history.client_name), lower(?)) > 0 "
            "OR instr(lower(payment_history.national_id), lower(?)) > 0)"
        )
        parameters.extend((search, search))
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit_clause = "" if filters else "LIMIT 200"
    rows = get_db().execute(
        f"""WITH payment_history AS (
                SELECT p.*, c.name AS client_name, c.national_id, l.principal_cents,
                       l.total_due_cents,
                       SUM(p.amount_cents) OVER (
                         PARTITION BY p.loan_id ORDER BY p.id
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS cumulative_paid_cents
                FROM payments p JOIN loans l ON l.id = p.loan_id
                JOIN clients c ON c.id = l.client_id
            )
            SELECT * FROM payment_history
            {where_clause}
            ORDER BY id DESC {limit_clause}""",
        parameters,
    ).fetchall()
    return jsonify(
        [payment_row(row) for row in rows]
    )


def payment_row(row):
    row_data = dict(row)
    previous_paid = row["cumulative_paid_cents"] - row["amount_cents"]
    principal_paid = (
        allocated_principal_cents(
            row["principal_cents"], row["total_due_cents"], row["cumulative_paid_cents"]
        )
        - allocated_principal_cents(
            row["principal_cents"], row["total_due_cents"], previous_paid
        )
    )
    principal_remaining = max(
        0,
        row["principal_cents"]
        - allocated_principal_cents(
            row["principal_cents"], row["total_due_cents"], row["cumulative_paid_cents"]
        ),
    )
    paid_at = parse_payment_time(row["paid_at"])
    row_data.update(
        amount=format_money(row["amount_cents"]),
        original_total=format_money(row["total_due_cents"]),
        principal_paid=format_money(principal_paid),
        interest_earned=format_money(row["amount_cents"] - principal_paid),
        principal_balance=format_money(principal_remaining),
        balance=format_money(max(0, row["total_due_cents"] - row["cumulative_paid_cents"])),
        paid_at=paid_at.isoformat(sep=" "),
        payment_day=paid_at.strftime("%A"),
        payment_date=paid_at.date().isoformat(),
        payment_month=paid_at.strftime("%B"),
        payment_year=paid_at.year,
        payment_time=paid_at.strftime("%H:%M:%S"),
    )
    return row_data


@app.post("/api/mpesa/callback")
def mpesa_callback():
    if not CALLBACK_TOKEN:
        return error("M-Pesa callback is not configured.", 503)
    supplied_token = request.headers.get("X-Callback-Token", "")
    if not hmac.compare_digest(supplied_token, CALLBACK_TOKEN):
        return error("Unauthorized callback.", 401)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return error("Expected a JSON payment callback.")
    try:
        transaction_id = required_text(payload, "TransID", "Transaction ID")
        account_id = required_text(payload, "BillRefNumber", "Paybill account reference")
        amount_cents = money_to_cents(payload.get("TransAmount"))
        shortcode = str(payload.get("BusinessShortCode", "")).strip()
        if PAYBILL and shortcode != PAYBILL:
            return error("Payment was received for an unexpected Paybill.", 400)
    except ValueError as exc:
        return error(str(exc))

    db = get_db()
    client = db.execute(
        "SELECT * FROM clients WHERE national_id = ?", (account_id,)
    ).fetchone()
    if client is None:
        return error("No client is registered with this account reference.", 404)
    loan = db.execute(
        "SELECT * FROM loans WHERE client_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (client["id"],),
    ).fetchone()
    if loan is None:
        return error("No active loan is available for this payment.", 409)

    try:
        paid_at = parse_payment_time(payload.get("TransTime")).isoformat(sep=" ")
    except ValueError as exc:
        return error(str(exc))
    try:
        db.execute(
            """INSERT INTO payments (loan_id, transaction_id, amount_cents, phone, paid_at, raw_payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                loan["id"],
                transaction_id,
                amount_cents,
                str(payload.get("MSISDN", "")),
                paid_at,
                request.get_data(as_text=True),
            ),
        )
    except sqlite3.IntegrityError:
        try:
            export_excel_ledger()
        except (OSError, sqlite3.Error) as exc:
            app.logger.exception("Excel ledger update failed while recovering duplicate payment")
            return error(f"Duplicate payment is already recorded, but the Excel ledger could not be updated: {exc}", 500)
        return jsonify({"accepted": True, "duplicate": True}), 200

    paid = db.execute(
        "SELECT SUM(amount_cents) FROM payments WHERE loan_id = ?", (loan["id"],)
    ).fetchone()[0]
    if paid >= loan["total_due_cents"]:
        db.execute("UPDATE loans SET status = 'settled' WHERE id = ?", (loan["id"],))
    balance = max(0, loan["total_due_cents"] - paid)
    message = (
        f"Payment of KES {format_money(amount_cents)} received. "
        f"Loan issued: KES {format_money(loan['principal_cents'])}; "
        f"balance: KES {format_money(balance)}; due date: {loan['due_on']}."
    )
    db.commit()
    queue_notification(db, client["id"], loan["id"], f"payment:{transaction_id}", message)
    try:
        export_excel_ledger()
    except (OSError, sqlite3.Error) as exc:
        app.logger.exception("Excel ledger update failed after Paybill payment")
        return error(f"Payment was recorded, but the Excel ledger could not be updated: {exc}", 500)
    return jsonify({"accepted": True, "balance": format_money(balance)})


def run_daily_reminders():
    db = connect_db()
    today = date.today()
    rows = db.execute(
        """SELECT l.*, c.id AS client_id, c.name, c.national_id,
                  COALESCE(p.paid, 0) AS paid
           FROM loans l JOIN clients c ON c.id = l.client_id
           LEFT JOIN (SELECT loan_id, SUM(amount_cents) AS paid FROM payments GROUP BY loan_id) p
             ON p.loan_id = l.id
           WHERE l.status = 'active'"""
    ).fetchall()
    queued = 0
    try:
        for loan in rows:
            age_days = (today - date.fromisoformat(loan["issued_on"])).days
            if age_days == 25 and loan["paid"] < loan["total_due_cents"]:
                balance = loan["total_due_cents"] - loan["paid"]
                message = (
                    f"Reminder: your loan payment is delayed. Outstanding balance: "
                    f"KES {format_money(balance)}; due date: {loan['due_on']}."
                )
                notification_id = queue_notification(
                    db, loan["client_id"], loan["id"], "day_25_reminder", message
                )
                queued += int(notification_id is not None)
    finally:
        db.close()
    return queued


@app.get("/api/notifications")
@staff_required
def notifications():
    rows = get_db().execute(
        """SELECT n.*, c.name AS client_name, c.phone
           FROM notifications n JOIN clients c ON c.id = n.client_id
           ORDER BY n.id DESC LIMIT 200"""
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/ledger/daily-payments")
@staff_required
def daily_ledger_payments():
    requested_date = request.args.get("date", date.today().isoformat())
    try:
        report_date = date.fromisoformat(requested_date)
        if report_date.isoformat() != requested_date:
            raise ValueError
    except ValueError:
        return error("Report date must use YYYY-MM-DD format.")

    db = get_db()
    try:
        sync_ledger_report(db, report_date)
    except (OSError, sqlite3.Error) as exc:
        app.logger.exception("Daily ledger report could not be refreshed")
        return error(f"Could not refresh the daily ledger report: {exc}", 500)

    rows = db.execute(
        """SELECT p.location, p.client_name, p.amount_cents, p.balance_cents,
                  sl.staff_id, u.full_name AS assigned_staff
           FROM ledger_report_payments p
           LEFT JOIN staff_locations sl ON sl.location = p.location
           LEFT JOIN staff_users u ON u.id = sl.staff_id
           WHERE p.report_date = ?
           ORDER BY p.location COLLATE NOCASE, p.client_name COLLATE NOCASE,
                    p.source_path, p.source_row""",
        (requested_date,),
    ).fetchall()
    my_locations = [
        row["location"]
        for row in db.execute(
            "SELECT location FROM staff_locations WHERE staff_id = ? ORDER BY location",
            (g.staff_user["id"],),
        ).fetchall()
    ]
    all_payments = [
        {
            "location": row["location"],
            "client_name": row["client_name"],
            "amount": format_money(row["amount_cents"]),
            "balance": (
                format_money(row["balance_cents"])
                if row["balance_cents"] is not None
                else None
            ),
            "assigned_staff": row["assigned_staff"] or "Unassigned",
        }
        for row in rows
    ]
    my_payments = [
        payment for payment in all_payments if payment["location"] in my_locations
    ]
    error_rows = db.execute(
        """SELECT source_path FROM ledger_report_scans
           WHERE report_date = ? AND error != ''""",
        (requested_date,),
    ).fetchall()
    unreadable_by_location = {}
    for row in error_rows:
        location = row["source_path"].split("/", 1)[0] or "Unassigned"
        unreadable_by_location[location] = unreadable_by_location.get(location, 0) + 1
    return jsonify(
        {
            "report_date": requested_date,
            "my_locations": my_locations,
            "my_payments": my_payments,
            "all_payments": all_payments,
            "unreadable_workbooks": sum(unreadable_by_location.values()),
            "unreadable_by_location": unreadable_by_location,
        }
    )


@app.cli.command("send-daily-reminders")
def send_daily_reminders_command():
    """Create and deliver the day-25 late-payment reminders."""
    count = run_daily_reminders()
    print(f"Processed day-25 reminders: {count}")


if __name__ == "__main__":
    app.run(debug=os.environ.get("APP_DEBUG", "false").lower() == "true")
