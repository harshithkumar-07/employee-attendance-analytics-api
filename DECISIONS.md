# Design Decisions

**1. Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes, and a compound unique index on `attendance_logs.emp_code` and `date` to allow only one attendance record per employee per day. I also added indexes for date-based attendance queries and employee/date/status filtering. I would consider a department-and-joining-date index for faster department reports, but would confirm its benefit using execution statistics before adding it.

**2. Punch-in race.** If two identical requests arrive together, both might check for an existing record before either inserts. The unique compound index is the final safeguard: only one insert can succeed. The other request must be handled as a duplicate conflict. This depends on the code correctly handling MongoDB duplicate-key errors.

**3. Ties.** The leaderboard uses competition ranking. Employees with equal late-minute totals receive the same rank. If a tie crosses the requested limit, all employees sharing that cutoff rank should be returned.

**4. Headcount.** The department summary starts from eligible employee records and looks up attendance logs. Employees without logs therefore remain in the results and contribute to headcount.

**5. Scaling.** For 100 times more data, I would measure query plans and execution times, review index usage, and add automated tests for large datasets before optimizing expensive aggregations.