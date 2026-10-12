"""
Monitoring as code: builds the 4 Grafana dashboards and the alert rules from this one file.

  python monitoring/grafana/build.py          writes dashboards/*.json and provisioning/alerting/rules.yml
  python monitoring/grafana/build.py --check  CI: fails if the committed files differ from what this builds

Change a panel or an alert HERE, run the script, commit both. Grafana loads the files at start-up
(provisioning) and the UI cannot change them, so what runs is always what was reviewed.
Every SQL query in here is executed against a real Postgres by tests/monitoring.
"""
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
# =====================================================================================
# DASHBOARDS
# =====================================================================================
PG = {"type": "grafana-postgresql-datasource", "uid": "reporting"}
PROM = {"type": "prometheus", "uid": "prometheus"}
REPLAY_RANGE = {"from": "2019-09-15T00:00:00.000Z", "to": "2020-10-15T00:00:00.000Z"}
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def sql(raw, fmt="table", ref="A"):
    return {"refId": ref, "datasource": PG, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": raw.strip()}


def prom(expr, legend="", ref="A", instant=False):
    t = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend, "editorMode": "code"}
    if instant:
        t.update(instant=True, range=False)
    return t


def panel(kind, title, targets, x, y, w, h, desc="", unit=None, thresholds=None, overrides=None, options=None,
          custom=None, mappings=None, decimals=None, ds=PG, min_=None, max_=None):
    defaults = {"color": {"mode": "palette-classic" if kind in ("timeseries", "barchart") else "thresholds"},
                "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "green", "value": None}]}}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if custom:
        defaults["custom"] = custom
    if mappings:
        defaults["mappings"] = mappings
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return {"id": nid(), "type": kind, "title": title, "description": desc, "datasource": ds,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {}}


def row(title, y):
    return {"id": nid(), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def text(content, x, y, w, h, title=""):
    return {"id": nid(), "type": "text", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": content}}


STATUS_COLORS = [{"type": "value", "options": {
    "SUCCESS": {"color": "green", "index": 0}, "PASS": {"color": "green", "index": 1},
    "WARN": {"color": "orange", "index": 2}, "SKIPPED": {"color": "blue", "index": 3},
    "PENDING": {"color": "blue", "index": 4}, "FAILED": {"color": "red", "index": 5}, "FAIL": {"color": "red", "index": 6},
    "SENT": {"color": "green", "index": 7}, "READY": {"color": "blue", "index": 8}, "SENDING": {"color": "orange", "index": 9},
    "DRY_RUN": {"color": "text", "index": 10}}}]
COLOR_STATUS_CELL = [{"matcher": {"id": "byRegexp", "options": "status|gate_status"},
                      "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                     {"id": "mappings", "value": STATUS_COLORS}]}]
RED_IF_ABOVE_0 = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
STAT = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "background",
        "graphMode": "none", "textMode": "value"}
TS_LINES = {"drawStyle": "line", "lineWidth": 2, "pointSize": 6, "showPoints": "always", "spanNulls": True}
TS_LEGEND = {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]},
             "tooltip": {"mode": "multi", "sort": "desc"}}


def dashboard(uid, title, desc, panels, time_range, tags, refresh=""):
    return {"uid": uid, "title": title, "description": desc, "tags": ["nbo"] + tags, "timezone": "utc",
            "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1, "refresh": refresh,
            "time": time_range, "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
            "links": [{"title": "NBO dashboards", "type": "dashboards", "tags": ["nbo"], "asDropdown": True}]}


# =====================================================================================
# 1. PIPELINE HEALTH (wall-clock time: when did things run)
# =====================================================================================
_id[0] = 0
p = [
    text("**Is the pipeline running?** Airflow health from Prometheus, step results from Snowflake's NBO_RUN_LOG "
         "(copied to Postgres by `nbo_reporting_export`). Red = act now; every alert email links its fix.", 0, 0, 24, 2),
    panel("stat", "Airflow scheduler", [prom("nbo:scheduler_heartbeats:rate5m or vector(0)", instant=True)], 0, 2, 4, 4,
          "Heartbeats per second over 5 minutes. 0 = the scheduler is down: nothing gets scheduled.", ds=PROM,
          options=STAT, thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0.01}],
          mappings=[{"type": "range", "options": {"from": 0, "to": 0.0099, "result": {"text": "DOWN", "index": 0}}},
                    {"type": "range", "options": {"from": 0.01, "to": 1000, "result": {"text": "ALIVE", "index": 1}}}]),
    panel("stat", "DAG import errors", [prom("max(airflow_dag_processing_import_errors) or vector(0)", instant=True)],
          4, 2, 4, 4, "DAG files that fail to load. Above 0 = a DAG has disappeared from Airflow.", ds=PROM,
          options=STAT, thresholds=RED_IF_ABOVE_0),
    panel("stat", "Failed steps (24 h)", [sql("""
        SELECT count(*) AS failed FROM reporting.run_log
        WHERE status = 'FAILED' AND start_time > (now() AT TIME ZONE 'UTC') - interval '24 hours'""")], 8, 2, 4, 4,
          "Procedure runs with status FAILED in the last 24 hours (any DAG, any send date).", options=STAT,
          thresholds=RED_IF_ABOVE_0),
    panel("stat", "Last completed send", [sql("""
        SELECT max(run_date)::text AS last_send FROM reporting.run_log
        WHERE procedure_name = 'NBO_EVENTS_SENT' AND status = 'SUCCESS'""")], 12, 2, 4, 4,
          "Send date of the latest send that finished (emails recorded as sent).", options={**STAT, "colorMode": "none"}),
    panel("stat", "Emails stuck in SENDING", [sql("""
        SELECT coalesce(sum(emails), 0) AS stuck FROM reporting.outbox_status WHERE status = 'SENDING'""")],
          16, 2, 4, 4, "Claimed but never confirmed (Airflow died mid-send). Check the mail log before re-sending.",
          options=STAT, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
    panel("stat", "Dashboard data age", [sql("""
        SELECT extract(epoch FROM now() - max(exported_at)) AS age FROM reporting.export_log""")], 20, 2, 4, 4,
          "Time since the last Snowflake -> Postgres copy. Above 3 h the numbers here are stale.", unit="s",
          options=STAT, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 10800}]),
    row("Steps", 6),
    panel("table", "Latest result of each step", [sql("""
        SELECT DISTINCT ON (procedure_name) procedure_name AS step, run_date, status, start_time,
               duration_seconds AS seconds, code_version, left(error_message, 200) AS error
        FROM reporting.run_log ORDER BY procedure_name, start_time DESC""")], 0, 7, 24, 8,
          "One row per procedure: its most recent run. code_version = the git commit that ran.",
          overrides=COLOR_STATUS_CELL),
    panel("timeseries", "Step duration", [sql("""
        SELECT start_time AS time, procedure_name AS metric, duration_seconds AS value FROM reporting.run_log
        WHERE $__timeFilter(start_time) ORDER BY 1""", fmt="time_series")], 0, 15, 12, 8,
          "How long each procedure took per run. A step getting slower month after month needs a bigger warehouse "
          "or a smaller window.", unit="s", custom={**TS_LINES, "drawStyle": "points"}, options=TS_LEGEND),
    panel("timeseries", "Failed tasks per hour (Airflow)", [prom(
        "sum by (dag_id) (increase(airflow_task_finished{state=\"failed\"}[1h]))", "{{dag_id}}")], 12, 15, 12, 8,
          "Task tries that failed, per DAG (retries included).", ds=PROM, custom={**TS_LINES, "drawStyle": "bars"},
          options=TS_LEGEND),
    panel("timeseries", "Task duration, 95th percentile (1 day)", [prom(
        "nbo:task_duration_seconds:p95_1d", "{{dag_id}}.{{task_id}}")], 0, 23, 12, 8,
          "Slowest 5% of task runs. Compare with the task time limits (train 4 h, recommend 2 h).", ds=PROM,
          unit="s", custom=TS_LINES, options=TS_LEGEND),
    panel("timeseries", "Snowflake pool (max 2 jobs at a time)", [
        prom("max(airflow_pool_running_slots{pool=\"snowflake\"})", "running", "A"),
        prom("max(airflow_pool_queued_slots{pool=\"snowflake\"})", "waiting", "B")], 12, 23, 12, 8,
          "Waiting jobs for hours = the pool is too small or a job hangs.", ds=PROM, custom=TS_LINES,
          options=TS_LEGEND),
    row("Emails and deployments", 31),
    panel("table", "Emails per send", [sql("""
        SELECT run_date, status, emails FROM reporting.outbox_status ORDER BY run_date DESC, status""")], 0, 32, 12, 8,
          "READY = rendered, waiting for SMTP; SENT; FAILED (retried by Airflow); DRY_RUN = recorded, not mailed "
          "(customers without a real address).", overrides=COLOR_STATUS_CELL),
    panel("table", "Deployments", [sql("""
        SELECT deployed_at, code_version, deployed_by, source FROM reporting.deployments ORDER BY deployed_at DESC""")],
          12, 32, 12, 8, "Every deploy to production: which git commit, by whom (CI run or person)."),
]
pipeline = dashboard("nbo-pipeline", "NBO - Pipeline health", "Is the pipeline running and on time?", p,
                     {"from": "now-7d", "to": "now"}, ["pipeline"], refresh="1m")

