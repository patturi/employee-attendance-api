"""
Employee Attendance & Analytics API - Complete Implementation

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Literal, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ensure_indexes():
    """Create all required indexes idempotently at application startup."""
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")

    # attendance_logs indexes
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_attendance_emp_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_desc_emp_asc"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_emp_punch_in"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("late_minutes", ASCENDING)],
        name="idx_attendance_date_status_late"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="idx_attendance_status_date"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rule Math
# --------------------------------------------------------------------------- #
def round_half_up(val: float, decimals: int = 2) -> float:
    """Half-up rounding per R4, R8."""
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def validate_epoch_ms(val: Any) -> int:
    """Validate instant integer epoch milliseconds (reject floats, strings, seconds)."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise HTTPException(status_code=422, detail="Epoch millisecond must be an integer")
    if val < 100000000000 or val > 4102444800000:
        raise HTTPException(status_code=422, detail="Epoch millisecond out of range or looks like seconds")
    return val


def epoch_to_utc_dt(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    truncated_sec = epoch_ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def utc_dt_to_epoch(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    sec = int(dt.timestamp())
    return sec * 1000


def parse_hh_mm(s: str) -> dt_time:
    parts = s.split(":")
    return dt_time(hour=int(parts[0]), minute=int(parts[1]))


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception: overnight shifts (shift_end <= shift_start):
    a punch-in earlier than shift_end belongs to the shift that started previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_time = parse_hh_mm(shift_start)
    end_time = parse_hh_mm(shift_end)

    if end_time <= start_time:
        # Overnight shift
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2 Late. Grace period is 10 minutes.
    A punch-in is late only if strictly more than 10:00 (600 seconds) after shift_start.
    When late, whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    att_date = dt_date.fromisoformat(attendance_date_str)
    start_time = parse_hh_mm(shift_start)
    shift_start_ist = datetime.combine(att_date, start_time, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST)

    elapsed_seconds = (punch_in_ist - shift_start_ist).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_overtime_minutes(
    punch_out_utc: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date_str: str
) -> int:
    """
    R3 Overtime. Whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on next calendar day.
    """
    att_date = dt_date.fromisoformat(attendance_date_str)
    start_time = parse_hh_mm(shift_start)
    end_time = parse_hh_mm(shift_end)

    if end_time <= start_time:
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_ist = datetime.combine(shift_end_date, end_time, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)

    elapsed_seconds = (punch_out_ist - shift_end_ist).total_seconds()
    if elapsed_seconds < 0:
        return 0
    whole_minutes = int(elapsed_seconds // 60)
    return whole_minutes if whole_minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours. (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up.
    """
    total_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = total_sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5 Half day. work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for h in history:
        entry = {
            "at": utc_dt_to_epoch(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        changes = h.get("changes", {})
        for field, diff in changes.items():
            f_val = diff.get("from")
            t_val = diff.get("to")
            if field in ("punch_in", "punch_out"):
                f_val = utc_dt_to_epoch(f_val) if isinstance(f_val, datetime) else f_val
                t_val = utc_dt_to_epoch(t_val) if isinstance(t_val, datetime) else t_val
            entry["changes"][field] = {"from": f_val, "to": t_val}
        out.append(entry)
    return out


def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_epoch(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": utc_dt_to_epoch(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_CODE_REGEX = re.compile(r"^EMP\d{4,6}$")


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email_pattern(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            validate_epoch_ms(v)
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            validate_epoch_ms(v)
        return v


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_instants(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            validate_epoch_ms(v)
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=UTC).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_to_utc_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(tz=UTC).replace(microsecond=0)

    attendance_date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for that date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = epoch_to_utc_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(tz=UTC).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punch_out
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    diff_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime_minutes(punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"])
    half = compute_half_day(work_hrs)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100)
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    # Determine requested changes
    req_status = body.status if body.status is not None else rec["status"]
    req_punch_in = epoch_to_utc_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    req_punch_out = epoch_to_utc_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")

    if req_punch_in and req_punch_in.tzinfo is None:
        req_punch_in = req_punch_in.replace(tzinfo=UTC)
    if req_punch_out and req_punch_out.tzinfo is None:
        req_punch_out = req_punch_out.replace(tzinfo=UTC)

    # Validation rules
    if req_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out with ABSENT/LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Presence status requires punch_in
        if req_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        # punch_in must stay on record's attendance date under R1
        derived_date = determine_attendance_date(req_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to this attendance date")

        final_punch_in = req_punch_in
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)

        if req_punch_out is not None:
            diff_sec = (req_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
            final_punch_out = req_punch_out
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime_minutes(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_punch_out = None
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Compare changes
    db_changes = {}
    new_doc_fields = {
        "status": req_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    fields_to_check = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    for f in fields_to_check:
        old_val = rec.get(f)
        new_val = new_doc_fields[f]
        if f in ("punch_in", "punch_out"):
            if old_val and old_val.tzinfo is None:
                old_val = old_val.replace(tzinfo=UTC)
            if new_val and new_val.tzinfo is None:
                new_val = new_val.replace(tzinfo=UTC)
        if old_val != new_val:
            db_changes[f] = {"from": old_val, "to": new_val}

    if not db_changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": datetime.now(tz=UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": db_changes,
    }

    # Optimistic concurrency: ensure history matches so concurrent updates don't overwrite
    current_history = rec.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": current_history
        },
        {
            "$set": new_doc_fields,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Conflict: record was modified concurrently")

    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    m_start = dt_date(year, m_num, 1)
    m_end = dt_date(year, m_num, num_days)

    joined_date = dt_date.fromisoformat(emp["joined_on"])
    effective_start = max(m_start, joined_date)

    working_days = 0
    if effective_start <= m_end:
        cur = effective_start
        while cur <= m_end:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        }
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    emp_match = {"joined_on": {"$lte": end_str}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ecode": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$ecode"]},
                                    {"$gte": ["$date", start_str]},
                                    {"$lte": ["$date", end_str]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": ["$$l.day_of_week", [2, 3, 4, 5, 6]]}
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "work_hours_array": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]}
                            ]
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$flat_logs.late_minutes"
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_array.work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(float(avg_wh), 2)
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None
):
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    match_filter = {
        "date": {"$gte": start_str, "$lte": end_str},
        "late_minutes": {"$gt": 0}
    }

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp[" + "department" + "]": department} if False else {"$match": {"emp.department": department}}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    f_date = dt_date.fromisoformat(from_date)
    t_date = dt_date.fromisoformat(to_date)
    if t_date < f_date:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    delta_days = (t_date - f_date).days + 1
    if delta_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime.combine(f_date, dt_time(0, 0), tzinfo=UTC)
    to_dt = datetime.combine(t_date, dt_time(0, 0), tzinfo=UTC)

    # Pure MongoDB aggregation pipeline with date densification / sequence generation
    pipeline = [
        {"$limit": 1},
        {
            "$project": {
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {"$add": [{"$dateDiff": {"startDate": from_dt, "endDate": to_dt, "unit": "day"}}, 1]}
                            ]
                        },
                        "as": "d",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$d"}}
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$dates"},
        {"$project": {"_id": 0, "date": "$dates"}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        # Lookup headcount in department with joined_on <= date
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc_res"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$first": "$hc_res.count"}, 0]
                }
            }
        },
        # Lookup attendance records for this department on this date
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {"emp.department": department}
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]

    docs = list(db.employees.aggregate(pipeline))
    items = []
    for d in docs:
        att_rate = d.get("attendance_rate")
        if att_rate is not None:
            att_rate = round_half_up(float(att_rate), 4)

        m_avg = d.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(float(m_avg), 4)

        items.append({
            "date": d["date"],
            "is_working_day": bool(d["is_working_day"]),
            "headcount": int(d["headcount"]),
            "present_count": float(d["present_count"]),
            "late_count": int(d["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        explain_res = db.command("explain", find_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        start_str = f"{month}-01"
        end_str = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_str, "$lte": end_str}}},
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": 1},
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        end_str = f"{month}-{num_days:02d}"
        emp_match = {"joined_on": {"$lte": end_str}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
        ]
        explain_res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        start_str = f"{month}-01"
        end_str = f"{month}-{num_days:02d}"
        pipeline = [
            {"$match": {"date": {"$gte": start_str, "$lte": end_str}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}
        ]
        explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        f_date = dt_date.fromisoformat(from_date)
        t_date = dt_date.fromisoformat(to_date)
        if t_date < f_date:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        from_dt = datetime.combine(f_date, dt_time(0, 0), tzinfo=UTC)
        to_dt = datetime.combine(t_date, dt_time(0, 0), tzinfo=UTC)
        pipeline = [
            {"$limit": 1},
            {
                "$project": {
                    "dates": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    {"$add": [{"$dateDiff": {"startDate": from_dt, "endDate": to_dt, "unit": "day"}}, 1]}
                                ]
                            },
                            "as": "d",
                            "in": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$d"}}
                                }
                            }
                        }
                    }
                }
            }
        ]
        explain_res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_res}

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")
