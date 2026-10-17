# Production-reader baseline: failed attempt 1

This synthetic PostgreSQL attempt used the actual production model selection:
Astra/high for chat and Sol with the existing formation settings. It retained
12 of the planned 20 development observations before an unsettled formation
call stopped the paired bank. All twelve completed sparse observations failed
answer success; these are preserved failures, not activation evidence.

Reported model spend was USD 0.75479875. The failed call retains its full USD
2.19152 reservation, so USD 2.94631875 counts against the approved USD 100 cap.
The original journal did not retain the exception class for that call; the
outer bank reported ValueError. Its exact underlying cause is unconfirmed.
No cost was assumed to be zero and no observation was rewritten.

The retained budget allocation permits one complete 20-observation baseline
retry capped at USD 5 and confirmation capped at USD 92. Together with this
attempt's reported and uncertain spend, the maximum is USD 99.94631875.
The retry covers the original full development set, not selected failures.
Its cost and latency ceiling rule is fixed before that retry's measurements.

The baseline was development evidence on the recorded implementation digest,
not a clean-build confirmatory collection. The corpus holdout was not executed.
