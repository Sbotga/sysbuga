from __future__ import annotations

# score-up % a card's skill contributes under best-case play (no combo breaks, full
# life). only score-related effect types count; everything else contributes nothing.
#   score_up                -> the effect value
#   score_up_keep           -> the raised tier (assume no break)
#   score_up_condition_life -> the highest tier (assume full life)
#   score_up_character_rank -> highest tier the card's character rank clears
#   score_up_unit_count     -> highest tier the deck's other-unit count clears
# other_member_score_up_reference_rate is deck-level (references other members), so it
# is resolved separately by reference_bonus() after every member's base is known.


def _detail_at(effect: dict, level: int) -> dict:
    details = effect.get("skillEffectDetails") or []
    for detail in details:
        if detail.get("level") == level:
            return detail
    return details[-1] if details else {}


def _effect_value(effect: dict, level: int) -> float:
    return _detail_at(effect, level).get("activateEffectValue", 0.0)


def sub_unit_enhance(skill: dict) -> tuple[str, float] | None:
    """(unit, per-member %) of a unit-scorer's sub_unit_score_up enhance, else None.

    e.g. skill 17: +10% per Vivid BAD SQUAD member -> ("street", 10).
    """
    for effect in skill.get("skillEffects", []):
        enh = effect.get("skillEnhance")
        if enh and enh.get("skillEnhanceType") == "sub_unit_score_up":
            unit = (enh.get("skillEnhanceCondition") or {}).get("unit")
            if unit:
                return unit, enh.get("activateEffectValue", 0.0)
    return None


def card_score_up(
    skill: dict,
    level: int,
    *,
    character_rank: int = 0,
    other_unit_count: int = 0,
    sub_unit_bonus: float = 0.0,
) -> float:
    flat = 0.0
    keep: list[float] = []
    life: list[float] = []
    rank_bonus = 0.0
    unit_bonus = 0.0
    for effect in skill.get("skillEffects", []):
        value = _effect_value(effect, level)
        match effect.get("skillEffectType"):
            case "score_up":
                flat += value
            case "score_up_keep":
                keep.append(value)
            case "score_up_condition_life":
                life.append(value)
            # tiered thresholds (equals_or_over); take the highest tier cleared
            case "score_up_character_rank":
                if character_rank >= effect.get("activateCharacterRank", 0):
                    rank_bonus = max(rank_bonus, value)
            case "score_up_unit_count":
                if other_unit_count >= effect.get("activateUnitCount", 0):
                    unit_bonus = max(unit_bonus, value)
    if keep:
        flat += max(keep)
    if life:
        flat += max(life)
    return flat + rank_bonus + unit_bonus + sub_unit_bonus


def reference_params(skill: dict, level: int) -> tuple[float, float] | None:
    """(rate%, cap) of an encore reference effect, or None if the skill has none."""
    for effect in skill.get("skillEffects", []):
        if effect.get("skillEffectType") == "other_member_score_up_reference_rate":
            detail = _detail_at(effect, level)
            return (
                detail.get("activateEffectValue", 0.0),
                detail.get("activateEffectValue2", 0.0),
            )
    return None


def _sub_unit_bonus(
    skill: dict, cid: int, member_unit_sets: dict[int, set[str]]
) -> float:
    """A unit-scorer's bonus: +value per OTHER member of its unit, plus one more value
    when every member is that unit (excluding self, per the skill text)."""
    sub = sub_unit_enhance(skill)
    if not sub:
        return 0.0
    unit, value = sub
    others = sum(1 for o, s in member_unit_sets.items() if o != cid and unit in s)
    all_unit = bool(member_unit_sets) and all(
        unit in s for s in member_unit_sets.values()
    )
    return value * others + (value if all_unit else 0.0)


def member_scores(
    skills: dict[int, dict],
    skill_levels: dict[int, int],
    char_ranks: dict[int, int],
    unit_counts: dict[int, int],
    member_unit_sets: dict[int, set[str]],
) -> tuple[dict[int, float], dict[int, float]]:
    """(base, max) score-up per card. base excludes cross-member references; max adds
    the card's own encore cap (its theoretical max, used when others reference it)."""
    base = {
        cid: card_score_up(
            skill,
            skill_levels.get(cid, 4),
            character_rank=char_ranks.get(cid, 0),
            other_unit_count=unit_counts.get(cid, 0),
            sub_unit_bonus=_sub_unit_bonus(skill, cid, member_unit_sets),
        )
        for cid, skill in skills.items()
    }
    max_scores = {}
    for cid in base:
        p = reference_params(skills[cid], skill_levels.get(cid, 4))
        max_scores[cid] = base[cid] + (p[1] if p else 0.0)
    return base, max_scores


def deck_isv(
    skills: dict[int, dict],
    skill_levels: dict[int, int],
    char_ranks: dict[int, int],
    unit_counts: dict[int, int],
    member_unit_sets: dict[int, set[str]],
    leader_id: int | None,
) -> tuple[dict[int, float], str]:
    """Per-card best-case score-up and the ``leader/total`` string for a deck.

    char_ranks / unit_counts / member_unit_sets are keyed by card id.
    """
    base, max_scores = member_scores(
        skills, skill_levels, char_ranks, unit_counts, member_unit_sets
    )
    per_card: dict[int, float] = {}
    for cid, b in base.items():
        p = reference_params(skills[cid], skill_levels.get(cid, 4))
        if p:
            others = [m for other, m in max_scores.items() if other != cid]
            b += reference_bonus(p[0], p[1], others)
        per_card[cid] = b
    total = sum(per_card.values())
    leader = per_card.get(leader_id, 0.0)
    return per_card, f"{leader:g}/{total:g}"


def encore_guaranteed(rate: float, cap: float, others_max: list[float]) -> bool:
    """True when every reference already caps (rate% of each member's max >= cap), so
    the encore's value is a guaranteed cap rather than a random average."""
    return bool(others_max) and all(rate / 100 * m >= cap for m in others_max)


def reference_bonus(rate: float, cap: float, others_max: list[float]) -> float:
    """Expected encore bonus: it references a *random* other member and adds rate% of
    that member's MAX score-up (each capped). Random pick -> average over the others.

    Note others_max must be each member's *maximum* score-up: an encore member is
    itself assumed to be at its own max (base + its cap), so an encore referencing
    another encore uses that member's capped max, not its base.
    """
    if not others_max:
        return 0.0
    return sum(min(cap, rate / 100 * m) for m in others_max) / len(others_max)
