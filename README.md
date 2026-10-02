# Sunsremedy Spa Manager V10 — clean cloud build

A fresh FastAPI + PostgreSQL spa billing/management application with a V8-style interface.

## Features
- Manager and Staff login
- Sales create/edit/delete
- Room dropdown: Big Thai Room, Small Thai Room, Middle Room, 3rd Room
- Payment dropdown: Cash, GPay, Card, Membership, Bank/UPI, Other
- Customer search and detailed visit history
- Expenses create/edit/delete
- Manager dashboard with current-month sales, expenses, profit and daily sales
- Dashboard figures are calculated directly from the sales/expense tables
- Staff cannot access manager-only dashboard, reports or staff financial summaries
- Staff can enter/edit/delete sales and expenses and can see individual sale amount/payment in sales and customer history

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn server:app --host 0.0.0.0 --port $PORT`

Environment variable:
`DATABASE_URL` = Render Postgres **Internal Database URL**

## Initial accounts
Manager: `manager` / `2580`
Staff: `staff` / `staff123`

For production, change the passwords and keep the database URL only in Render environment variables.