# =====================================================================================
# 2. DATA QUALITY (per send date)
# =====================================================================================
_id[0] = 0
p = [
    text("**Is the input data fit?** The checks `NBO_DATA_CHECKS` runs before every send. FAIL stops the send; WARN "
         "is shown here and alerted, the send goes on. Time axis = send date (replay: Oct 2019 - Sep 2020).",
         0, 0, 24, 2),
    panel("stat", "Checks failed", [sql("""
        SELECT count(*) AS failed FROM reporting.data_checks WHERE status = 'FAIL' AND $__timeFilter(run_date)""")],
          0, 2, 8, 4, "Send dates stopped by bad data in the selected range.", options=STAT, thresholds=RED_IF_ABOVE_0),
    panel("stat", "Warnings", [sql("""
        SELECT count(*) AS warned FROM reporting.data_checks WHERE status = 'WARN' AND $__timeFilter(run_date)""")],
          8, 2, 8, 4, "Unusual but allowed (e.g. volume change during the 2020 lockdown).", options=STAT,
          thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}]),
    panel("stat", "Send dates checked", [sql("""
        SELECT count(DISTINCT run_date) AS dates FROM reporting.data_checks WHERE $__timeFilter(run_date)""")],
          16, 2, 8, 4, options={**STAT, "colorMode": "none"}),
    panel("timeseries", "Purchase volume vs the 4 weeks before", [sql("""
        SELECT run_date::timestamp AS time, value AS "volume change" FROM reporting.data_checks
        WHERE check_name = 'volume_change_vs_4_weeks' AND $__timeFilter(run_date) ORDER BY 1""", fmt="time_series")],
          0, 6, 12, 8, "Last 7 days vs the weekly average of the 4 weeks before. Outside +/-30% = WARN.",
          unit="percentunit", custom={**TS_LINES, "thresholdsStyle": {"mode": "area"}},
          thresholds=[{"color": "red", "value": None}, {"color": "transparent", "value": -0.3},
                      {"color": "red", "value": 0.3}], options=TS_LEGEND),
    panel("timeseries", "Broken / unknown shares", [sql("""
        SELECT run_date::timestamp AS time, check_name AS metric, value FROM reporting.data_checks
        WHERE check_name IN ('unknown_article_share', 'unknown_customer_share', 'bad_price_share',
                             'articles_missing_attributes_share') AND $__timeFilter(run_date) ORDER BY 1""",
                                                         fmt="time_series")],
          12, 6, 12, 8, "Each must stay at or below 1% (FAIL above).", unit="percentunit",
          custom={**TS_LINES, "thresholdsStyle": {"mode": "line"}},
          thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 0.01}], options=TS_LEGEND),
    panel("table", "All checks per send date", [sql("""
        SELECT run_date, check_name, value, expected, status, detail FROM reporting.data_checks
        WHERE $__timeFilter(run_date) ORDER BY run_date DESC, status, check_name""")], 0, 14, 24, 10,
          overrides=COLOR_STATUS_CELL),
]
data = dashboard("nbo-data", "NBO - Data quality", "Input data checks before every send", p, REPLAY_RANGE, ["data"])

