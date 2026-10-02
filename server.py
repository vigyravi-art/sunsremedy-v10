import os
import json
import hashlib
import secrets
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATABASE_URL = os.getenv("DATABASE_URL")
SEED_FILE = os.path.join(os.path.dirname(__file__), "historical_seed.json")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

app = FastAPI(title="Sunsremedy Spa Manager V10")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def password_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seed_historical_data(conn) -> None:
    # Import the historical records only once, when the new V10 sales table is empty.
    sale_count = conn.execute("SELECT COUNT(*) AS n FROM sales").fetchone()["n"]
    if int(sale_count) != 0 or not os.path.exists(SEED_FILE):
        return

    with open(SEED_FILE, encoding="utf-8") as f:
        seed = json.load(f)

    customer_cache = {}

    def get_customer(name: str, phone: str):
        key = (name.strip().lower(), phone.strip())
        if not name.strip():
            return None
        if key in customer_cache:
            return customer_cache[key]
        row = conn.execute(
            "SELECT id FROM customers WHERE lower(name)=lower(%s) AND phone=%s ORDER BY id LIMIT 1",
            (name.strip(), phone.strip()),
        ).fetchone()
        if row:
            customer_cache[key] = int(row["id"])
            return int(row["id"])
        row = conn.execute(
            "INSERT INTO customers(name, phone) VALUES(%s,%s) RETURNING id",
            (name.strip(), phone.strip()),
        ).fetchone()
        customer_cache[key] = int(row["id"])
        return int(row["id"])

    for item in seed.get("transactions", []):
        name = str(item.get("customer", "") or "")
        phone = str(item.get("phone", "") or "")
        customer_id = get_customer(name, phone)
        raw_amount = item.get("amount", 0)
        try:
            amount = float(raw_amount) if raw_amount not in ("", None) else 0.0
        except Exception:
            amount = 0.0

        conn.execute(
            """
            INSERT INTO sales(
                customer_id, sale_date, customer_name, phone, room, staff,
                service, amount, payment_method, notes
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                customer_id,
                item.get("date"),
                name,
                phone,
                str(item.get("room", "") or ""),
                str(item.get("staff", "") or ""),
                str(item.get("package", "") or ""),
                amount,
                str(item.get("payment", "") or ""),
                f"Imported historical record S.no {item.get('sno', '')}",
            ),
        )

    # Keep historical expenses, but never duplicate them.
    exp_count = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()["n"]
    if int(exp_count) == 0:
        for item in seed.get("expenses", []):
            raw_amount = item.get("amount", 0)
            try:
                amount = float(raw_amount) if raw_amount not in ("", None) else 0.0
            except Exception:
                amount = 0.0
            conn.execute(
                """
                INSERT INTO expenses(expense_date, description, amount, source)
                VALUES(%s,%s,%s,%s)
                """,
                (
                    item.get("date"),
                    str(item.get("description", "") or ""),
                    amount,
                    str(item.get("source", "historical") or "historical"),
                ),
            )

    conn.commit()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('manager','staff')),
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales (
                id BIGSERIAL PRIMARY KEY,
                customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
                sale_date DATE NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                room TEXT NOT NULL DEFAULT '',
                staff TEXT NOT NULL DEFAULT '',
                service TEXT NOT NULL DEFAULT '',
                amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                payment_method TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_phone ON sales(phone)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id BIGSERIAL PRIMARY KEY,
                expense_date DATE NOT NULL,
                description TEXT NOT NULL,
                amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_date ON expenses(expense_date)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES app_users(username) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        conn.execute(
            """
            INSERT INTO app_users(username, password_hash, role)
            VALUES (%s, %s, 'manager')
            ON CONFLICT (username) DO NOTHING
            """,
            ("manager", password_hash("2580")),
        )
        conn.execute(
            """
            INSERT INTO app_users(username, password_hash, role)
            VALUES (%s, %s, 'staff')
            ON CONFLICT (username) DO NOTHING
            """,
            ("staff", password_hash("staff123")),
        )

        seed_historical_data(conn)
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()


def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")
    token = authorization[7:]
    with db() as conn:
        row = conn.execute(
            """
            SELECT u.username, u.role
            FROM app_sessions s
            JOIN app_users u ON u.username = s.username
            WHERE s.token = %s AND u.active = TRUE
            """,
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Session expired")
    return dict(row)


def manager_only(user):
    if user["role"] != "manager":
        raise HTTPException(status_code=403, detail="Manager access required")


class LoginBody(BaseModel):
    username: str
    password: str


class SaleBody(BaseModel):
    sale_date: date
    customer_name: str
    phone: str = ""
    room: str = ""
    staff: str = ""
    service: str = ""
    amount: float
    payment_method: str
    notes: str = ""


class ExpenseBody(BaseModel):
    expense_date: date
    description: str
    amount: float
    source: str = "manual"


@app.post("/api/login")
def login(body: LoginBody):
    username = body.username.strip().lower()
    with db() as conn:
        user = conn.execute(
            "SELECT username, password_hash, role FROM app_users WHERE username=%s AND active=TRUE",
            (username,),
        ).fetchone()
        if not user or password_hash(body.password) != user["password_hash"]:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = secrets.token_urlsafe(40)
        conn.execute(
            "INSERT INTO app_sessions(token,username) VALUES(%s,%s)",
            (token, username),
        )
        conn.commit()
    return {"token": token, "username": username, "role": user["role"]}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        with db() as conn:
            conn.execute("DELETE FROM app_sessions WHERE token=%s", (authorization[7:],))
            conn.commit()
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user)):
    manager_only(user)
    with db() as conn:
        today_india = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        current_start = today_india.replace(day=1)
        if current_start.month == 12:
            next_start = date(current_start.year + 1, 1, 1)
        else:
            next_start = date(current_start.year, current_start.month + 1, 1)

        month = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS sales, COUNT(*) AS sale_count
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            """,
            (current_start, next_start),
        ).fetchone()

        exp = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS expenses, COUNT(*) AS expense_count
            FROM expenses
            WHERE expense_date >= %s AND expense_date < %s
            """,
            (current_start, next_start),
        ).fetchone()

        daily = conn.execute(
            """
            SELECT sale_date AS day, COALESCE(SUM(amount),0) AS sales, COUNT(*) AS visits
            FROM sales
            GROUP BY sale_date
            ORDER BY sale_date DESC
            LIMIT 10
            """
        ).fetchall()

        payment_methods = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified') AS payment_method,
                   COUNT(*) AS visits,
                   COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            GROUP BY COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified')
            ORDER BY SUM(amount) DESC, payment_method
            """,
            (current_start, next_start),
        ).fetchall()

    return {
        "month": current_start.strftime("%Y-%m"),
        "sales": float(month["sales"] or 0),
        "sale_count": int(month["sale_count"] or 0),
        "expenses": float(exp["expenses"] or 0),
        "expense_count": int(exp["expense_count"] or 0),
        "profit": float((month["sales"] or 0) - (exp["expenses"] or 0)),
        "daily": [
            {
                "day": r["day"].isoformat(),
                "sales": float(r["sales"] or 0),
                "visits": int(r["visits"] or 0),
            }
            for r in daily
        ],
        "payment_methods": [
            {
                "payment_method": r["payment_method"],
                "visits": int(r["visits"] or 0),
                "sales": float(r["sales"] or 0),
            }
            for r in payment_methods
        ],
    }


