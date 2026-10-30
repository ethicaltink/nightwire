"""The alert rules in deploy/alerts.yml.

The rules are the deliverable, not the metrics endpoint, so they get
checked like code. The important assertion is the last one: **every metric an
alert references is actually exported.** A rule naming a metric that does not
exist never fires, and looks exactly like a rule that is simply quiet.
"""

import pathlib
import re

import pytest

from nightwire import metrics

ALERTS = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "alerts.yml"

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read the rules")


def rules():
    document = yaml.safe_load(ALERTS.read_text())
    found = []
    for group in document["groups"]:
        for rule in group["rules"]:
            found.append(rule)
    return found


def test_the_rules_file_exists_and_parses():
    assert ALERTS.is_file()
    document = yaml.safe_load(ALERTS.read_text())
    assert document["groups"], "no rule groups"


def test_every_rule_has_what_prometheus_requires():
    for rule in rules():
        assert rule.get("alert"), rule
        assert rule.get("expr"), rule["alert"]
        assert rule.get("for"), f"{rule['alert']} has no `for`, so it fires on a single scrape"
        assert rule.get("labels", {}).get("severity"), rule["alert"]


def test_every_rule_explains_itself_to_a_human():
    """An alert that arrives at 3am has to say what to do about it."""
    for rule in rules():
        annotations = rule.get("annotations") or {}
        assert annotations.get("summary"), rule["alert"]
        description = annotations.get("description") or ""
        assert len(description) > 60, f"{rule['alert']} description is too thin"


def test_alert_names_are_unique():
    names = [rule["alert"] for rule in rules()]
    assert len(names) == len(set(names))


def test_the_rules_the_spec_names_are_all_present():
    names = {rule["alert"] for rule in rules()}
    for required in [
        "NightwireDown",
        "NightwireRecorderStopped",
        "NightwireNoNewSegments",
        "NightwireDiskFilling",
        "NightwireRetentionShort",
        "NightwireExportStoreFull",
        "NightwireEventListenerDown",
        "NightwireCameraClockSkew",
    ]:
        assert required in names, f"{required} is in the design notes but not in the rules"


def test_the_recorder_stopped_rule_is_not_instant():
    """A camera blips; 10 minutes of silence is the real signal."""
    rule = next(r for r in rules() if r["alert"] == "NightwireRecorderStopped")
    assert rule["for"] in ("10m", "15m")


def test_every_metric_referenced_by_a_rule_is_exported():
    """The assertion that makes this file worth having.

    A rule referencing `nightwire_recorder_running` when the code exports
    `nightwire_recorder_recording` would simply never fire, and would be
    indistinguishable from a healthy system.
    """
    exported = metrics.exported_names()
    # `up` comes from Prometheus itself, not from this app.
    allowed = exported | {"up"}

    referenced: set[str] = set()
    for rule in rules():
        for token in re.findall(r"\b(nightwire_[a-z0-9_]+|up)\b", rule["expr"]):
            referenced.add(token)

    assert referenced, "no metrics referenced at all, which cannot be right"
    missing = sorted(referenced - allowed)
    assert not missing, f"rules reference metrics that are never exported: {missing}"


def test_rules_do_not_hardcode_the_disk_thresholds():
    """The thresholds are configurable on Core, so a rule must read them."""
    rule = next(r for r in rules() if r["alert"] == "NightwireDiskFilling")
    assert "nightwire_disk_max_percent" in rule["expr"]
    assert not re.search(r">\s*9[05]\b", rule["expr"]), "threshold hardcoded"


def test_the_file_says_how_to_install_it_and_that_alertmanager_is_required():
    text = ALERTS.read_text()
    assert "rule_files" in text
    assert "scrape_configs" in text
    assert "Alertmanager" in text, "the rules must say they need somewhere to go"