# =====================================================================================
# 3. MODEL AND RECOMMENDATIONS
# =====================================================================================
_id[0] = 0
p = [
    text("**Are the models and the emails healthy?** Training on the 1st of each month is checked on the 15 days "
         "after the check date (precision@5) and compared with the simplest alternative - showing everyone the "
         "best sellers of the week before, scored on the same customers. The gate keeps last month's models if the "
         "new one is more than 20% worse. Lower rows: what the 6 slots actually filled, per send date.", 0, 0, 24, 2),
    panel("timeseries", "Model check: precision@5 vs best sellers", [sql("""
        SELECT model_date::timestamp AS time, taste_p5 AS "taste (slots 1, 2, 5, 6)",
               taste_base_p5 AS "best sellers (taste customers)", basket_p5 AS "basket (slot 3)",
               basket_base_p5 AS "best sellers (basket customers)"
        FROM reporting.model_registry WHERE $__timeFilter(model_date) ORDER BY 1""", fmt="time_series")],
          0, 2, 12, 8, "Share of the top 5 picks the customer actually bought in the next 15 days. Each model should "
          "stay above its best-seller line.", unit="percentunit", custom=TS_LINES, options=TS_LEGEND, decimals=2),
    panel("table", "Trainings and gate decisions", [sql("""
        SELECT model_date, gate_status, model_used,
               CASE WHEN beats_baseline THEN 'yes' WHEN NOT beats_baseline THEN 'NO' END AS beats_best_sellers,
               round(taste_p5::numeric, 4) AS taste_p5, round(taste_base_p5::numeric, 4) AS taste_best_sellers,
               round(prev_taste_p5::numeric, 4) AS last_month_taste, round(basket_p5::numeric, 4) AS basket_p5,
               round(basket_base_p5::numeric, 4) AS basket_best_sellers,
               round(prev_basket_p5::numeric, 4) AS last_month_basket, taste_customers, round(minutes::numeric, 1)
               AS minutes, code_version, gate_reason, baseline_note
        FROM reporting.model_registry WHERE $__timeFilter(model_date) ORDER BY model_date DESC""")], 12, 2, 12, 8,
          "FAIL = the new models were not used (model_used shows which ones were). beats_best_sellers NO = a model "
          "does not beat the best sellers (baseline_note says which).", overrides=COLOR_STATUS_CELL),
    panel("timeseries", "Slot filled by its own logic (not a fallback)", [sql("""
        SELECT run_date::timestamp AS time, 'slot ' || substr(metric, 5, 1) AS metric, 1 - value AS value
        FROM reporting.run_metrics WHERE metric LIKE 'slot%_fallback_rate' AND $__timeFilter(run_date)
        ORDER BY 1""", fmt="time_series")], 0, 10, 12, 8,
          "Low = the slot mostly fell back (e.g. trending list too thin for an age group).", unit="percentunit",
          custom=TS_LINES, options=TS_LEGEND, min_=0, max_=1),
    panel("timeseries", "Output checks", [sql("""
        SELECT run_date::timestamp AS time, metric, value FROM reporting.run_metrics
        WHERE metric IN ('emails_complete_share', 'top_product_share', 'cold_start_share', 'section_default_share')
          AND $__timeFilter(run_date) ORDER BY 1""", fmt="time_series")], 12, 10, 12, 8,
          "emails_complete_share must be >= 99%; top_product_share must stay under the 20% cap.",
          unit="percentunit", custom=TS_LINES, options=TS_LEGEND, min_=0, max_=1),
    panel("barchart", "Slots 5 and 6: how the 'new to try' pick was found", [sql("""
        SELECT run_date::text AS send, slot::text || ' ' || rule AS rule, cards FROM reporting.slot_mix
        WHERE slot IN (5, 6) AND $__timeFilter(run_date) ORDER BY 1""")], 0, 18, 12, 8,
          "AND = new type and colour, OR = new type or colour, best = nothing new left.",
          options={"stacking": "percent", "xField": "send", "legend": {"displayMode": "list", "placement": "bottom"}},
          custom={"fillOpacity": 80}),
    panel("timeseries", "Customers per send", [sql("""
        SELECT run_date::timestamp AS time, metric, value FROM reporting.run_metrics
        WHERE metric IN ('customers_email', 'customers_holdout', 'distinct_products') AND $__timeFilter(run_date)
        ORDER BY 1""", fmt="time_series")], 12, 18, 12, 8,
          "Holdout should stay about 5% of all customers picked.", custom=TS_LINES, options=TS_LEGEND),
]
model = dashboard("nbo-model", "NBO - Models and recommendations", "Model checks, gate and slot health", p,
                  REPLAY_RANGE, ["model"])