@app.get("/api/reports")
def reports(month: Optional[str] = None, user=Depends(current_user)):
    """Detailed manager report for a selected YYYY-MM month."""
    manager_only(user)

    if month:
        try:
            current_start = date.fromisoformat(month + "-01")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid month. Use YYYY-MM")
    else:
        current_start = datetime.now(ZoneInfo("Asia/Kolkata")).date().replace(day=1)

    if current_start.month == 12:
        next_start = date(current_start.year + 1, 1, 1)
    else:
        next_start = date(current_start.year, current_start.month + 1, 1)

    with db() as conn:
        summary = conn.execute(
            """
            SELECT
                COALESCE(SUM(amount),0) AS sales,
                COUNT(*) AS sale_count
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            """,
            (current_start, next_start),
        ).fetchone()

        expense_summary = conn.execute(
            """
            SELECT
                COALESCE(SUM(amount),0) AS expenses,
                COUNT(*) AS expense_count
            FROM expenses
            WHERE expense_date >= %s AND expense_date < %s
            """,
            (current_start, next_start),
        ).fetchone()

        daily_sales = conn.execute(
            """
            SELECT sale_date AS day,
                   COUNT(*) AS visits,
                   COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            GROUP BY sale_date
            ORDER BY sale_date ASC
            """,
            (current_start, next_start),
        ).fetchall()

        payment_methods = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified') AS payment_method,
                   COUNT(*) AS visits,
                   COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            GROUP BY COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified')
            ORDER BY SUM(amount) DESC, payment_method
            """,
            (current_start, next_start),
        ).fetchall()

        staff = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(staff), ''), 'Not specified') AS staff,
                   COUNT(*) AS visits,
                   COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE sale_date >= %s AND sale_date < %s
            GROUP BY COALESCE(NULLIF(TRIM(staff), ''), 'Not specified')
            ORDER BY SUM(amount) DESC, staff
            """,
            (current_start, next_start),
        ).fetchall()

        expense_rows = conn.execute(
            """
            SELECT id, expense_date, description, amount, source
            FROM expenses
            WHERE expense_date >= %s AND expense_date < %s
            ORDER BY expense_date DESC, id DESC
            """,
            (current_start, next_start),
        ).fetchall()

    sales_total = float(summary["sales"] or 0)
    expenses_total = float(expense_summary["expenses"] or 0)

    return {
        "month": current_start.strftime("%Y-%m"),
        "sales": sales_total,
        "sale_count": int(summary["sale_count"] or 0),
        "expenses": expenses_total,
        "expense_count": int(expense_summary["expense_count"] or 0),
        "profit": sales_total - expenses_total,
        "daily_sales": [
            {
                "day": r["day"].isoformat(),
                "visits": int(r["visits"] or 0),
                "sales": float(r["sales"] or 0),
            }
            for r in daily_sales
        ],
        "payment_methods": [
            {
                "payment_method": r["payment_method"],
                "visits": int(r["visits"] or 0),
                "sales": float(r["sales"] or 0),
            }
            for r in payment_methods
        ],
        "staff": [
            {
                "staff": r["staff"],
                "visits": int(r["visits"] or 0),
                "sales": float(r["sales"] or 0),
            }
            for r in staff
        ],
        "expense_rows": [
            {
                "id": int(r["id"]),
                "expense_date": r["expense_date"].isoformat(),
                "description": r["description"],
                "amount": float(r["amount"] or 0),
                "source": r["source"],
            }
            for r in expense_rows
        ],
    }



