"""Task-independent progress normalization; estimates never invent missing work."""
import math
import statistics
from collections import deque


ALIASES = (
    ("training", "训练", ("completed_episodes", "episodes_completed", "episodes"),
     ("episodes_total", "target_episodes"), "回合", True),
    ("updates", "优化更新", ("optimizer_steps", "optimizer_updates", "updates"),
     ("updates_total", "target_optimizer_steps"), "次", False),
    ("validation", "验证", ("validation_done", "validation_completed", "completed_validation_cases", "validation"),
     ("validation_total",), "场", True),
    ("testing", "测试", ("tests_completed", "tests"), ("tests_total",), "场", True),
)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def first(source, names):
    return next((source[key] for key in names if number(source.get(key))), None)


def normalize_metrics(progress=None, metadata=None):
    progress, metadata = progress or {}, metadata or {}
    metrics = []
    declared = progress.get("metrics", metadata.get("metrics"))
    if isinstance(metadata.get("metrics"), list):
        declarations = {str(item.get("id", "metric-%d" % index)): dict(item, current=0)
                        for index, item in enumerate(metadata["metrics"]) if isinstance(item, dict)}
        for index, item in enumerate(progress.get("metrics", []) or []):
            if isinstance(item, dict):
                key = str(item.get("id", "metric-%d" % index))
                declarations[key] = dict(declarations.get(key, {}), **item)
        declared = list(declarations.values())
    if not isinstance(declared, list):
        phases = progress.get("phases", metadata.get("phases", []))
        if isinstance(phases, list):
            phase_metrics = []
            for index, phase in enumerate(phases):
                if not isinstance(phase, dict):
                    continue
                phase_id = str(phase.get("id", "phase-%d" % index))
                if isinstance(phase.get("metrics"), list):
                    for metric in phase["metrics"]:
                        if isinstance(metric, dict):
                            phase_metrics.append(dict(metric, id=phase_id + ":" + str(metric.get("id", "progress")),
                                                      phase_id=phase_id))
                elif "total" in phase or "current" in phase:
                    phase_metrics.append(dict(phase, id=phase_id, phase_id=phase_id))
            if phase_metrics:
                declared = phase_metrics
    if isinstance(declared, list):
        for index, item in enumerate(declared):
            if not isinstance(item, dict):
                continue
            current, total = item.get("current"), item.get("total")
            current = max(0, current) if number(current) else None
            total = total if number(total) and total >= 0 else None
            metrics.append(dict(item, id=str(item.get("id", "metric-%d" % index)),
                                label=str(item.get("label", item.get("id", "进度"))),
                                current=current, total=total, unit=str(item.get("unit", "")),
                                contributes_to_overall=item.get("contributes_to_overall", True)))
        return metrics
    for key, label, current_keys, total_keys, unit, counted in ALIASES:
        current = first(progress, current_keys)
        total = first(metadata, total_keys)
        if total is None:
            total = first(progress, total_keys)
        if current is None and total is None:
            continue
        if total == 0 and current in (None, 0):
            continue
        metrics.append(dict(id=key, label=label, current=max(0, current or 0), total=total,
                            unit=unit, contributes_to_overall=counted))
    return metrics


def normalize_phases(progress=None, metadata=None):
    phases = (progress or {}).get("phases", (metadata or {}).get("phases", []))
    if not isinstance(phases, list):
        return []
    return [dict(item, id=str(item.get("id", index)),
                 label=str(item.get("label", item.get("id", "阶段"))))
            for index, item in enumerate(phases) if isinstance(item, dict)]


def overall_progress(tasks, weights=None):
    """Only roots count: descendants explain work already included by a root."""
    weights = weights or {}
    current, total, known = 0.0, 0.0, True
    counted = 0
    roots = [row for row in tasks if not row.get("parent_id")]
    for row in roots:
        metrics = [m for m in row.get("metrics", []) if m.get("contributes_to_overall", True)]
        if not metrics:
            # A generic process without declared work has an unknown denominator.
            known = False
            continue
        for metric in metrics:
            counted += 1
            value, target = metric.get("current"), metric.get("total")
            weight = metric.get("weight", weights.get(metric["id"], 1))
            if not number(weight) or weight <= 0:
                weight = 1
            if not number(target) or target <= 0 or not number(value):
                known = False
                continue
            current += min(target, max(0, value)) * weight
            total += target * weight
    known = known and counted > 0 and total > 0
    return dict(current=current if counted else None, total=total if known else None,
                percent=100 * current / total if known else None, unit="计划项",
                completed_tasks=sum(row.get("status") in ("complete", "completed", "verified_resume") for row in roots),
                total_tasks=len(roots))


class ProgressEstimator:
    """Use observed throughput, require three samples, preserve unknown ETA."""
    def __init__(self):
        self.previous = {}
        self.rates = {}

    def update(self, tasks, now, active_metric_ids=None):
        active_metric_ids = active_metric_ids or {}
        root_estimates = []
        missing = False
        for row in tasks:
            if row.get("parent_id"):
                continue
            remaining_seconds = 0
            metrics = [m for m in row.get("metrics", []) if m.get("contributes_to_overall", True)]
            if not metrics:
                missing = row.get("status") not in ("complete", "completed", "verified_resume") or missing
            for metric in metrics:
                value, total = metric.get("current"), metric.get("total")
                key = (row["id"], row.get("attempt"), metric["id"])
                active = metric["id"] in active_metric_ids.get(row["id"], set())
                previous = self.previous.get(key)
                accumulated = 0
                if previous and number(value) and active and previous[2]:
                    elapsed, delta = now - previous[0], value - previous[1]
                    if 0 < elapsed <= 20:
                        accumulated = previous[3] + elapsed
                        if delta > 0:
                            self.rates.setdefault(key, deque(maxlen=12)).append(delta / accumulated)
                            accumulated = 0
                        elif delta < 0:
                            self.rates.pop(key, None)
                            accumulated = 0
                if number(value):
                    self.previous[key] = (now, value, active, accumulated)
                if not number(total) or not number(value):
                    missing = True
                elif value < total:
                    rates = self.rates.get(key, ())
                    if len(rates) < 3:
                        missing = True
                    else:
                        remaining_seconds += (total - value) / statistics.median(rates)
            root_estimates.append(remaining_seconds)
        return None if missing or not root_estimates else max(root_estimates)
