"""Render the internal operational snapshot as Prometheus text."""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from typing import Any


def _label_value(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _label_name(value: object) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", str(value))
    if not normalized or normalized[0].isdigit():
        normalized = f"label_{normalized}"
    return normalized


def _sample(
    name: str,
    value: object,
    labels: Mapping[str, object] | None = None,
) -> str | None:
    if isinstance(value, bool):
        numeric = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        numeric = float(value)
    else:
        return None
    if not math.isfinite(numeric):
        return None
    label_text = ""
    if labels:
        normalized_labels: dict[str, object] = {}
        for key, label_value in sorted(labels.items()):
            normalized_key = _label_name(key)
            while normalized_key in normalized_labels:
                normalized_key = f"label_{normalized_key}"
            normalized_labels[normalized_key] = label_value
        rendered = ",".join(
            f'{key}="{_label_value(label_value)}"'
            for key, label_value in sorted(normalized_labels.items())
        )
        label_text = "{" + rendered + "}"
    return f"stt_to_subtitle_{name}{label_text} {numeric:g}"


def _append(
    lines: list[str],
    name: str,
    value: object,
    labels: Mapping[str, object] | None = None,
) -> None:
    rendered = _sample(name, value, labels)
    if rendered is not None:
        lines.append(rendered)


def prometheus_exposition(snapshot: Mapping[str, Any]) -> str:
    """Return a bounded-cardinality Prometheus 0.0.4 exposition."""

    lines: list[str] = []
    jobs = snapshot.get("jobs")
    if isinstance(jobs, Mapping):
        _append(lines, "jobs_total", jobs.get("total"))
        for state, value in _mapping(jobs.get("by_state")).items():
            _append(lines, "jobs_by_state", value, {"state": state})
        for phase, value in _mapping(jobs.get("by_phase")).items():
            _append(lines, "jobs_by_phase", value, {"phase": phase})
        for phase, states in _mapping(jobs.get("by_phase_state")).items():
            for state, value in _mapping(states).items():
                _append(
                    lines,
                    "jobs_by_phase_state",
                    value,
                    {"phase": phase, "state": state},
                )
        for reason, value in _mapping(jobs.get("by_reason")).items():
            _append(lines, "jobs_by_reason", value, {"reason": reason})
        for phase, value in _mapping(
            jobs.get("oldest_waiting_seconds")
        ).items():
            _append(
                lines,
                "oldest_waiting_seconds",
                value,
                {"phase": phase},
            )
        _append(lines, "job_retries_total", jobs.get("total_retries"))
        _append(lines, "job_attempt_maximum", jobs.get("max_attempt"))

    for state, value in _mapping(snapshot.get("leases")).items():
        _append(lines, "leases", value, {"state": state})

    database = _mapping(snapshot.get("database"))
    _append(
        lines,
        "database_integrity",
        database.get("valid"),
        {"check": "valid"},
    )
    _append(
        lines,
        "database_integrity",
        database.get("foreign_keys_enabled"),
        {"check": "foreign_keys_enabled"},
    )
    _append(
        lines,
        "database_foreign_key_violations",
        database.get("foreign_key_violation_count"),
    )
    for statistic, value in _mapping(database.get("migrations")).items():
        _append(
            lines,
            "database_migrations",
            value,
            {"statistic": statistic},
        )

    remote_stt = _mapping(snapshot.get("remote_stt"))
    for state, key in (
        ("running", "running_job_ids"),
        ("cancel_pending", "cancel_pending_job_ids"),
    ):
        values = remote_stt.get(key)
        if isinstance(values, list):
            _append(lines, "remote_stt_jobs", len(values), {"state": state})

    dependencies = snapshot.get("dependencies")
    if isinstance(dependencies, list):
        for dependency in dependencies:
            if not isinstance(dependency, Mapping):
                continue
            labels = {
                "dependency": dependency.get("dependency", "unknown"),
                "state": dependency.get("state", "unknown"),
                "reason": dependency.get("reason_code") or "",
            }
            _append(lines, "dependency_state", 1, labels)
            _append(
                lines,
                "dependency_updated_timestamp_seconds",
                dependency.get("updated_at"),
                {"dependency": labels["dependency"]},
            )

    events = _mapping(snapshot.get("events"))
    for code, value in _mapping(events.get("by_code")).items():
        _append(lines, "events_total", value, {"code": code})
    for phase, metrics in _mapping(events.get("stages")).items():
        stage = _mapping(metrics)
        _append(
            lines,
            "stage_started_total",
            stage.get("started"),
            {"phase": phase},
        )
        for outcome, value in _mapping(stage.get("outcomes")).items():
            _append(
                lines,
                "stage_outcomes_total",
                value,
                {"phase": phase, "outcome": outcome},
            )
        for duration_kind in ("wait_seconds", "processing_seconds"):
            duration = _mapping(stage.get(duration_kind))
            for statistic in ("samples", "average", "maximum"):
                _append(
                    lines,
                    f"stage_{duration_kind}",
                    duration.get(statistic),
                    {"phase": phase, "statistic": statistic},
                )

    measurements = snapshot.get("measurements")
    if isinstance(measurements, list):
        for measurement in measurements:
            if not isinstance(measurement, Mapping):
                continue
            measurement_labels = {
                str(key): value
                for key, value in _mapping(
                    measurement.get("labels")
                ).items()
            }
            labels = {
                "metric": measurement.get("metric", "unknown"),
                **measurement_labels,
            }
            if (
                measurement.get("metric")
                == "pipeline.stage.duration_seconds"
            ):
                for suffix, key in (
                    ("count", "sample_count"),
                    ("sum", "total"),
                    ("maximum", "maximum"),
                ):
                    _append(
                        lines,
                        f"stage_duration_seconds_{suffix}",
                        measurement.get(key),
                        measurement_labels,
                    )
            if (
                measurement.get("metric")
                == "translation.pass.active_seconds"
            ):
                for suffix, key in (
                    ("count", "sample_count"),
                    ("sum", "total"),
                    ("maximum", "maximum"),
                ):
                    _append(
                        lines,
                        f"translation_pass_active_seconds_{suffix}",
                        measurement.get(key),
                        measurement_labels,
                    )
            if measurement.get("metric") == "translation.pass.requests":
                _append(
                    lines,
                    "translation_pass_requests_total",
                    measurement.get("total"),
                    measurement_labels,
                )
            for suffix, key in (
                ("samples_total", "sample_count"),
                ("sum", "total"),
                ("maximum", "maximum"),
                ("last", "last_value"),
                ("updated_timestamp_seconds", "updated_at"),
            ):
                _append(
                    lines,
                    f"operational_measurement_{suffix}",
                    measurement.get(key),
                    labels,
                )

    lines.sort()
    return "\n".join(lines) + "\n"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