@app.get("/api/staff/dashboard")
def staff_dashboard(user=Depends(current_user)):
    """Today's staff dashboard: current-day totals and payment-method breakdown only."""
    if user["role"] != "staff":
        raise HTTPException(status_code=403, detail="Staff dashboard only")

    today_india = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    with db() as conn:
        sales_summary = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS sales, COUNT(*) AS visits
            FROM sales
            WHERE sale_date = %s
            """,
            (today_india,),
        ).fetchone()

        expense_summary = conn.execute(
            """
            SELECT COALESCE(SUM(amount),0) AS expenses, COUNT(*) AS expense_count
            FROM expenses
            WHERE expense_date = %s
            """,
            (today_india,),
        ).fetchone()

        payment_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified') AS payment_method,
                   COUNT(*) AS visits,
                   COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE sale_date = %s
            GROUP BY COALESCE(NULLIF(TRIM(payment_method), ''), 'Not specified')
            ORDER BY SUM(amount) DESC, payment_method
            """,
            (today_india,),
        ).fetchall()

    return {
        "date": today_india.isoformat(),
        "sales": float(sales_summary["sales"] or 0),
        "visits": int(sales_summary["visits"] or 0),
        "expenses": float(expense_summary["expenses"] or 0),
        "expense_count": int(expense_summary["expense_count"] or 0),
        "payment_methods": [
            {
                "payment_method": r["payment_method"],
                "visits": int(r["visits"] or 0),
                "sales": float(r["sales"] or 0),
            }
            for r in payment_rows
        ],
    }

