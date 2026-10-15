# Code Review

This review records defects identified in the starter implementation, how they can be detected, and the changes made or required in the final implementation. The findings should be verified against the original starter code and the final code before submission.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Attendance punch-in | Duplicate attendance records or repeated punch-in operations must be prevented for the same employee and attendance date. | Submit two punch-in requests for the same employee and date. | Validate the existing attendance record and enforce uniqueness with a MongoDB compound index. |
| 2 | Attendance punch-out | Invalid punch-out timestamps can result in incorrect attendance data if their order or maximum permitted interval is not validated. | Try a punch-out before punch-in or more than 24 hours after punch-in. | Validate timestamp order and the permitted interval before updating the attendance record. |
| 3 | Attendance timestamp helpers | Converting between epoch milliseconds and BSON dates can produce inconsistent results if UTC handling is not consistent. | Test timestamps near midnight and across date boundaries. | Use consistent UTC conversions and apply the configured business timezone when calculating attendance dates. |
| 4 | Manual attendance correction | Overwriting attendance values without preserving the previous state makes corrections difficult to audit. | Correct an existing attendance record and inspect its history. | Preserve the prior values and record correction details in the history field. |
| 5 | Monthly employee analytics | Working-day and attendance calculations can be incorrect if employee joining dates, half-days, leave, or non-working days are ignored. | Test an employee who joined mid-month, has a half-day, and has no attendance on some weekdays. | Calculate eligible working days and aggregate the relevant attendance statuses according to the API contract. |
| 6 | Department summary analytics | Department-level calculations can omit employees without attendance records or calculate averages using inappropriate values. | Test a department with employees who have no attendance logs or missing work-hours values. | Include eligible employees and calculate each metric using the contract's definitions. |
| 7 | Late-arrival leaderboard | Ranking can be incorrect if ties are broken into separate ranks or filtering is applied after ranking. | Create employees with equal late-minute totals and test the department filter and limit. | Apply filtering before ranking and use competition ranking so tied totals receive the same rank. |
| 8 | Department attendance trend | Dates without attendance records must still appear, and the attendance rate and rolling average must follow the specified rules. | Test a date range containing weekends and weekdays without attendance records. | The trend implementation needs MongoDB-based calendar gap-filling and contract-compliant calculations before this finding can be marked fixed. |
| 9 | Explain-plan endpoint | A successful explain request does not prove that the actual route query uses an appropriate index. | Inspect execution statistics and compare the explain pipeline with the route being evaluated. | Use the actual route query shape and verify index usage and execution statistics. |

## Items Reviewed but Not Considered Defects

- **MongoDB `_id` field:** MongoDB automatically adds this field to documents. Its presence is expected and is not itself a defect.
- **HTTP 404 for the root path:** The application is not required to define a `/` route if the documented health endpoint and API routes work.
- **HTTP 422 for invalid requests:** This is an expected validation response when a request violates the API's input requirements.
- **HTTP 409 for a conflicting attendance operation:** This is appropriate when the API contract defines the operation as a conflict, such as a duplicate punch-in.
- **A successful HTTP 200 response:** This confirms that a request was handled successfully, but it does not by itself establish correctness of calculations, business rules, or query performance.