# =====================================================================================
# 4. EVALUATION (did the picks lead to purchases?)
# =====================================================================================
_id[0] = 0
EVAL = "reporting.eval_results"
p = [
    text("**Did the recommendations lead to purchases?** A pick counts as bought when the customer bought that "
         "product (any colour) within 90 days of the send - read from the real purchase data. Cards are shown in a "
         "random order, so *slot* (which logic) and *position* (where in the email) are measured separately. "
         "Ranges = 95% bootstrap interval. In the 2019-2020 replay nobody received the email, so emailed vs holdout "
         "is an A/A check: its range should include 0. Opens and clicks would come from a website's tracking "
         "(not part of this build).", 0, 0, 24, 3),
    panel("stat", "Emails with a bought pick", [sql(f"""
        SELECT avg(value) AS email_buy_rate FROM {EVAL} WHERE group_by = 'email' AND metric = 'email_buy_rate'
          AND $__timeFilter(run_date)""")], 0, 3, 6, 4, "Share of emails where at least one of the 6 picks was bought.",
          unit="percentunit", decimals=2, options={**STAT, "colorMode": "none"}),
    panel("stat", "Picks bought per email", [sql(f"""
        SELECT avg(value) AS buys_per_email FROM {EVAL} WHERE group_by = 'email' AND metric = 'buys_per_email'
          AND $__timeFilter(run_date)""")], 6, 3, 6, 4, decimals=3, options={**STAT, "colorMode": "none"}),
    panel("stat", "Lift: any purchase, all sends pooled", [sql(f"""
        SELECT value AS lift FROM {EVAL} WHERE group_by = 'holdout_pooled' AND group_key = 'email_minus_holdout'
          AND metric = 'any_buy_rate' AND $__timeFilter(run_date) ORDER BY run_date DESC LIMIT 1""")], 12, 3, 6, 4,
          "Emailed minus holdout over every evaluated send (customers counted once). Replay: expected around 0 "
          "(nobody really got the email).", unit="percentunit", decimals=2, options={**STAT, "colorMode": "none"}),
    panel("stat", "Send dates evaluated", [sql(f"""
        SELECT count(DISTINCT run_date) AS sends FROM {EVAL} WHERE $__timeFilter(run_date)""")], 18, 3, 6, 4,
          "Sends whose 90 days are covered by the purchase data (up to late June 2020).",
          options={**STAT, "colorMode": "none"}),
    panel("barchart", "Buy rate by slot (which logic works)", [sql(f"""
        SELECT 'slot ' || group_key AS slot, avg(value) AS buy_rate, avg(ci_low) AS low, avg(ci_high) AS high
        FROM {EVAL} WHERE group_by = 'slot' AND metric = 'buy_rate' AND $__timeFilter(run_date)
        GROUP BY group_key ORDER BY group_key""")], 0, 7, 12, 8,
          "1 your style, 2 occasion, 3 popular with people like you, 4 trending for you, 5 new to try, "
          "6 new for the occasion.", unit="percentunit",
          options={"xField": "slot", "legend": {"displayMode": "list", "placement": "bottom"}},
          custom={"fillOpacity": 80}, decimals=3),
    panel("barchart", "Buy rate by position (where it was shown)", [sql(f"""
        SELECT 'position ' || group_key AS position, avg(value) AS buy_rate, avg(ci_low) AS low,
               avg(ci_high) AS high
        FROM {EVAL} WHERE group_by = 'position' AND metric = 'buy_rate' AND $__timeFilter(run_date)
        GROUP BY group_key ORDER BY group_key""")], 12, 7, 12, 8,
          "Position should not matter for buys in a replay (nobody saw the email) - a check of the random order.",
          unit="percentunit", options={"xField": "position", "legend": {"displayMode": "list", "placement": "bottom"}},
          custom={"fillOpacity": 80}, decimals=3),
    panel("timeseries", "Buy rate by slot per send", [sql(f"""
        SELECT run_date::timestamp AS time, 'slot ' || group_key AS metric, value FROM {EVAL}
        WHERE group_by = 'slot' AND metric = 'buy_rate' AND $__timeFilter(run_date) ORDER BY 1""",
                                                      fmt="time_series")], 0, 15, 12, 8,
          "Bought the picked product (any colour) within 90 days.", unit="percentunit", custom=TS_LINES,
          options=TS_LEGEND, decimals=2),
    panel("timeseries", "Exact item bought, by slot per send", [sql(f"""
        SELECT run_date::timestamp AS time, 'slot ' || group_key AS metric, value FROM {EVAL}
        WHERE group_by = 'slot' AND metric = 'buy_item_rate' AND $__timeFilter(run_date) ORDER BY 1""",
                                                                fmt="time_series")], 12, 15, 12, 8,
          "Bought exactly the article shown (same colour).", unit="percentunit", custom=TS_LINES, options=TS_LEGEND,
          decimals=2),
    row("Did the email change anything? (emailed minus holdout)", 23),
    panel("timeseries", "Lift: any purchase within 90 days", [sql(f"""
        SELECT run_date::timestamp AS time, value AS lift, ci_low AS "95% low", ci_high AS "95% high" FROM {EVAL}
        WHERE group_by = 'holdout' AND group_key = 'email_minus_holdout' AND metric = 'any_buy_rate'
          AND $__timeFilter(run_date) ORDER BY 1""", fmt="time_series")], 0, 24, 12, 8,
          "Above 0 with the whole range above 0 = the emails create purchases that would not have happened.",
          unit="percentunit", custom={**TS_LINES, "thresholdsStyle": {"mode": "line"}},
          thresholds=[{"color": "transparent", "value": None}, {"color": "text", "value": 0}], options=TS_LEGEND,
          decimals=2),
    panel("table", "Holdout comparison per send", [sql(f"""
        SELECT run_date, metric, round(value::numeric, 5) AS email_minus_holdout,
               round(ci_low::numeric, 5) AS low, round(ci_high::numeric, 5) AS high, n AS customers,
               CASE WHEN ci_low > 0 THEN 'emails help' WHEN ci_high < 0 THEN 'emails hurt' ELSE 'no clear difference'
               END AS verdict
        FROM {EVAL} WHERE group_by = 'holdout' AND group_key = 'email_minus_holdout' AND $__timeFilter(run_date)
        ORDER BY run_date DESC, metric""")], 12, 24, 12, 8),
    panel("timeseries", "Pooled lift as sends add up (any purchase)", [sql(f"""
        SELECT l.run_date::timestamp AS time, l.value AS lift, l.ci_low AS "95% low", l.ci_high AS "95% high",
               m.value AS "smallest detectable lift"
        FROM {EVAL} l JOIN {EVAL} m ON m.run_date = l.run_date AND m.group_by = 'holdout_pooled'
          AND m.group_key = 'min_detectable_lift' AND m.metric = l.metric
        WHERE l.group_by = 'holdout_pooled' AND l.group_key = 'email_minus_holdout' AND l.metric = 'any_buy_rate'
          AND $__timeFilter(l.run_date) ORDER BY 1""", fmt="time_series")], 0, 32, 12, 8,
          "Each point pools every send evaluated up to that date. The range narrows as customers add up; a real "
          "lift smaller than the 'smallest detectable lift' line would not show.", unit="percentunit",
          custom={**TS_LINES, "thresholdsStyle": {"mode": "line"}},
          thresholds=[{"color": "transparent", "value": None}, {"color": "text", "value": 0}], options=TS_LEGEND,
          decimals=3),
    panel("table", "Pooled comparison (latest)", [sql(f"""
        SELECT l.metric, round(l.value::numeric, 5) AS email_minus_holdout, round(l.ci_low::numeric, 5) AS low,
               round(l.ci_high::numeric, 5) AS high, round(h.value::numeric, 5) AS holdout_level,
               round(m.value::numeric, 5) AS smallest_detectable, l.n AS customers,
               CASE WHEN l.ci_low > 0 THEN 'emails help' WHEN l.ci_high < 0 THEN 'emails hurt'
                    ELSE 'no clear difference' END AS verdict
        FROM {EVAL} l
        JOIN {EVAL} h ON h.run_date = l.run_date AND h.group_by = l.group_by AND h.metric = l.metric
          AND h.group_key = 'holdout_level'
        JOIN {EVAL} m ON m.run_date = l.run_date AND m.group_by = l.group_by AND m.metric = l.metric
          AND m.group_key = 'min_detectable_lift'
        WHERE l.group_by = 'holdout_pooled' AND l.group_key = 'email_minus_holdout'
          AND l.run_date = (SELECT max(run_date) FROM {EVAL} WHERE group_by = 'holdout_pooled'
                            AND $__timeFilter(run_date))
        ORDER BY l.metric""")], 12, 32, 12, 8,
          "All sends evaluated so far, each customer counted once. In the replay the verdict should be "
          "'no clear difference' (an A/A check of the holdout split)."),
]
business = dashboard("nbo-evaluation", "NBO - Evaluation", "Buy rate per slot and position; emailed vs holdout", p,
                     REPLAY_RANGE, ["evaluation"])


