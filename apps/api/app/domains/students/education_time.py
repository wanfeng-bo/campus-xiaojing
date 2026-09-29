"""学校事实领域的纯教育时态计算；不写年度行，不产生毕业或任职变化。

调用方须传同校、同日历、同学段的完整正式规则历史，以及一个班或学生的
正式起点和例外历史。记录期间只用于审计版本选择，不授予历史查询权限。
Writer负责发布、冲突预检和取消引起的因果截止恢复；此处绝不猜测恢复。
Reader须另绑定学校/条件集合、身份、成员及来源等依赖；这里的依赖不是授权。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timezone-aware datetime is required")


def _record_period(recorded_at: datetime, superseded_at: datetime | None) -> None:
    _aware(recorded_at)
    if superseded_at is not None:
        _aware(superseded_at)
        if superseded_at <= recorded_at:
            raise ValueError("record period must be nonempty")


@dataclass(frozen=True, kw_only=True)
class CalendarRule:
    """某日历/学段规则版本；周期标签为起始公历年，适用范围[start,end)。

    每周期只出现一个边界，由覆盖该周期的规则提供。下一周期采用新日期时，
    它同时定义上一周期的结束，避免先按旧日期升年再按新日期升一次。
    """

    rule_id: str
    version: int
    start_cycle: int
    end_cycle: int | None
    month: int
    day: int
    timezone: str
    grade_ids: tuple[str, ...]
    recorded_at: datetime
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        _record_period(self.recorded_at, self.superseded_at)
        ZoneInfo(self.timezone)
        # A recurring annual boundary must exist in ordinary years as well.
        date(2001, self.month, self.day)
        if not self.rule_id or self.version < 1:
            raise ValueError("rule identity and positive version are required")
        if not 1 <= self.start_cycle <= 9998:
            raise ValueError("start cycle is outside supported calendar years")
        if self.end_cycle is not None and not self.start_cycle < self.end_cycle <= 9999:
            raise ValueError("cycle period must be nonempty")
        if not self.grade_ids or any(not item for item in self.grade_ids):
            raise ValueError("an explicit nonempty grade sequence is required")
        if len(set(self.grade_ids)) != len(self.grade_ids):
            raise ValueError("grade identities must be unique within a stage")


def clip_calendar_rule(rule: CalendarRule, *, valid_to: datetime | None) -> CalendarRule | None:
    """Intersect declared cycles with annual boundaries before a physical cutoff."""
    if valid_to is None:
        return rule
    _aware(valid_to)
    zone = ZoneInfo(rule.timezone)
    year = valid_to.astimezone(zone).year
    boundary = datetime(year, rule.month, rule.day, tzinfo=zone)
    physical_end = year if valid_to <= boundary else year + 1
    end = min(rule.end_cycle, physical_end) if rule.end_cycle is not None else physical_end
    if end <= rule.start_cycle:
        return None
    return replace(rule, end_cycle=end)


@dataclass(frozen=True, kw_only=True)
class GradeAnchor:
    """原始起点或后来正式例外；后来起点继续递进，None明确表示待安排。

    事实时间effective_from与登记期间分开。一次真实新安排取代此前起点的
    后续解释；历史纠正则关闭旧版本的记录期间并发布保留事实时间的新版本。
    """

    anchor_id: str
    version: int
    effective_from: datetime
    grade_id: str | None
    recorded_at: datetime
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        _aware(self.effective_from)
        _record_period(self.recorded_at, self.superseded_at)
        if not self.anchor_id or self.version < 1 or self.grade_id == "":
            raise ValueError("anchor identity, version and explicit grade are required")


@dataclass(frozen=True)
class GradeResolution:
    status: Literal["resolved", "unknown", "conflict"]
    cycle: int | None
    grade_id: str | None
    last_known_grade_id: str | None
    next_boundary: datetime | None
    dependencies: tuple[str, ...]
    reason: str


class _Unresolved(Exception):
    def __init__(self, reason: str, *, conflict: bool = False):
        self.reason = reason
        self.conflict = conflict


def _visible(record: CalendarRule | GradeAnchor, known_at: datetime | None) -> bool:
    if known_at is None:
        return record.superseded_at is None
    return record.recorded_at <= known_at and (
        record.superseded_at is None or known_at < record.superseded_at
    )


def _rule_for(cycle: int, rules: tuple[CalendarRule, ...]) -> CalendarRule | None:
    matches = [
        rule
        for rule in rules
        if rule.start_cycle <= cycle and (rule.end_cycle is None or cycle < rule.end_cycle)
    ]
    if len(matches) > 1:
        raise _Unresolved("rule_conflict", conflict=True)
    return matches[0] if matches else None


def _boundary(cycle: int, rule: CalendarRule) -> datetime:
    return datetime(cycle, rule.month, rule.day, tzinfo=ZoneInfo(rule.timezone))


def _cycle_at(at: datetime, rules: tuple[CalendarRule, ...]) -> tuple[int, datetime]:
    year = at.astimezone(ZoneInfo(rules[0].timezone)).year
    for cycle in (year - 1, year):
        if not 1 <= cycle <= 9998:
            continue
        rule = _rule_for(cycle, rules)
        if rule is None:
            continue
        following = _rule_for(cycle + 1, rules)
        # A genuinely ended rule never extends beyond its nominal next boundary.
        end = _boundary(cycle + 1, following or rule)
        if _boundary(cycle, rule) <= at < end:
            return cycle, end
    raise _Unresolved("rule_gap")


def resolve_grade(
    *,
    at: datetime,
    rules: tuple[CalendarRule, ...],
    anchors: tuple[GradeAnchor, ...],
    known_at: datetime | None = None,
) -> GradeResolution:
    """从权威起点和完整规则重建所问时点，不依赖上次运行或已保存年级。

    known_at=None表示现在经正式纠正后的历史，显式known_at供获准审计使用。
    下一边界只表达本计算的时间依赖，读取方还应合并其他关系及权限到期时间。
    """
    _aware(at)
    if known_at is not None:
        _aware(known_at)
    rules = tuple(rule for rule in rules if _visible(rule, known_at))
    anchors = tuple(anchor for anchor in anchors if _visible(anchor, known_at))
    dependencies = tuple(
        sorted(
            [f"rule:{rule.rule_id}:{rule.version}" for rule in rules]
            + [f"anchor:{anchor.anchor_id}:{anchor.version}" for anchor in anchors]
        )
    )
    future_origins = [anchor.effective_from for anchor in anchors if anchor.effective_from > at]
    next_boundary = min(future_origins, default=None)
    cycle: int | None = None
    last_grade: str | None = None
    try:
        if not rules:
            raise _Unresolved("rule_gap")
        if len({rule.timezone for rule in rules}) != 1:
            raise _Unresolved("timezone_conflict", conflict=True)
        cycle, calendar_end = _cycle_at(at, rules)
        next_boundary = min(calendar_end, next_boundary) if next_boundary else calendar_end
        past_origins = [anchor for anchor in anchors if anchor.effective_from <= at]
        if not past_origins:
            raise _Unresolved("missing_origin")
        latest_start = max(anchor.effective_from for anchor in past_origins)
        selected = [anchor for anchor in past_origins if anchor.effective_from == latest_start]
        if len(selected) != 1:
            raise _Unresolved("origin_conflict", conflict=True)
        origin = selected[0]
        if origin.grade_id is None:
            raise _Unresolved("explicit_unknown_origin")
        origin_cycle, _ = _cycle_at(origin.effective_from, rules)
        origin_rule = _rule_for(origin_cycle, rules)
        if origin_rule is None or origin.grade_id not in origin_rule.grade_ids:
            raise _Unresolved("grade_not_in_rule")
        last_grade = origin.grade_id
        for next_cycle in range(origin_cycle + 1, cycle + 1):
            next_rule = _rule_for(next_cycle, rules)
            if next_rule is None:
                raise _Unresolved("rule_gap")
            if last_grade not in next_rule.grade_ids:
                raise _Unresolved("grade_not_in_rule")
            index = next_rule.grade_ids.index(last_grade)
            if index + 1 == len(next_rule.grade_ids):
                raise _Unresolved("stage_exhausted")
            last_grade = next_rule.grade_ids[index + 1]
        return GradeResolution(
            "resolved",
            cycle,
            last_grade,
            last_grade,
            next_boundary,
            dependencies,
            "rule_and_origin",
        )
    except _Unresolved as error:
        return GradeResolution(
            "conflict" if error.conflict else "unknown",
            cycle,
            None,
            last_grade,
            next_boundary,
            dependencies,
            error.reason,
        )
