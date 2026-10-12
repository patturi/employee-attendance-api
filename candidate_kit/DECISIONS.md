# DECISIONS.md

1. **Indexes.**
   - `employees`: `{emp_code: 1}` (unique constraint and instant lookup); `{department: 1, joined_on: 1}` (supports department filtering and join-date range queries).
   - `attendance_logs`: `{emp_code: 1, date: 1}` (unique constraint guaranteeing exactly one record per employee per day); `{date: -1, emp_code: 1}` (serves attendance listing sort order directly without memory sort); `{emp_code: 1, punch_in: -1}` (accelerates punch-out record matching); `{date: 1, status: 1, late_minutes: 1}` (optimizes analytics date ranges and late-ranking filters).
   - *Rejected:* Single-field index on `status` alone, because its low cardinality causes poor index selectivity compared to compound date-bounded indexes.

2. **Punch-in race.**
   - When two identical requests arrive concurrently, both resolve the employee and compute the IST shift date. Both execute `insert_one()`. At the WiredTiger storage layer, the unique compound index `{emp_code: 1, date: 1}` allows only the first transaction to commit (returning 201). The second transaction fails immediately with a MongoDB `DuplicateKeyError`, caught cleanly by our handler to return HTTP 409 Conflict.

3. **Ties.**
   - Per R10, we apply standard competition ranking using `$rank` in `$setWindowFields` (e.g., ranks 1, 2, 2, 4). The leaderboard filter applies `rank <= limit`. If two employees tie at the boundary (e.g., rank 2 with `limit=2`), both are returned, ensuring fair, deterministic reporting without arbitrary drop-offs.

4. **Headcount.**
   - The `department_summary` pipeline begins at the `employees` collection (matching `joined_on <= month_end`) and performs a `$lookup` into `attendance_logs`. Grouping by department calculates `headcount: {$sum: 1}` over the employee documents, ensuring employees with zero attendance logs are fully counted.

5. **One thing to change for 100x data.**
   - Pre-aggregate daily and monthly metrics incrementally using MongoDB Change Streams into a dedicated `monthly_attendance_rollups` collection, replacing real-time cross-collection `$lookup` joins with instant document lookups.