# =====================================================================================
# ALERT RULES
# =====================================================================================
NOW = "(now() AT TIME ZONE 'UTC')"          # run_log times are stored in UTC without a zone


def pg_rule(uid, title, sql, severity, summary, todo, for_="0m", no_data="OK", op="gt", limit=0):
    """Postgres query returning ONE number -> alert when number <op> limit."""
    return {
        "uid": uid, "title": title, "condition": "C",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 3600, "to": 0}, "datasourceUid": "reporting",
             "model": {"refId": "A", "rawQuery": True, "editorMode": "code", "format": "table",
                       "rawSql": " ".join(sql.split())}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": op, "params": [limit]}}]}},
        ],
        "noDataState": no_data, "execErrState": "Error", "for": for_, "isPaused": False,
        "labels": {"severity": severity, "system": "nbo"},
        "annotations": {"summary": summary, "description": todo},
    }


def prom_rule(uid, title, expr, severity, summary, todo, op, limit, for_="5m", no_data="Alerting"):
    """Prometheus instant query -> last value -> alert when value <op> limit."""
    return {
        "uid": uid, "title": title, "condition": "C",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": "prometheus",
             "model": {"refId": "A", "expr": expr, "instant": True, "range": False, "editorMode": "code"}},
            {"refId": "B", "datasourceUid": "__expr__",
             "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last"}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "threshold", "expression": "B",
                       "conditions": [{"evaluator": {"type": op, "params": [limit]}}]}},
        ],
        "noDataState": no_data, "execErrState": "Error", "for": for_, "isPaused": False,
        "labels": {"severity": severity, "system": "nbo"},
        "annotations": {"summary": summary, "description": todo},
    }


