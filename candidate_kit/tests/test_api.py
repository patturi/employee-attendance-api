import os
import threading
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Configure before importing app
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, ensure_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_teardown():
    # Clear test db collections and recreate indexes
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    ensure_indexes()
    yield


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud_and_validation():
    # Invalid email
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Same shift_start and shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@corp.com",
        "department": "Engineering",
        "shift_start": "09:00",
        "shift_end": "09:00",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

    # Valid creation
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@corp.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)

    # Duplicate emp_code -> 409
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice Duplicate",
        "email": "alice2@corp.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 409

    # Listing with pagination
    client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Bob",
        "email": "bob@corp.com",
        "department": "Sales",
        "joined_on": "2026-02-01"
    })
    res = client.get("/employees?page=1&page_size=1")
    assert res.status_code == 200
    assert len(res.json()["items"]) == 1
    assert res.json()["total"] == 2
    assert res.json()["items"][0]["emp_code"] == "EMP0001"

    # Listing filter by department
    res = client.get("/employees?department=Sales")
    assert res.status_code == 200
    assert res.json()["total"] == 1
    assert res.json()["items"][0]["emp_code"] == "EMP0002"


def test_punch_in_and_late_grace_period():
    client.post("/employees", json={
        "emp_code": "EMP0010",
        "name": "Charlie",
        "email": "charlie@corp.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Exactly 09:40:00 IST (10 min exactly) -> 0 late minutes
    # 2026-07-01 09:40:00 IST = 2026-07-01 04:10:00 UTC = 1782879000000 ms
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0010",
        "punched_at": 1782879000000
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 0
    assert res.json()["date"] == "2026-07-01"

    # Duplicate punch in on same date -> 409
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0010",
        "punched_at": 1782879000000
    })
    assert res.status_code == 409

    # Next day: 09:40:01 IST (10m 1s elapsed) -> late by 10 minutes
    # 2026-07-02 09:40:01 IST = 2026-07-02 04:10:01 UTC = 1782965401000 ms
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0010",
        "punched_at": 1782965401000
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10
    assert res.json()["date"] == "2026-07-02"


def test_overnight_shift_punch_in_and_out():
    # Overnight shift: 22:00 to 06:00
    client.post("/employees", json={
        "emp_code": "EMP0020",
        "name": "Night Owl",
        "email": "owl@corp.com",
        "department": "Ops",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in on 2026-07-02 at 01:00:00 IST (< 06:00) -> belongs to 2026-07-01
    # 2026-07-01 19:30:00 UTC = 2026-07-02 01:00:00 IST = 1782934200000 ms
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0020",
        "punched_at": 1782934200000
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-01"

    # Punch out at 06:45:00 IST next day
    # 2026-07-02 01:15:00 UTC = 2026-07-02 06:45:00 IST = 1782954900000 ms
    # Worked from 01:00 to 06:45 = 5.75 hours
    # Shift ended at 06:00, punched out at 06:45 -> 45 minutes overtime
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0020",
        "punched_at": 1782954900000
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 5.75
    assert data["overtime_minutes"] == 45
    assert data["half_day"] is False

    # Second punch out -> 409
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0020",
        "punched_at": 1782954900000
    })
    assert res.status_code == 409


def test_work_hours_half_up_and_half_day_threshold():
    client.post("/employees", json={
        "emp_code": "EMP0030",
        "name": "Dan",
        "email": "dan@corp.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 09:30:00 IST (04:00 UTC) = 1782878400000 ms
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0030",
        "punched_at": 1782878400000
    })

    # Punch out after 4 hours 29 minutes 30 seconds = 16170 seconds = 4.491666... hours -> rounds half-up to 4.49 -> half_day = True
    # 04:00:00 UTC + 16170s = 08:29:30 UTC = 1782894570000 ms
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0030",
        "punched_at": 1782894570000
    })
    assert res.status_code == 200
    assert res.json()["work_hours"] == 4.49
    assert res.json()["half_day"] is True


