# app.py
import os
from datetime import datetime, date, timedelta
from decimal import Decimal
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from supabase import create_client, Client

load_dotenv()

app = Flask(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be configured.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def ok(data=None, status=200):
    return jsonify(data if data is not None else {"ok": True}), status


def fail(message, status=400):
    return jsonify({"error": str(message)}), status


def api_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return fail(exc, 500)
    return wrapper


def body():
    return request.get_json(silent=True) or {}


def clean_payload(payload, allowed):
    return {k: v for k, v in payload.items() if k in allowed}


def fiscal_args():
    fy = request.args.get("fiscal_year", type=int)
    fm = request.args.get("fiscal_month", type=int)
    return fy, fm


def validate_period(fiscal_year, fiscal_month):
    if fiscal_year is None or int(fiscal_year) < 2000:
        raise ValueError("Valid fiscal_year is required.")
    if fiscal_month is None or int(fiscal_month) not in range(1, 14):
        raise ValueError("fiscal_month must be between 1 and 13.")


def money(value):
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def calculate_reading_status(previous_reading, current_reading, customer_status="active"):
    prev = Decimal(str(previous_reading or 0))
    curr = Decimal(str(current_reading or 0))
    consumption = curr - prev

    if customer_status == "disconnected":
        return "disconnected"
    if consumption < 0:
        return "negative"
    if consumption == 0:
        return "zero"
    if consumption > 100:
        return "exaggerated"
    return "normal"


def tariff(consumption):
    c = Decimal(str(max(float(consumption or 0), 0)))
    total = Decimal("0")

    slabs = [
        (Decimal("5"), Decimal("6.00")),
        (Decimal("10"), Decimal("8.50")),
        (Decimal("15"), Decimal("12.00")),
        (Decimal("20"), Decimal("16.00")),
    ]

    remaining = c
    for size, rate in slabs:
        used = min(remaining, size)
        total += used * rate
        remaining -= used
        if remaining <= 0:
            break

    if remaining > 0:
        total += remaining * Decimal("22.00")

    service_charge = Decimal("20.00") if c > 0 else Decimal("0.00")
    amount = total + service_charge
    tax = amount * Decimal("0.15")
    return money(amount), money(tax), money(amount + tax)


def get_customer(customer_id):
    result = supabase.table("customers").select("*").eq("id", customer_id).single().execute()
    return result.data


def rebuild_summary(fiscal_year, fiscal_month):
    validate_period(fiscal_year, fiscal_month)

    readings = supabase.table("meter_readings").select("*").eq("fiscal_year", fiscal_year).eq("fiscal_month", fiscal_month).execute().data or []

    analysis = {
        "normal": 0,
        "negative": 0,
        "exaggerated": 0,
        "zero": 0,
        "disconnected": 0,
    }

    total_consumption = Decimal("0")
    for row in readings:
        status = row.get("status") or "normal"
        if status in analysis:
            analysis[status] += 1
        total_consumption += Decimal(str(row.get("consumption") or 0))

    disconnected = analysis["disconnected"]
    negative = analysis["negative"]
    exaggerated = analysis["exaggerated"]
    zero = analysis["zero"]

    payload = {
        "fiscal_year": fiscal_year,
        "fiscal_month": fiscal_month,
        "total_readings": len(readings),
        "total_consumption": float(total_consumption),
        "negative_readings": negative,
        "exaggerated_readings": exaggerated,
        "zero_readings": zero,
        "disconnected_connections": disconnected,
        "analysis": analysis,
        "updated_at": datetime.utcnow().isoformat(),
    }

    existing = supabase.table("reading_period_summary").select("id").eq("fiscal_year", fiscal_year).eq("fiscal_month", fiscal_month).execute().data
    if existing:
        result = supabase.table("reading_period_summary").update(payload).eq("id", existing[0]["id"]).execute()
    else:
        result = supabase.table("reading_period_summary").insert(payload).execute()
    return result.data[0] if result.data else payload


def generate_bill(reading):
    consumption = max(float(reading.get("consumption") or 0), 0)
    amount, tax, total = tariff(consumption)
    due_date = (date.today() + timedelta(days=30)).isoformat()

    payload = {
        "customer_id": reading["customer_id"],
        "meter_reading_id": reading["id"],
        "fiscal_year": reading["fiscal_year"],
        "fiscal_month": reading["fiscal_month"],
        "consumption": consumption,
        "rate": 0,
        "amount": amount,
        "tax": tax,
        "penalty": 0,
        "total_amount": total,
        "paid_amount": 0,
        "status": "unpaid",
        "due_date": due_date,
    }

    existing = supabase.table("billing").select("id,paid_amount").eq("meter_reading_id", reading["id"]).execute().data
    if existing:
        paid_amount = float(existing[0].get("paid_amount") or 0)
        payload["paid_amount"] = paid_amount
        payload["status"] = "paid" if paid_amount >= total else ("partial" if paid_amount > 0 else "unpaid")
        return supabase.table("billing").update(payload).eq("id", existing[0]["id"]).execute().data

    return supabase.table("billing").insert(payload).execute().data


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/reference-data")
@api_guard
def reference_data():
    zones = supabase.table("zones").select("*").order("name").execute().data or []
    employees = supabase.table("employees").select("*").order("name").execute().data or []
    devices = supabase.table("devices").select("*").order("serial_number").execute().data or []

    groups = supabase.table("reading_groups").select("*, zones(name)").order("name").execute().data or []
    for g in groups:
        g["zone_name"] = (g.get("zones") or {}).get("name")
        g.pop("zones", None)

    return ok({
        "zones": zones,
        "groups": groups,
        "employees": employees,
        "devices": devices,
    })


@app.get("/api/dashboard")
@api_guard
def dashboard():
    fy, fm = fiscal_args()
    validate_period(fy, fm)

    customers = supabase.table("customers").select("id", count="exact").execute()
    readings = supabase.table("meter_readings").select("*").eq("fiscal_year", fy).eq("fiscal_month", fm).execute().data or []
    bills = supabase.table("billing").select("*").eq("fiscal_year", fy).eq("fiscal_month", fm).execute().data or []
    maintenance = supabase.table("maintenance_requests").select("id", count="exact").in_("status", ["open", "assigned", "in_progress"]).execute()
    readers = supabase.table("employees").select("id", count="exact").eq("role", "meter_reader").eq("is_active", True).execute()

    total_consumption = sum(float(r.get("consumption") or 0) for r in readings)
    total_bill_amount = sum(float(b.get("total_amount") or 0) for b in bills)

    analysis = {"normal": 0, "negative": 0, "exaggerated": 0, "zero": 0, "disconnected": 0}
    for r in readings:
        if r.get("status") in analysis:
            analysis[r["status"]] += 1

    trend_rows = supabase.table("billing").select("fiscal_year,fiscal_month,consumption").order("fiscal_year").order("fiscal_month").execute().data or []
    buckets = {}
    for b in trend_rows:
        key = (b["fiscal_year"], b["fiscal_month"])
        buckets[key] = buckets.get(key, 0) + float(b.get("consumption") or 0)

    trend = [
        {"fiscal_year": y, "fiscal_month": m, "total_consumption": round(v, 2)}
        for (y, m), v in sorted(buckets.items())[-12:]
    ]

    return ok({
        "total_customers": customers.count or 0,
        "total_consumption": round(total_consumption, 2),
        "total_bill_amount": round(total_bill_amount, 2),
        "pending_maintenance": maintenance.count or 0,
        "active_meter_readers": readers.count or 0,
        "analysis": analysis,
        "trend": trend,
    })


CUSTOMER_FIELDS = {
    "customer_code", "old_code", "name", "phone", "email", "address", "zone_id",
    "reading_group_id", "device_id", "direct_register", "status"
}


@app.get("/api/customers")
@api_guard
def customers_list():
    query = request.args.get("q")
    status = request.args.get("status")
    zone_id = request.args.get("zone_id")

    req = supabase.table("customers").select("*, zones(name), reading_groups(name)")
    if query:
        req = req.or_(f"name.ilike.%{query}%,customer_code.ilike.%{query}%,old_code.ilike.%{query}%,phone.ilike.%{query}%")
    if status:
        req = req.eq("status", status)
    if zone_id:
        req = req.eq("zone_id", zone_id)

    rows = req.order("created_at", desc=True).execute().data or []
    for r in rows:
        r["zone_name"] = (r.get("zones") or {}).get("name")
        r["reading_group_name"] = (r.get("reading_groups") or {}).get("name")
        r.pop("zones", None)
        r.pop("reading_groups", None)
    return ok(rows)


@app.post("/api/customers")
@api_guard
def customers_create():
    payload = clean_payload(body(), CUSTOMER_FIELDS)
    if not payload.get("customer_code") or not payload.get("name"):
        return fail("customer_code and name are required.")
    result = supabase.table("customers").insert(payload).execute()
    return ok(result.data[0], 201)


@app.get("/api/customers/<customer_id>")
@api_guard
def customers_detail(customer_id):
    customer = supabase.table("customers").select("*, zones(name), reading_groups(name)").eq("id", customer_id).single().execute().data
    readings = supabase.table("meter_readings").select("*").eq("customer_id", customer_id).order("fiscal_year", desc=True).order("fiscal_month", desc=True).limit(12).execute().data or []
    bills = supabase.table("billing").select("*").eq("customer_id", customer_id).order("created_at", desc=True).limit(12).execute().data or []
    return ok({"customer": customer, "readings": readings, "billing": bills})


@app.put("/api/customers/<customer_id>")
@api_guard
def customers_update(customer_id):
    payload = clean_payload(body(), CUSTOMER_FIELDS)
    result = supabase.table("customers").update(payload).eq("id", customer_id).execute()
    return ok(result.data[0] if result.data else {})


@app.delete("/api/customers/<customer_id>")
@api_guard
def customers_delete(customer_id):
    supabase.table("customers").delete().eq("id", customer_id).execute()
    return ok()


READING_FIELDS = {
    "customer_id", "employee_id", "device_id", "fiscal_year", "fiscal_month",
    "previous_reading", "current_reading", "reading_date", "status", "notes"
}


@app.get("/api/readings")
@api_guard
def readings_list():
    fy, fm = fiscal_args()
    req = supabase.table("meter_readings").select("*, customers(name,customer_code,status), employees(name)")
    if fy:
        req = req.eq("fiscal_year", fy)
    if fm:
        req = req.eq("fiscal_month", fm)

    rows = req.order("created_at", desc=True).execute().data or []
    for r in rows:
        r["customer_name"] = (r.get("customers") or {}).get("name")
        r["customer_code"] = (r.get("customers") or {}).get("customer_code")
        r["employee_name"] = (r.get("employees") or {}).get("name")
        r.pop("customers", None)
        r.pop("employees", None)
    return ok(rows)


@app.post("/api/readings")
@api_guard
def readings_create():
    payload = clean_payload(body(), READING_FIELDS)
    validate_period(payload.get("fiscal_year"), payload.get("fiscal_month"))

    customer = get_customer(payload["customer_id"])
    previous = payload.get("previous_reading") or 0
    current = payload.get("current_reading") or 0
    payload["consumption"] = float(Decimal(str(current)) - Decimal(str(previous)))
    payload["status"] = calculate_reading_status(previous, current, customer.get("status", "active"))
    payload.setdefault("reading_date", date.today().isoformat())

    result = supabase.table("meter_readings").insert(payload).execute()
    reading = result.data[0]
    generate_bill(reading)
    rebuild_summary(reading["fiscal_year"], reading["fiscal_month"])
    return ok(reading, 201)


@app.put("/api/readings/<reading_id>")
@api_guard
def readings_update(reading_id):
    existing = supabase.table("meter_readings").select("*").eq("id", reading_id).single().execute().data
    payload = clean_payload(body(), READING_FIELDS)
    merged = {**existing, **payload}

    validate_period(merged.get("fiscal_year"), merged.get("fiscal_month"))
    customer = get_customer(merged["customer_id"])

    previous = merged.get("previous_reading") or 0
    current = merged.get("current_reading") or 0
    payload["consumption"] = float(Decimal(str(current)) - Decimal(str(previous)))
    payload["status"] = calculate_reading_status(previous, current, customer.get("status", "active"))

    result = supabase.table("meter_readings").update(payload).eq("id", reading_id).execute()
    reading = result.data[0]
    generate_bill(reading)
    rebuild_summary(reading["fiscal_year"], reading["fiscal_month"])
    return ok(reading)


@app.delete("/api/readings/<reading_id>")
@api_guard
def readings_delete(reading_id):
    existing = supabase.table("meter_readings").select("*").eq("id", reading_id).single().execute().data
    supabase.table("billing").delete().eq("meter_reading_id", reading_id).execute()
    supabase.table("meter_readings").delete().eq("id", reading_id).execute()
    rebuild_summary(existing["fiscal_year"], existing["fiscal_month"])
    return ok()


@app.get("/api/reading-summary")
@api_guard
def reading_summary():
    fy, fm = fiscal_args()
    validate_period(fy, fm)
    rows = supabase.table("reading_period_summary").select("*").eq("fiscal_year", fy).eq("fiscal_month", fm).execute().data
    if rows:
        return ok(rows[0])
    return ok(rebuild_summary(fy, fm))


@app.post("/api/reading-summary/rebuild")
@api_guard
def reading_summary_rebuild():
    payload = body()
    return ok(rebuild_summary(payload.get("fiscal_year"), payload.get("fiscal_month")))


@app.get("/api/billing")
@api_guard
def billing_list():
    fy, fm = fiscal_args()
    req = supabase.table("billing").select("*, customers(name,customer_code)")
    if fy:
        req = req.eq("fiscal_year", fy)
    if fm:
        req = req.eq("fiscal_month", fm)

    rows = req.order("created_at", desc=True).execute().data or []
    for r in rows:
        r["customer_name"] = (r.get("customers") or {}).get("name")
        r["customer_code"] = (r.get("customers") or {}).get("customer_code")
        r.pop("customers", None)
    return ok(rows)


@app.put("/api/billing/<bill_id>")
@api_guard
def billing_update(bill_id):
    allowed = {"penalty", "status", "due_date"}
    result = supabase.table("billing").update(clean_payload(body(), allowed)).eq("id", bill_id).execute()
    return ok(result.data[0] if result.data else {})


@app.post("/api/payments")
@api_guard
def payments_create():
    payload = body()
    bill_id = payload.get("billing_id")
    paid_amount = float(payload.get("paid_amount") or 0)
    if not bill_id or paid_amount <= 0:
        return fail("billing_id and positive paid_amount are required.")

    bill = supabase.table("billing").select("*").eq("id", bill_id).single().execute().data
    payment_payload = {
        "billing_id": bill_id,
        "customer_id": bill["customer_id"],
        "paid_amount": paid_amount,
        "payment_method": payload.get("payment_method", "cash"),
        "reference_number": payload.get("reference_number"),
        "paid_at": datetime.utcnow().isoformat(),
    }

    payment = supabase.table("payment_history").insert(payment_payload).execute().data[0]

    new_paid = float(bill.get("paid_amount") or 0) + paid_amount
    total = float(bill.get("total_amount") or 0)
    status = "paid" if new_paid >= total else "partial"

    supabase.table("billing").update({"paid_amount": new_paid, "status": status}).eq("id", bill_id).execute()
    return ok(payment, 201)


MAINT_FIELDS = {
    "customer_id", "issue_type", "description", "priority", "status",
    "assigned_employee_id", "resolved_at"
}


@app.get("/api/maintenance")
@api_guard
def maintenance_list():
    rows = supabase.table("maintenance_requests").select("*, customers(name), employees(name)").order("requested_at", desc=True).execute().data or []
    for r in rows:
        r["customer_name"] = (r.get("customers") or {}).get("name")
        r["assigned_employee_name"] = (r.get("employees") or {}).get("name")
        r.pop("customers", None)
        r.pop("employees", None)
    return ok(rows)


@app.post("/api/maintenance")
@api_guard
def maintenance_create():
    payload = clean_payload(body(), MAINT_FIELDS)
    result = supabase.table("maintenance_requests").insert(payload).execute()
    return ok(result.data[0], 201)


@app.put("/api/maintenance/<request_id>")
@api_guard
def maintenance_update(request_id):
    payload = clean_payload(body(), MAINT_FIELDS)
    if payload.get("status") in ["resolved", "closed"] and not payload.get("resolved_at"):
        payload["resolved_at"] = datetime.utcnow().isoformat()
    result = supabase.table("maintenance_requests").update(payload).eq("id", request_id).execute()
    return ok(result.data[0] if result.data else {})


@app.delete("/api/maintenance/<request_id>")
@api_guard
def maintenance_delete(request_id):
    supabase.table("maintenance_requests").delete().eq("id", request_id).execute()
    return ok()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