pipeline_rules = [
    pg_rule("nbo-step-failed", "A pipeline step failed",
            f"SELECT count(*) AS failed FROM reporting.run_log WHERE status = 'FAILED' "
            f"AND start_time > {NOW} - interval '1 hour'",
            "critical", "{{ $values.A }} procedure run(s) FAILED in the last hour.",
            "WHAT TO DO: dashboard 'NBO - Pipeline health' > 'Latest result of each step' shows which step and the "
            "error. Crashes (error text) were retried by Airflow already; rule stops (no error text: data check, "
            "output check) need the cause fixed. Then clear the failed Airflow task - every step is safe to re-run."),
    pg_rule("nbo-send-late", "Send not finished by 09:00",
            "SELECT CASE WHEN extract(day FROM now() AT TIME ZONE 'Europe/Berlin') IN (1, 16) "
            "AND (now() AT TIME ZONE 'Europe/Berlin')::time > time '09:00' "
            "AND NOT EXISTS (SELECT 1 FROM reporting.run_log WHERE procedure_name = 'NBO_EVENTS_SENT' "
            "AND status = 'SUCCESS' AND run_date = (now() AT TIME ZONE 'Europe/Berlin')::date) "
            "THEN 1 ELSE 0 END AS late",
            "critical", "Today is a send day and the send has not finished by 09:00 (it starts 06:00).",
            "WHAT TO DO: open the nbo_send run of today in Airflow. Waiting in wait_for_data = upstream purchases "
            "not loaded; waiting in dev_rehearsal = the monthly DEV rehearsal has not passed; a long train = check "
            "the Snowflake query history (QUERY_TAG airflow:nbo_send:train). Note: in the 2019-2020 replay send "
            "dates are in the past, so this alert only acts in live operation."),
    pg_rule("nbo-emails-stuck", "Emails stuck in SENDING",
            "SELECT coalesce(sum(emails), 0) AS stuck FROM reporting.outbox_status WHERE status = 'SENDING'",
            "warning", "{{ $values.A }} email(s) were claimed for sending but never confirmed.",
            "WHAT TO DO: Airflow stopped between sending and marking. Check the mail server log for these "
            "EMAIL_IDs (header X-NBO-Email-Id). Sent -> set STATUS='SENT'; not sent -> set STATUS='READY' and "
            "clear send_emails. They are never re-sent automatically (no duplicates).", for_="30m"),
    pg_rule("nbo-export-stale", "Dashboards are stale",
            "SELECT CASE WHEN coalesce(max(exported_at), '1970-01-01') < now() - interval '3 hours' THEN 1 ELSE 0 "
            "END AS stale FROM reporting.export_log",
            "warning", "The Snowflake -> Postgres copy has not run for 3 hours: dashboards show old numbers.",
            "WHAT TO DO: check the nbo_reporting_export DAG in Airflow (is it paused? failing?). The pipeline itself "
            "is not affected - only what Grafana shows."),
]