@app.get("/api/sales")
def list_sales(
    search: str = "",
    sale_date: Optional[date] = None,
    payment_method: str = "",
    staff: str = "",
    user=Depends(current_user),
):
    q = f"%{search.strip()}%"
    sql = """
        SELECT id, sale_date, customer_name, phone, room, staff, service,
               amount, payment_method, notes
        FROM sales
        WHERE (%s = '' OR customer_name ILIKE %s OR phone ILIKE %s OR staff ILIKE %s OR service ILIKE %s)
          AND (%s IS NULL OR sale_date = %s)
          AND (%s = '' OR payment_method = %s)
          AND (%s = '' OR staff = %s)
        ORDER BY sale_date DESC, id DESC
        LIMIT 1000
    """
    with db() as conn:
        rows = conn.execute(
            sql,
            (search, q, q, q, q, sale_date, sale_date, payment_method, payment_method, staff, staff),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/sales")
def create_sale(body: SaleBody, user=Depends(current_user)):
    with db() as conn:
        customer_id = upsert_customer(conn, body.customer_name, body.phone)
        row = conn.execute(
            """
            INSERT INTO sales(
                customer_id,sale_date,customer_name,phone,room,staff,service,
                amount,payment_method,notes
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                customer_id, body.sale_date, body.customer_name.strip(), body.phone.strip(),
                body.room, body.staff.strip(), body.service.strip(), body.amount,
                body.payment_method, body.notes,
            ),
        ).fetchone()
        conn.commit()
    return {"id": int(row["id"])}


@app.put("/api/sales/{sale_id}")
def update_sale(sale_id: int, body: SaleBody, user=Depends(current_user)):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM sales WHERE id=%s", (sale_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Sale not found")
        customer_id = upsert_customer(conn, body.customer_name, body.phone)
        conn.execute(
            """
            UPDATE sales
            SET customer_id=%s,sale_date=%s,customer_name=%s,phone=%s,room=%s,
                staff=%s,service=%s,amount=%s,payment_method=%s,notes=%s,updated_at=NOW()
            WHERE id=%s
            """,
            (
                customer_id, body.sale_date, body.customer_name.strip(), body.phone.strip(),
                body.room, body.staff.strip(), body.service.strip(), body.amount,
                body.payment_method, body.notes, sale_id,
            ),
        )
        conn.commit()
    return {"ok": True}


@app.delete("/api/sales/{sale_id}")
def delete_sale(sale_id: int, user=Depends(current_user)):
    with db() as conn:
        cur = conn.execute("DELETE FROM sales WHERE id=%s", (sale_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Sale not found")
        conn.commit()
    return {"ok": True}


def upsert_customer(conn, name: str, phone: str) -> Optional[int]:
    name = (name or "").strip()
    phone = (phone or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM customers WHERE lower(name)=lower(%s) AND phone=%s ORDER BY id LIMIT 1",
        (name, phone),
    ).fetchone()
    if row:
        conn.execute("UPDATE customers SET updated_at=NOW() WHERE id=%s", (row["id"],))
        return int(row["id"])
    row = conn.execute(
        "INSERT INTO customers(name,phone) VALUES(%s,%s) RETURNING id",
        (name, phone),
    ).fetchone()
    return int(row["id"])


@app.get("/api/customers")
def customers(search: str = "", user=Depends(current_user)):
    q = f"%{search.strip()}%"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.phone, COUNT(s.id) AS visits,
                   MAX(s.sale_date) AS last_visit,
                   COALESCE(SUM(s.amount),0) AS spend
            FROM customers c
            LEFT JOIN sales s ON s.customer_id=c.id
            WHERE (%s='' OR c.name ILIKE %s OR c.phone ILIKE %s)
            GROUP BY c.id
            HAVING COUNT(s.id) > 0
            ORDER BY MAX(s.sale_date) DESC NULLS LAST, c.name
            LIMIT 500
            """,
            (search, q, q),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/customers/{customer_id}/history")
def customer_history(customer_id: int, user=Depends(current_user)):
    with db() as conn:
        customer = conn.execute(
            "SELECT id,name,phone FROM customers WHERE id=%s",
            (customer_id,),
        ).fetchone()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        visits = conn.execute(
            """
            SELECT id,sale_date,room,staff,service,amount,payment_method,notes
            FROM sales
            WHERE customer_id=%s
            ORDER BY sale_date DESC,id DESC
            """,
            (customer_id,),
        ).fetchall()
    return {"customer": dict(customer), "visits": [dict(v) for v in visits]}


@app.get("/api/expenses")
def list_expenses(search: str = "", expense_date: Optional[date] = None, user=Depends(current_user)):
    q = f"%{search.strip()}%"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id,expense_date,description,amount,source
            FROM expenses
            WHERE (%s='' OR description ILIKE %s)
              AND (%s IS NULL OR expense_date=%s)
            ORDER BY expense_date DESC,id DESC
            LIMIT 1000
            """,
            (search, q, expense_date, expense_date),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/expenses")
def create_expense(body: ExpenseBody, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO expenses(expense_date,description,amount,source)
            VALUES(%s,%s,%s,%s) RETURNING id
            """,
            (body.expense_date, body.description.strip(), body.amount, body.source),
        ).fetchone()
        conn.commit()
    return {"id": int(row["id"])}


@app.put("/api/expenses/{expense_id}")
def update_expense(expense_id: int, body: ExpenseBody, user=Depends(current_user)):
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE expenses
            SET expense_date=%s,description=%s,amount=%s,source=%s,updated_at=NOW()
            WHERE id=%s
            """,
            (body.expense_date, body.description.strip(), body.amount, body.source, expense_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Expense not found")
        conn.commit()
    return {"ok": True}


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: int, user=Depends(current_user)):
    with db() as conn:
        cur = conn.execute("DELETE FROM expenses WHERE id=%s", (expense_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Expense not found")
        conn.commit()
    return {"ok": True}


@app.get("/api/staff")
def staff_summary(user=Depends(current_user)):
    manager_only(user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT staff, COUNT(*) AS visits, COALESCE(SUM(amount),0) AS sales
            FROM sales
            WHERE staff<>''
            GROUP BY staff
            ORDER BY COUNT(*) DESC, staff
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)
