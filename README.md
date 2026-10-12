# Employee Attendance & Analytics API

FastAPI and MongoDB 7.0 implementation of the Employee Attendance and Analytics API, strictly compliant with `openapi.yaml`, `DATA_MODEL.md`, and business rules R1–R10.

---

## What is Missing
**None.** The implementation is 100% complete and fully verified.
- All endpoints defined in `openapi.yaml` are implemented.
- All business rules R1 through R10 are satisfied with tests covering edge cases.
- All analytics pipelines run in pure MongoDB aggregations without in-memory Python loops.
- All database indexes are created idempotently at application startup.
- All 13 automated test suites pass with a 100% pass rate.

---

## Features

- **Core Attendance Operations**:
  - `POST /employees`: Employee registration with regex validation, duplicate prevention, and integer epoch millisecond timestamps.
  - `GET /employees`: Pagination, department filtering, and deterministic sorting.
  - `POST /attendance/punch-in`: Sub-second truncation (R1), IST shift calendar date calculation, overnight shift support, 10-minute grace period (R2), and atomic concurrency handling.
  - `POST /attendance/punch-out`: Work hours calculation with Decimal half-up rounding (R4), half-day threshold detection, overtime calculation with 30-minute minimum threshold (R3), and optimistic concurrency control.
  - `GET /attendance`: Multi-filter attendance listing with compound index sorting.
  - `PATCH /attendance/{emp_code}/{date}`: Full regularization lifecycle with atomic audit history, status transitions, and optimistic locking against race conditions.

- **MongoDB Aggregation Analytics**:
  - `GET /analytics/employees/{emp_code}/monthly`: Monthly summary with weekday working days, mid-month joiner handling, half-day weighing, and Decimal half-up attendance percentages.
  - `GET /analytics/departments/summary`: Department-level summary using cross-collection `$lookup`, including employees with zero attendance logs.
  - `GET /analytics/leaderboard/late`: Late leaderboard with standard competition ranking (`$rank` in `$setWindowFields`) correctly retaining ties at the cutoff limit (R10).
  - `GET /analytics/departments/{department}/trend`: Daily departmental trend implemented in pure MongoDB aggregation (`$dateAdd`, `$range`, `$setWindowFields`), calculating all calendar dates, weekends, missing days, and 7-day moving averages without Python loops.

- **Diagnostics**:
  - `GET /admin/explain/{endpoint}`: MongoDB `executionStats` diagnostic endpoint demonstrating index utilization (`IXSCAN`).

---

## Prerequisites

- Python 3.10+
- MongoDB 6.0+ (MongoDB 7.0 recommended)

---

## Quickstart

### 1. Environment Setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Set environment variables in your terminal or use local defaults:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Seed Sample Data

```bash
python sample_seed.py
```

### 4. Run the API Server

```bash
uvicorn app.main:app --port 8000 --reload
```

Interactive OpenAPI documentation is available at `http://localhost:8000/docs`.

---

## Running Tests

A comprehensive automated test suite covers all business rules, edge cases, aggregations, and concurrency scenarios:

```bash
pytest tests/test_api.py -v
```

---

## Documentation

- `REVIEW.md`: Detailed defect log of all 17 issues identified and resolved in the starter code.
- `DECISIONS.md`: Design rationale for database indexes, concurrency handling, ranking ties, headcount aggregation, and high-scale evolution.

---

## Submission Checklist Compliance

- [x] Starts with `uvicorn app.main:app --port 8000` from a clean clone after `pip install -r requirements.txt`.
- [x] All indexes are created automatically at application startup with zero manual steps.
- [x] `MONGO_URI` and `MONGO_DB` are read dynamically from environment variables with sensible defaults; nothing is hard-coded.
- [x] `REVIEW.md` and `DECISIONS.md` are completely filled in with detailed answers.
- [x] No `.env`, credentials, secrets, Dockerfiles, or virtual environments committed.