def test_regularization_patch():
    client.post("/employees", json={
        "emp_code": "EMP0040",
        "name": "Eve",
        "email": "eve@corp.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Punch in at 09:30 IST on 2026-07-01 = 1782878400000 ms
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0040",
        "punched_at": 1782878400000
    })

    # Regularize to LEAVE (cannot provide punch_in/punch_out)
    res = client.patch("/attendance/EMP0040/2026-07-01", json={
        "status": "LEAVE",
        "reason": "Applied sick leave",
        "regularized_by": "Manager1"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "Manager1"
    assert "status" in data["history"][0]["changes"]

    # Regularize back to PRESENT with punch_in and punch_out
    res = client.patch("/attendance/EMP0040/2026-07-01", json={
        "status": "PRESENT",
        "punch_in": 1782878400000,
        "punch_out": 1782910800000,  # 9 hours later = 18:30 IST
        "reason": "Corrected record",
        "regularized_by": "Manager2"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "PRESENT"
    assert data["work_hours"] == 9.0
    assert len(data["history"]) == 2

    # Attempting to regularize with no changes -> 422
    res = client.patch("/attendance/EMP0040/2026-07-01", json={
        "status": "PRESENT",
        "punch_in": 1782878400000,
        "punch_out": 1782910800000,
        "reason": "No changes at all",
        "regularized_by": "Manager2"
    })
    assert res.status_code == 422


def test_concurrency_punch_in():
    client.post("/employees", json={
        "emp_code": "EMP0050",
        "name": "Concurrent Guy",
        "email": "concurrent@corp.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    results = []

    def make_punch():
        res = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0050",
            "punched_at": 1782878400000
        })
        results.append(res.status_code)

    t1 = threading.Thread(target=make_punch)
    t2 = threading.Thread(target=make_punch)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [201, 409]


def test_leaderboard_competition_ranking_with_ties():
    # Create 4 employees
    for i in range(1, 5):
        client.post("/employees", json={
            "emp_code": f"EMP010{i}",
            "name": f"Late Person {i}",
            "email": f"late{i}@corp.com",
            "department": "Support",
            "joined_on": "2026-01-01"
        })

    # EMP0101: 50 min late
    # EMP0102: 30 min late
    # EMP0103: 30 min late (tie with EMP0102)
    # EMP0104: 10 min late
    for code, late in [("EMP0101", 50), ("EMP0102", 30), ("EMP0103", 30), ("EMP0104", 10)]:
        db.attendance_logs.insert_one({
            "emp_code": code,
            "date": "2026-07-01",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

    # Leaderboard with limit=2: EMP0101 is rank 1, EMP0102 and EMP0103 tie for rank 2.
    # Since rank 2 <= limit 2, BOTH tied employees must be included!
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP0101"
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2


def test_department_trend_mongodb_aggregation():
    client.post("/employees", json={
        "emp_code": "EMP0201",
        "name": "Trend Person",
        "email": "trend@corp.com",
        "department": "Analytics",
        "joined_on": "2026-07-01"
    })

    # Date range across 5 days: 2026-07-01 to 2026-07-05
    res = client.get("/analytics/departments/Analytics/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5
    dates = [item["date"] for item in items]
    assert dates == ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"]
    assert items[0]["headcount"] == 1
    assert items[3]["is_working_day"] is False  # 2026-07-04 is Saturday


def test_explain_endpoint():
    res = client.get("/admin/explain/attendance_list?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert "explain" in data

    # Explain all 5 endpoints
    for ep, params in [
        ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
        ("department_summary", "?month=2026-07&department=Engineering"),
        ("late_leaderboard", "?month=2026-07"),
        ("department_trend", "?department=Engineering&from=2026-07-01&to=2026-07-05"),
    ]:
        resp = client.get(f"/admin/explain/{ep}{params}")
        assert resp.status_code == 200, f"Explain failed for {ep}: {resp.text}"
        assert "explain" in resp.json()


def test_monthly_analytics_and_zero_working_days():
    # Joined on 2026-07-20 (late in the month)
    client.post("/employees", json={
        "emp_code": "EMP0301",
        "name": "Mid Month Joiner",
        "email": "mid@corp.com",
        "department": "Engineering",
        "joined_on": "2026-07-20"
    })

    # Record on 2026-07-21 (Tuesday) - PRESENT half day
    db.attendance_logs.insert_one({
        "emp_code": "EMP0301",
        "date": "2026-07-21",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
        "work_hours": 4.0,
        "late_minutes": 15,
        "overtime_minutes": 0,
        "half_day": True,
        "history": []
    })

    res = client.get("/analytics/employees/EMP0301/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0301"
    # Working days from July 20 to July 31 (weekdays): 20, 21, 22, 23, 24, 27, 28, 29, 30, 31 = 10 days
    assert data["working_days"] == 10
    assert data["present_days"] == 0.5  # half day = 0.5
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 15
    # 0.5 / 10 * 100 = 5.0%
    assert data["attendance_pct"] == 5.0

    # Test joining after month end -> working days = 0, attendance_pct = None
    res_past = client.get("/analytics/employees/EMP0301/monthly?month=2026-06")
    assert res_past.status_code == 200
    assert res_past.json()["working_days"] == 0
    assert res_past.json()["attendance_pct"] is None


def test_department_summary_includes_zero_log_employees():
    # 2 employees in Design department
    client.post("/employees", json={
        "emp_code": "EMP0401",
        "name": "Designer 1",
        "email": "des1@corp.com",
        "department": "Design",
        "joined_on": "2026-07-01"
    })
    client.post("/employees", json={
        "emp_code": "EMP0402",
        "name": "Designer 2",
        "email": "des2@corp.com",
        "department": "Design",
        "joined_on": "2026-07-01"
    })

    # Only Designer 1 has an attendance log
    db.attendance_logs.insert_one({
        "emp_code": "EMP0401",
        "date": "2026-07-01",  # Wednesday
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 30,
        "half_day": False,
        "history": []
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=Design")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    design_stat = items[0]
    assert design_stat["department"] == "Design"
    # Headcount MUST include Designer 2 even with zero logs
    assert design_stat["headcount"] == 2
    assert design_stat["present_days"] == 1.0
    assert design_stat["avg_work_hours"] == 9.0


def test_concurrency_punch_out():
    client.post("/employees", json={
        "emp_code": "EMP0501",
        "name": "Punch Out Race",
        "email": "porace@corp.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    # Punch in first
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0501",
        "punched_at": 1782878400000
    })

    results = []

    def make_punch_out():
        res = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0501",
            "punched_at": 1782910800000
        })
        results.append(res.status_code)

    t1 = threading.Thread(target=make_punch_out)
    t2 = threading.Thread(target=make_punch_out)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One succeeds (200), one conflicts (409)
    assert sorted(results) == [200, 409]

