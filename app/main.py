"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import json
from calendar import monthrange
from bson import json_util
from datetime import datetime, timezone, timedelta
from typing import Optional
from pymongo.errors import DuplicateKeyError
from fastapi import FastAPI, HTTPException, Query

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
import re
from datetime import date
from pymongo import MongoClient
from decimal import Decimal, ROUND_HALF_UP


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    """Create indexes required by the API. Safe to run repeatedly."""
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1), ("status", 1)],
        name="ix_attendance_emp_date_status",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
    )

    elapsed_seconds = int((punch_in - start).total_seconds())

    # More than 10 minutes late: count whole minutes since shift start.
    if elapsed_seconds > 10 * 60:
        return elapsed_seconds // 60

    return 0



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))

# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def validate_month(month: str) -> tuple[str, str, str]:
    """Return month, first date, and last date for YYYY-MM."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))
    last_day = monthrange(year, month_number)[1]

    return (
        month,
        f"{year:04d}-{month_number:02d}-01",
        f"{year:04d}-{month_number:02d}-{last_day:02d}",
    )


def round_half_up(value, places: int = 2):
    """Round a number using the assignment's half-up rule."""
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)

    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def working_days_pipeline_expression(start_date, end_date):
    """
    MongoDB expression that counts Monday-Friday dates in an inclusive
    period, starting no earlier than the employee's joining date.

    start_date and end_date are YYYY-MM-DD strings.
    """
    return {
        "$size": {
            "$filter": {
                "input": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": start_date
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateAdd": {
                                                        "startDate": {
                                                            "$dateFromString": {
                                                                "dateString": end_date
                                                            }
                                                        },
                                                        "unit": "day",
                                                        "amount": 1,
                                                    }
                                                },
                                                "unit": "day",
                                            }
                                        },
                                        0,
                                    ]
                                },
                            ]
                        },
                        "as": "day_offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": start_date
                                    }
                                },
                                "unit": "day",
                                "amount": "$$day_offset",
                            }
                        },
                    }
                },
                "as": "calendar_day",
                "cond": {
                    "$and": [
                        {
                            "$gte": [
                                "$$calendar_day",
                                {
                                    "$dateFromString": {
                                        "dateString": "$joined_on"
                                    }
                                },
                            ]
                        },
                        {
                            "$lte": [
                                "$$calendar_day",
                                {
                                    "$dateFromString": {
                                        "dateString": end_date
                                    }
                                },
                            ]
                        },
                        {
                            "$in": [
                                {"$dayOfWeek": "$$calendar_day"},
                                [2, 3, 4, 5, 6],
                            ]
                        },
                    ]
                },
            }
        }
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must be in HH:MM format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_end(cls, value: str, info) -> str:
        start = info.data.get("shift_start")
        if start is not None and value == start:
            raise ValueError("Shift start and end must differ")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError("punched_at must be an integer epoch timestamp in milliseconds")

        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("punched_at is outside the permitted timestamp range")

        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in {"PRESENT", "WFH", "ON_DUTY"}:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError(
                "punched_at must be an integer epoch timestamp in milliseconds"
            )

        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("punched_at is outside the permitted timestamp range")

        return value


class AttendancePatchIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    by: str
    reason: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        if value is None:
            return value

        if type(value) is not int:
            raise ValueError("Punch timestamps must be integer epoch milliseconds")

        if not 100000000000 <= value <= 4102444800000:
            raise ValueError("Timestamp is outside the permitted range")

        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("by", "reason")
    @classmethod
    def validate_required_text(cls, value):
        if not value.strip():
            raise ValueError("This field cannot be empty")
        return value




# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department:
        query["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )
    else:
        ts = datetime.now(timezone.utc)

    # Truncate to whole seconds, as required by the contract.
    ts = ts.replace(microsecond=0)

    # Attendance date is determined using Indian Standard Time.
    ist = timezone(timedelta(hours=5, minutes=30))
    local_ts = ts.astimezone(ist)
    date_str = local_ts.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            local_ts,
            emp["shift_start"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    # Return instants as epoch milliseconds.
    doc["punch_in"] = int(ts.timestamp() * 1000)

    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)

    # Find the most recent punch-in that occurred at or before punch-out.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No eligible punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out",
        )

    punch_in_time = record["punch_in"]

    # MongoDB normally returns naive UTC datetimes.
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=timezone.utc)

    duration_seconds = (ts - punch_in_time).total_seconds()

    if duration_seconds <= 0 or duration_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = float((Decimal(str(duration_seconds)) / Decimal("3600")).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP,))
    ist = timezone(timedelta(hours=5, minutes=30))
    local_out = ts.astimezone(ist)

    shift_end_hour, shift_end_minute = map(
        int, emp["shift_end"].split(":")
    )

    shift_end = datetime.fromisoformat(record["date"]).replace(
        hour=shift_end_hour,
        minute=shift_end_minute,
        tzinfo=ist,
    )

    shift_start_hour, shift_start_minute = map(
        int, emp["shift_start"].split(":")
    )

    if emp["shift_end"] <= emp["shift_start"]:
        shift_end += timedelta(days=1)

    overtime_seconds = (local_out - shift_end).total_seconds()
    overtime_minutes = (
        int(overtime_seconds // 60)
        if overtime_seconds >= 30 * 60
        else 0
    )

    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": work_hours < 4.50,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {"$set": update},
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already updated",
        )

    record.update(update)
    record["id"] = str(record.pop("_id"))
    record["punch_in"] = int(punch_in_time.timestamp() * 1000)
    record["punch_out"] = int(ts.timestamp() * 1000)

    return record



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from.isoformat()

        if date_to:
            query["date"]["$lte"] = date_to.isoformat()

    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs.find(query, {"_id": 1, "emp_code": 1, "date": 1,
                                        "status": 1, "punch_in": 1, "punch_out": 1,
                                        "work_hours": 1, "late_minutes": 1,
                                        "overtime_minutes": 1, "half_day": 1,
                                        "history": 1})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    def epoch_ms(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    for doc in docs:
        doc["id"] = str(doc.pop("_id"))
        doc["punch_in"] = epoch_ms(doc.get("punch_in"))
        doc["punch_out"] = epoch_ms(doc.get("punch_out"))

        for entry in doc.get("history", []):
            if isinstance(entry.get("at"), datetime):
                entry["at"] = epoch_ms(entry["at"])
            changes = entry.get("changes", {})
            for field in ("punch_in", "punch_out"):
                field_change = changes.get(field, {})
                for key in ("old", "new"):
                    value = field_change.get(key)
                    if isinstance(value, datetime):
                        field_change[key] = epoch_ms(value)

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: AttendancePatchIn,
):
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be a valid YYYY-MM-DD date",
        )

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    changes = {}
    updates = {}

    supplied = body.model_dump(exclude_unset=True)

    for field in ("status", "punch_in", "punch_out"):
        if field not in supplied:
            continue

        value = supplied[field]

        if field in ("punch_in", "punch_out"):
            value = (
                datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                .replace(microsecond=0)
                if value is not None else None
            )

        old_value = record.get(field)
        if old_value != value:
            changes[field] = {
                "old": old_value,
                "new": value,
            }
            updates[field] = value

    if not changes:
        raise HTTPException(status_code=422, detail="No changes supplied")

    new_status = updates.get("status", record["status"])
    new_punch_in = updates.get("punch_in", record.get("punch_in"))
    new_punch_out = updates.get("punch_out", record.get("punch_out"))

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied and supplied["punch_in"] is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times",
            )
        if "punch_out" in supplied and supplied["punch_out"] is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times",
            )

        new_punch_in = None
        new_punch_out = None

        # Record the punch-time changes in the audit history too.
        for field in ("punch_in", "punch_out"):
            old_value = record.get(field)
            if old_value is not None:
                changes[field] = {
                    "old": old_value,
                    "new": None,
                }

        updates["punch_in"] = None
        updates["punch_out"] = None

        # Reset derived fields because there is no attendance duration.
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False

    else:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence statuses require punch_in",
            )

        ist = timezone(timedelta(hours=5, minutes=30))
        local_in = new_punch_in.astimezone(ist)

        if local_in.date().isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date in IST",
            )

        if new_punch_out is not None:
            duration = (new_punch_out - new_punch_in).total_seconds()
            if duration <= 0 or duration > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

        updates["late_minutes"] = compute_late_minutes(
            local_in, emp["shift_start"]
        )

        if new_punch_out is None:
            updates["work_hours"] = None
            updates["overtime_minutes"] = 0
            updates["half_day"] = False
        else:
            duration = (new_punch_out - new_punch_in).total_seconds()
            work_hours = float(
                (Decimal(str(duration)) / Decimal("3600")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
            updates["work_hours"] = work_hours
            updates["half_day"] = work_hours < 4.50

            shift_end = datetime.fromisoformat(date).replace(
                hour=int(emp["shift_end"][:2]),
                minute=int(emp["shift_end"][3:]),
                tzinfo=ist,
            )
            if emp["shift_end"] <= emp["shift_start"]:
                shift_end += timedelta(days=1)

            overtime_seconds = (
                new_punch_out.astimezone(ist) - shift_end
            ).total_seconds()
            updates["overtime_minutes"] = (
                int(overtime_seconds // 60)
                if overtime_seconds >= 1800 else 0
            )

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.by,
        "reason": body.reason,
        "changes": changes,
    }

    updates["history"] = {"$push": history_entry}

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": {k: v for k, v in updates.items() if k != "history"},
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated.get("history", []):
        if isinstance(entry.get("at"), datetime):
            at = entry["at"]
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            entry["at"] = int(at.timestamp() * 1000)

        for field in ("punch_in", "punch_out"):
            for key in ("old", "new"):
                value = entry.get("changes", {}).get(field, {}).get(key)
                if isinstance(value, datetime):
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=timezone.utc)
                    entry["changes"][field][key] = int(value.timestamp() * 1000)

    return updated

# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    month, first_date, last_date = validate_month(month)

    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": first_date,
                                "$lte": last_date,
                            }
                        }
                    },
                    {
                        "$match": {
                            "$expr": {
                                "$eq": [
                                    "$emp_code",
                                    "$$employee_code",
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_attendance_day": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": "$_attendance_day"},
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "joined_on": 1,
                "working_days": working_days_pipeline_expression(
                    first_date,
                    last_date,
                ),
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    PRESENCE_STATUSES,
                                                ]
                                            },
                                            "$$log._is_weekday",
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            "$$log.half_day",
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$eq": [
                                    "$$log.status",
                                    "LEAVE",
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$$log.late_minutes",
                                            0,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$ifNull": [
                                    "$$log.late_minutes",
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$ifNull": [
                                    "$$log.overtime_minutes",
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
    ]

    result = list(db.employees.aggregate(pipeline))

    if not result:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    summary = result[0]

    working_days = summary["working_days"]
    present_days = summary["present_days"]

    attendance_pct = (
        round_half_up(
            Decimal(str(present_days))
            / Decimal(str(working_days))
            * Decimal("100"),
            2,
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary["leave_days"],
        "late_count": summary["late_count"],
        "total_late_minutes": summary["total_late_minutes"],
        "total_overtime_minutes": summary["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }

# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month, first_date, last_date = validate_month(month)

    employee_match = {
        "joined_on": {"$lte": last_date}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": first_date,
                                "$lte": last_date,
                            }
                        }
                    },
                    {
                        "$match": {
                            "$expr": {
                                "$eq": [
                                    "$emp_code",
                                    "$$employee_code",
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_attendance_day": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$in": [
                                    {"$dayOfWeek": "$_attendance_day"},
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "joined_on": 1,
                "working_days": working_days_pipeline_expression(
                    first_date,
                    last_date,
                ),
                "logs": 1,
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            PRESENCE_STATUSES,
                                        ]
                                    },
                                    "$logs._is_weekday",
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hour_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            PRESENCE_STATUSES,
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$logs.late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employee_codes"},
                "present_days": 1,
                "avg_work_hours": {
                    "$avg": "$work_hour_values"
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = round_half_up(
            item["avg_work_hours"], 2
        )

    return {
        "month": month,
        "items": items,
    }

# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month, first_date, last_date = validate_month(month)

    pipeline = [
        # 1. Select attendance records in the requested month.
        {
            "$match": {
                "date": {
                    "$gte": first_date,
                    "$lte": last_date,
                }
            }
        },

        # 2. Calculate monthly late minutes and late-day count per employee.
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },

        # 3. Employees with zero late minutes are not on the leaderboard.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },

        # 4. Join employee details so we can return name and department.
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},

        # 5. If requested, restrict the competition to one department.
        *(
            [{"$match": {"employee.department": department}}]
            if department is not None
            else []
        ),

        # 6. Rank using MongoDB's competition ranking.
        # Equal totals receive equal ranks; the next rank is skipped.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },

        # 7. Apply the limit AFTER ranking so ties at the limit are retained.
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },

        # 8. Sort the final output deterministically.
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },

        # 9. Return only the fields required by the API contract.
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    from datetime import date, timedelta

    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must be valid YYYY-MM-DD dates",
        )

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    number_of_days = (end - start).days + 1

    if number_of_days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Return 404 if this department does not exist.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "date": "$logs.date",
                    "emp_code": "$emp_code",
                    "joined_on": "$joined_on",
                },
                "status": {"$first": "$logs.status"},
                "half_day": {"$first": "$logs.half_day"},
                "late_minutes": {"$first": "$logs.late_minutes"},
            }
        },
        {
            "$group": {
                "_id": "$_id.date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    # Run the aggregation for recorded attendance dates.
    daily_records = list(db.employees.aggregate(pipeline))

    # Convert the returned records into a date lookup.
    records_by_date = {
        item["date"]: item for item in daily_records if item.get("date")
    }

    # Build the calendar response, including days without attendance logs.
    items = []
    rates = []

    current = start

    while current <= end:
        day_string = current.isoformat()
        is_working_day = current.weekday() < 5

        headcount = db.employees.count_documents(
            {
                "department": department,
                "joined_on": {"$lte": day_string},
            }
        )

        record = records_by_date.get(day_string, {})
        present_count = record.get("present_count", 0)
        late_count = record.get("late_count", 0)

        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(
                present_count / headcount, 4
            )
        else:
            attendance_rate = None

        rates.append(attendance_rate)

        recent_rates = [
            value for value in rates[-7:] if value is not None
        ]

        moving_avg_7d = (
            round_half_up(
                sum(recent_rates) / len(recent_rates), 4
            )
            if recent_rates
            else None
        )

        items.append(
            {
                "date": day_string,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": attendance_rate,
                "moving_avg_7d": moving_avg_7d,
            }
        )

        current += timedelta(days=1)

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(allowed_endpoints))}",
        )

    pipeline = []
    collection_name = "attendance_logs"

    if endpoint == "attendance_list":
        match = {}

        if emp_code:
            match["emp_code"] = emp_code
        if from_date:
            match["date"] = {"$gte": from_date}
        if to_date:
            match.setdefault("date", {})["$lte"] = to_date

        pipeline = [{"$match": match}] if match else [{"$match": {}}]

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="employee_monthly requires emp_code and month=YYYY-MM",
            )

        month, first_date, last_date = validate_month(month)
        collection_name = "employees"

        pipeline = [
            {"$match": {"emp_code": emp_code}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", first_date]},
                                        {"$lte": ["$date", last_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "monthly_logs",
                }
            },
        ]

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="department_summary requires month=YYYY-MM",
            )

        month, first_date, last_date = validate_month(month)
        collection_name = "employees"

        employee_match = {"joined_on": {"$lte": last_date}}
        if department:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", first_date]},
                                        {"$lte": ["$date", last_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "monthly_logs",
                }
            },
        ]

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="late_leaderboard requires month=YYYY-MM",
            )

        month, first_date, last_date = validate_month(month)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_date, "$lte": last_date}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
        ]

        if department:
            pipeline.extend([
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
            ])

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department_trend requires department, from, and to",
            )

        collection_name = "employees"

        pipeline = [
            {
                "$match": {
                    "department": department,
                    "joined_on": {"$lte": to_date},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", from_date]},
                                        {"$lte": ["$date", to_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]

    try:
        explain_result = db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": collection_name,
            "explain": json.loads(json_util.dumps(explain_result)),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not generate explain plan: {str(exc)}",
        )