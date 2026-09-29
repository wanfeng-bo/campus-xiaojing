"""Public F6 preflight language; no diagnostic, object or occupant data enters it."""

_REASONS = {
    "residence.capacity_requires_same_batch_resolution": (
        "可用床位超过房间容量，请在同一修改方案中处理相关床位。"
    ),
    "residence.room_availability_requires_same_batch_resolution": (
        "房间有效期或可用状态未覆盖相关床位，请在同一修改方案中处理。"
    ),
    "residence.bed_availability_requires_same_batch_resolution": (
        "床位有效期或可用状态未覆盖相关住宿，请在同一修改方案中处理。"
    ),
    "residence.eligibility_requires_same_batch_resolution": (
        "正式寄宿资格未覆盖完整住宿期间，请核对住校属性和有效期，并处理相关住宿。"
    ),
    "residence.student_overlap_requires_same_batch_resolution": (
        "学生在同一期间有重复住宿安排，请在同一修改方案中处理。"
    ),
    "residence.bed_overlap_requires_same_batch_resolution": (
        "床位在同一期间被重复安排，请在同一修改方案中处理。"
    ),
    "residence.bed_identity_changed": "不能直接改写床位所属房间，请核对原床位并单独登记正确床位。",
    "residence.timeline_requires_explicit_resolution": (
        "相关有效期或已有安排尚未核对完整，请补齐明确的同批处理方案。"
    ),
}


def format_residence_preflight_rejection(codes: tuple[str, ...]) -> str | None:
    """Unknown codes retain the shared rejection; known codes map to fixed text."""
    if len(codes) > 64 or any(not isinstance(code, str) for code in codes):
        return None
    unique = frozenset(codes)
    reasons = [message for code, message in _REASONS.items() if code in unique]
    if not reasons:
        return None
    if unique - _REASONS.keys():
        reasons.append("另有事项未通过核对，请检查本次对象、引用和版本。")
    return "".join((*reasons, "未生成可确认方案。"))
