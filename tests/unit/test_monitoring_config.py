"""
Monitoring as code: the dashboards, alert rules and data sources are files, so they are tested like code.
(The SQL inside them runs against a real Postgres in tests/monitoring; promtool / statsd-exporter / compose
checks run as separate CI steps.)
"""
import glob
import json
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MON = os.path.join(ROOT, "monitoring")
PROV = os.path.join(MON, "grafana", "provisioning")
DASHBOARDS = sorted(glob.glob(os.path.join(MON, "grafana", "dashboards", "*.json")))


def load_yaml(*parts):
    with open(os.path.join(*parts)) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def datasource_uids():
    doc = load_yaml(PROV, "datasources", "datasources.yml")
    return {d["uid"] for d in doc["datasources"]}


@pytest.fixture(scope="module")
def rules():
    doc = load_yaml(PROV, "alerting", "rules.yml")
    return [r for g in doc["groups"] for r in g["rules"]]


def test_four_dashboards_with_unique_uids():
    uids = [json.load(open(f))["uid"] for f in DASHBOARDS]
    assert len(DASHBOARDS) == 4 and len(set(uids)) == 4


def test_generated_files_match_the_builder():
    """dashboards/*.json and alerting/rules.yml must be exactly what monitoring/grafana/build.py produces."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("grafana_build", os.path.join(MON, "grafana", "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    build.main(["--check"])


def test_dashboards_only_use_provisioned_datasources(datasource_uids):
    for f in DASHBOARDS:
        for p in json.load(open(f))["panels"]:
            for t in p.get("targets", []):
                assert t["datasource"]["uid"] in datasource_uids, (f, p["title"])


def test_every_data_panel_has_a_title_and_a_query():
    for f in DASHBOARDS:
        for p in json.load(open(f))["panels"]:
            if p["type"] in ("row", "text"):
                continue
            assert p["title"] and p["targets"], (f, p.get("title"))
            for t in p["targets"]:
                assert t.get("rawSql") or t.get("expr"), (f, p["title"])


def test_dashboards_cannot_be_changed_in_the_ui():
    prov = load_yaml(PROV, "dashboards", "dashboards.yml")["providers"][0]
    assert prov["allowUiUpdates"] is False and prov["disableDeletion"] is True
    assert all(json.load(open(f))["editable"] is False for f in DASHBOARDS)


def test_every_alert_says_what_to_do(rules):
    assert len(rules) >= 10
    for r in rules:
        assert r["annotations"]["summary"], r["uid"]
        assert r["annotations"]["description"].startswith("WHAT TO DO:"), r["uid"]
        assert r["labels"]["severity"] in ("critical", "warning"), r["uid"]


def test_alert_uids_unique_and_queries_valid(rules, datasource_uids):
    uids = [r["uid"] for r in rules]
    assert len(uids) == len(set(uids))
    for r in rules:
        refs = {q["refId"] for q in r["data"]}
        assert r["condition"] in refs, r["uid"]
        for q in r["data"]:
            assert q["datasourceUid"] in datasource_uids | {"__expr__"}, r["uid"]


def test_alerts_route_to_a_contact_point():
    cps = {c["name"] for c in load_yaml(PROV, "alerting", "contact_points.yml")["contactPoints"]}
    pol = load_yaml(PROV, "alerting", "policies.yml")["policies"][0]
    assert pol["receiver"] in cps and all(r["receiver"] in cps for r in pol.get("routes", []))


def test_prometheus_scrapes_the_exporters_that_compose_starts():
    compose = load_yaml(ROOT, "docker-compose.yaml")
    prom = load_yaml(MON, "prometheus", "prometheus.yml")
    for job in prom["scrape_configs"]:
        for target in job["static_configs"][0]["targets"]:
            host = target.split(":")[0]
            assert host == "localhost" or host in compose["services"], target


def test_compose_mounts_existing_files():
    compose = load_yaml(ROOT, "docker-compose.yaml")
    for name, svc in compose["services"].items():
        for v in svc.get("volumes", []):
            src = v.split(":")[0]
            if src.startswith("./") and "secrets" not in src:
                assert os.path.exists(os.path.join(ROOT, src[2:])), (name, src)


def test_every_compose_variable_is_documented_in_env_example():
    text = open(os.path.join(ROOT, "docker-compose.yaml")).read()
    used = set(re.findall(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*)", text))         # $${X} is a container variable
    documented = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", open(os.path.join(ROOT, ".env.example")).read(), re.M))
    assert used <= documented, used - documented


def test_no_secret_values_in_committed_config():
    """Passwords and keys only come from .env / secrets: no literal value in the committed files."""
    pattern = re.compile(r"(password|secret|token|passphrase)\s*[:=]\s*['\"]?[A-Za-z0-9+/]{12,}", re.I)
    files = [os.path.join(ROOT, "docker-compose.yaml"), os.path.join(ROOT, ".env.example")] + \
        glob.glob(os.path.join(MON, "**", "*.yml"), recursive=True)
    for f in files:
        for line in open(f):
            assert not pattern.search(line) or "${" in line or "$" in line.split(":", 1)[-1], (f, line)