quality_rules = [
    pg_rule("nbo-data-check-failed", "Input data check FAILED",
            "SELECT count(*) AS failed FROM reporting.data_checks WHERE status = 'FAIL' "
            "AND checked_at > now() AT TIME ZONE 'UTC' - interval '24 hours'",
            "critical", "{{ $values.A }} data check(s) failed: the send was stopped.",
            "WHAT TO DO: dashboard 'NBO - Data quality' > 'All checks per send date' shows which check and the "
            "numbers. Fix the source data upstream (HM_ODS), then clear data_checks in the nbo_send run."),
    pg_rule("nbo-data-check-warn", "Unusual input data (warning)",
            "SELECT count(*) AS warned FROM reporting.data_checks WHERE status = 'WARN' "
            "AND checked_at > now() AT TIME ZONE 'UTC' - interval '24 hours'",
            "warning", "{{ $values.A }} data check warning(s); the send continued.",
            "WHAT TO DO: look at 'Purchase volume vs the 4 weeks before'. A real event (sale, lockdown) needs no "
            "action; a double or partial load upstream does - ask the data owner before the next send."),
    pg_rule("nbo-gate-failed", "New models rejected by the gate",
            "SELECT count(*) AS failed FROM reporting.model_registry WHERE gate_status = 'FAIL' "
            "AND trained_at > now() AT TIME ZONE 'UTC' - interval '24 hours'",
            "warning", "This month's models were more than 20% worse than last month's: last month's are used.",
            "WHAT TO DO: dashboard 'NBO - Models and recommendations' > 'Trainings and gate decisions' shows both "
            "numbers. Compare the data checks of the date (a drop in data explains it); if the data is fine, "
            "investigate the training window before next month. Sends keep working with the old models."),
    pg_rule("nbo-model-below-baseline", "Model does not beat the best sellers",
            "SELECT count(*) AS behind FROM reporting.model_registry WHERE beats_baseline = false "
            "AND trained_at > now() AT TIME ZONE 'UTC' - interval '24 hours'",
            "warning", "A new model scored no better than showing everyone last week's best sellers.",
            "WHAT TO DO: dashboard 'NBO - Models and recommendations' > 'Trainings and gate decisions', column "
            "baseline_note says which model and by how much. Sends continue (last month's model would not be better: "
            "same method). Check the data checks of the date and the training window; if it repeats next month, "
            "the model needs work - consider filling that slot from the best sellers until it is fixed."),
    pg_rule("nbo-emails-hurt", "Emails reduce purchases",
            "SELECT count(*) AS hurt FROM reporting.eval_results WHERE group_by = 'holdout' "
            "AND group_key = 'email_minus_holdout' AND metric = 'any_buy_rate' AND ci_high < 0 "
            "AND run_date > (now() AT TIME ZONE 'UTC')::date - 120",
            "warning", "For a recent send, emailed customers bought LESS than the holdout (whole 95% range below 0).",
            "WHAT TO DO: dashboard 'NBO - Evaluation' > 'Holdout comparison per send'. Check send frequency "
            "(fatigue), the slots with the lowest buy rate and unsubscribes. One send can be noise; two in a row "
            "is a reason to pause sends (switch nbo_send_enabled off)."),
]

