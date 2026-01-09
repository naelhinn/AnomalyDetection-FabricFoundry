# Foundry output tables (Gold)

## gold_conveyor_findings
```sql
CREATE TABLE IF NOT EXISTS gold_conveyor_findings (
  run_id STRING,
  event_id STRING,
  predicted_label STRING,
  foreign_object_type STRING,
  severity INT,
  confidence DOUBLE,
  finding STRING,
  recommended_action STRING,
  evidence_frames_json STRING,
  created_at_utc TIMESTAMP
) USING DELTA;
```

## gold_eod_summary (optional)
```sql
CREATE TABLE IF NOT EXISTS gold_eod_summary (
  run_id STRING,
  summary STRING,
  top_anomalies_json STRING,
  telemetry_patterns_json STRING,
  actions_json STRING,
  created_at_utc TIMESTAMP
) USING DELTA;
```
