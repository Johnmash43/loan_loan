# Nama Traders

A small staff-facing loan ledger built with a React/JavaScript frontend, a Python/Flask API, and SQLite. The interface uses a soft orange and white theme. Client records include guarantor details and an optional uploaded Word registration form; loans, Paybill repayments, balances, and notification attempts are tracked in one place.

> **Payment rule:** cash is not supported. Repayments must arrive through the configured M-Pesa Paybill. The customer's National ID is used as the Paybill account reference.

## Features

- Administrator and staff sign-in. Administrators can create staff accounts; staff accounts cannot create other accounts or administer the system.
- Staff cannot issue loans without administrator approval. After a loan is fully paid, staff can request a renewal; the administrator receives an in-app approval pop-up, and approval grants that staff member one renewal with access only to its loan number, issued amount, total repayment, issue date, and due date.
- Client and guarantor registration, with client and guarantor phone numbers and an optional `.docx` registration-form upload.
- Loan ledger with principal, flat interest, total repayment, term, amount paid, balance, and final due date.
- Repayment starts on the calendar day after issue. The selected term is 25–30 days; total repayment is principal plus flat interest, distributed evenly as a daily installment estimate.
- Paybill callback matches the `BillRefNumber` to the client's National ID, deduplicates M-Pesa transaction IDs, updates the loan balance, and creates a payment notification.
- An automatically refreshed Excel workbook with a client account sheet and a payment history sheet. The payment history includes each payment's principal repayment, interest earned, remaining principal, total balance, weekday, date, month, year, and time.
- A searchable **Ledgers** page that opens and edits existing client workbooks in place. Administrators can edit ledgers; staff can request edit permission, which administrators may grant or revoke. Staff access remains limited to their assigned locations.
- Staff can pause and reopen a client's account. A paused account cannot receive a new loan. Clients with an active loan cannot receive another.
- A daily command creates the late-payment reminder on day 25 when a balance remains.
- Notification history records pending, sent, and failed deliveries.
- The Notifications tab builds date-based payment reports from the client workbooks. Staff see their assigned locations and a company-wide list; administrators see all locations grouped separately.

## Requirements

- Python 3.10 or newer
- Node.js 18 or newer and npm

## Local setup

Open two terminals from the project directory.

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
export FLASK_SECRET_KEY="replace-with-a-long-random-secret"
export STAFF_USERNAME="admin"
export STAFF_PASSWORD="change-this-password"
python backend/app.py
```

The local demo account defaults to `admin` / `admin123` if the corresponding environment variables are not set. Change these values before using the app beyond a local demo.

The first backend startup creates the administrator account. Additional accounts are created from the **Staff** tab after signing in as an administrator. New staff passwords must be at least 10 characters and are stored as password hashes.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL printed in the terminal. The development server proxies `/api` requests to Flask on `127.0.0.1:5000`.

To create a production frontend bundle, run `npm run build` in `frontend/`; serve `frontend/dist` from a web server configured to proxy `/api` to Flask.

## Client forms and Excel ledger

Attach the generated registration form by selecting the `.docx` Word file in the client registration form. The file is stored privately under `data/forms/` with an internal generated filename; staff can download it from the client list. PDF files and web links are not accepted as registration forms.

After a client is registered, a loan is issued, or an M-Pesa payment is recorded, the backend regenerates `data/loan_ledger.xlsx` from the SQLite database. This generated workbook contains:

- **Client Accounts**: client and guarantor details, Word form name, account state, loan principal, interest, original total issued, total paid, remaining balance, term, and dates.
- **Payment History**: one row per Paybill transaction, including principal and interest, original total issued, transaction payment, cumulative paid, remaining balance, weekday, date, month, year, and time.

The **Ledgers** page reads client workbooks directly from `CLIENT_LEDGER_DIRECTORY` (default: `~/MASH/LEDGERS`). Set this variable to the folder containing the existing client ledger directories. Administrators can edit ledger table cells; staff can submit an access request for administrator approval. Granted staff may edit only ledgers in their assigned locations. Edits are saved to the original workbook, formula cells are protected, and stale edits are rejected if the workbook changed after it was opened. Temporary Excel lock files are excluded. Set `EXCEL_LEDGER_PATH` and `FORMS_DIRECTORY` to change the generated workbook and form locations. SQLite remains the source of truth for app-managed client and payment data.

Daily payment reports use the subfolder name as the location and refresh changed workbooks when opened. The configured locations are: Staff1 (NJORO, PIAVE NJORO), Staff2 (ELBURGON & MOLO, MAILI TATU), and Staff3 (EGERTON, EGERTON 2, KIHINGO & STOO). Locations without a staff assignment remain visible to administrators.

## M-Pesa and notifications

Set `MPESA_PAYBILL` and `MPESA_CALLBACK_TOKEN` in the Flask environment. Configure the trusted integration/proxy to post Safaricom C2B confirmation JSON to:

```text
POST /api/mpesa/callback
X-Callback-Token: <the configured secret>
Content-Type: application/json
```

The callback must include the standard fields `TransID`, `TransAmount`, `BusinessShortCode`, `BillRefNumber`, and optionally `MSISDN` and `TransTime`. `BillRefNumber` must exactly match a registered client's National ID. The callback endpoint is not a substitute for registering the Paybill confirmation URL with Safaricom. Restrict access to this endpoint at the network/proxy layer and inject the callback token only from that trusted integration; do not expose the token in browser code.

To deliver SMS or other payment/reminder messages, set `NOTIFICATION_WEBHOOK_URL` and optionally `NOTIFICATION_WEBHOOK_TOKEN`. The webhook receives a JSON object with `to` and `message`; it should return a successful HTTP status after accepting the message. Without a configured webhook, generated notifications remain visibly pending in the notification log. Delivery failures are saved and surfaced in the dashboard.

Run the day-25 reminder task once daily using your host scheduler, from the project root and with the same environment variables as the Flask app:

```bash
flask --app backend.app send-daily-reminders
```

Schedule this command daily. It is idempotent per loan and reminder type.

## Loan calculation

For principal `P`, flat interest rate `r` percent, and term `t` days:

```text
interest = round(P × r ÷ 100, 2)
total repayment = P + interest
daily installment estimate = round(total repayment ÷ t, 2)
first repayment date = issue date + 1 day
final due date = issue date + t days
```

The interest rate is entered for each loan (the form defaults to 25%, matching the existing ledgers). Payments are split in proportion to principal and total repayment; at 25%, that is 80% principal and 20% interest. The daily figure is an estimate; rounding means the final installments may need adjustment to equal the total due. Actual Paybill deposits reduce the outstanding balance in receipt order.

## Important production notes

- Set `APP_ENV=production`, `FLASK_SECRET_KEY`, `STAFF_USERNAME`, `STAFF_PASSWORD`, `MPESA_PAYBILL`, and `MPESA_CALLBACK_TOKEN`. Production startup rejects missing security/Paybill configuration. The initial administrator password is taken from `STAFF_PASSWORD`; later staff credentials are managed in the application.
- Serve Flask behind HTTPS and set `COOKIE_SECURE=true`; use a process manager and back up the SQLite database.
- The app processes payment confirmations delivered to its callback endpoint; Paybill onboarding and callback forwarding remain deployment responsibilities.
- The application does not implement loan underwriting, credit bureau checks, or legal/regulatory compliance workflows. Confirm applicable lending, privacy, and consumer-protection requirements before production use.