system_rules = [
    prom_rule("nbo-scheduler-down", "Airflow scheduler down",
              "nbo:scheduler_heartbeats:rate5m or vector(0)", "critical",
              "The Airflow scheduler sent no heartbeat for 5 minutes: nothing is being scheduled.",
              "WHAT TO DO: docker compose ps (is airflow-scheduler healthy?), docker compose logs airflow-scheduler. "
              "Usually the metadata database or memory. Restart: docker compose up -d airflow-scheduler. Missed "
              "sends run automatically afterwards (catchup).", "lt", 0.01),
    prom_rule("nbo-dag-import-errors", "A DAG file does not load",
              "max(airflow_dag_processing_import_errors) or vector(0)", "critical",
              "{{ $values.B }} DAG file(s) fail to import: those DAGs are missing from Airflow.",
              "WHAT TO DO: Airflow UI > DAG import errors shows the file and the error. The DAG tests in CI should "
              "have caught it - check how the change reached main. Revert the commit; the git bundle picks it up "
              "within a minute.", "gt", 0, for_="2m", no_data="OK"),
    prom_rule("nbo-snowflake-queue", "Snowflake jobs waiting for hours",
              "max(airflow_pool_queued_slots{pool=\"snowflake\"}) or vector(0)", "warning",
              "Tasks have waited for a Snowflake slot for over 2 hours.",
              "WHAT TO DO: a job holds a slot too long (look at 'Snowflake pool' and running tasks) or too many DAGs "
              "run at once. Do not just raise the pool: check the Snowflake query history first.", "gt", 0,
              for_="2h", no_data="OK"),
]

ALERTS = {"apiVersion": 1, "groups": [
    {"orgId": 1, "name": "nbo-pipeline", "folder": "NBO", "interval": "5m", "rules": pipeline_rules},
    {"orgId": 1, "name": "nbo-quality", "folder": "NBO", "interval": "15m", "rules": quality_rules},
    {"orgId": 1, "name": "nbo-system", "folder": "NBO", "interval": "1m", "rules": system_rules},
]}


# =====================================================================================
# write / check
# =====================================================================================
ALERT_HEADER = ("# Alert rules, provisioned at Grafana start-up. GENERATED by monitoring/grafana/build.py - edit there.\n"
                "# Each rule says what it means and WHAT TO DO. Postgres rules return one number; Prometheus rules take\n"
                "# the last value.\n")


def rendered():
    """{relative path: file content} for everything this script owns."""
    out = {}
    for name, d in [("pipeline", pipeline), ("data_quality", data), ("model", model), ("evaluation", business)]:
        out[os.path.join("dashboards", f"{name}.json")] = json.dumps(d, indent=2) + "\n"
    out[os.path.join("provisioning", "alerting", "rules.yml")] = \
        ALERT_HEADER + yaml.safe_dump(ALERTS, sort_keys=False, width=120, allow_unicode=True)
    return out


def main(argv=None):
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    stale = []
    for rel, text in rendered().items():
        path = os.path.join(HERE, rel)
        current = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if check:
            if current != text:
                stale.append(rel)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:      # same bytes on Windows and Linux
                f.write(text)
            print("wrote", rel)
    if stale:
        raise SystemExit(f"Out of date (run python monitoring/grafana/build.py and commit): {stale}")


if __name__ == "__main__":
    main()
