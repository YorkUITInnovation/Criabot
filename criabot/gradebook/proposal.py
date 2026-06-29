from __future__ import annotations

import difflib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

from .schemas import (
    CourseActivity, GradebookCategory, GradebookSubcategory, GradebookProposal,
    GRADE_DISPLAY_TYPE_DEFAULT, GRADE_DISPLAY_TYPE_REAL, GRADE_DISPLAY_TYPE_PERCENTAGE,
    GRADE_DISPLAY_TYPE_LETTER, GRADE_DISPLAY_TYPE_REAL_PERCENTAGE, GRADE_DISPLAY_TYPE_REAL_LETTER,
    GRADE_DISPLAY_TYPE_LETTER_REAL, GRADE_DISPLAY_TYPE_PERCENTAGE_REAL,
)
from .formula_parser import FormulaParser
from .formula_resolver import FormulaResolver
from .naming_utils import activity_name_matches_lab, is_syllabus_like_name


def validate_proposal_weights(proposal: GradebookProposal) -> List[Dict[str, object]]:
    """Validate proposal weights for aggregation methods that require strict 100% total.
    
    - Methods 10, 11 (weighted means): MUST total exactly 100%
    - Methods 12 (mean+extra): MUST total 100% (excluding extra-credit categories)
    - Methods 0, 13: Allow any total (validation/warning handled elsewhere)
    """
    method = int(getattr(proposal, "aggregation_method", 13))
    # Only strict validation for weighted/mean methods
    if method not in {10, 11, 12}:
        return []

    categories = list(getattr(proposal, "categories", []) or [])
    if not categories:
        return []

    weighted_categories = categories
    if method == 12:
        weighted_categories = [cat for cat in categories if not bool(getattr(cat, "extra_credit", False))]

    total_weight = sum(float(getattr(cat, "weight", 0.0)) for cat in weighted_categories)
    errors: List[Dict[str, object]] = []
    if abs(total_weight - 100.0) > 0.01:
        errors.append(
            {
                "path": ["proposal"],
                "aggregation_method": method,
                "total_weight": total_weight,
                "expected": 100.0,
                "details": (
                    f"Weight sum for proposal categories is {total_weight:.1f}%, expected 100.0% "
                    f"for aggregation method {method}. Please adjust category weights to total 100%."
                ),
            }
        )

    return errors


NOT_GRADED_MAPPING_CATEGORIES = frozenset({"__not_graded__", "not graded", ""})
NOT_GRADED_CATEGORY = "__not_graded__"


def _find_subcategory_in_category(
    category: GradebookCategory,
    subcategory_name: str,
) -> GradebookSubcategory | None:
    """Return a subcategory on *category* whose name matches *subcategory_name*."""
    key = str(subcategory_name or "").strip().lower()
    if not key:
        return None
    for sub in (category.subcategories or []):
        if str(sub.name).strip().lower() == key:
            return sub
    return None


def _find_item_location_in_category(
    category: GradebookCategory,
    item_key: str,
) -> Tuple[str, GradebookSubcategory | None]:
    """Return ('parent', None), ('sub', sub), or ('none', None) for a case-insensitive item key."""
    key = str(item_key or "").strip().lower()
    if not key:
        return "none", None
    for item in (category.items or []):
        if str(item).strip().lower() == key:
            return "parent", None
    for sub in (category.subcategories or []):
        for item in (sub.items or []):
            if str(item).strip().lower() == key:
                return "sub", sub
    return "none", None


def _remove_all_item_instances_from_category_tree(
    category: GradebookCategory,
    item_key: str,
) -> str | None:
    """Remove every parent/subcategory copy of *item_key*; return the first canonical name removed."""
    key = str(item_key or "").strip().lower()
    if not key:
        return None

    canonical: str | None = None

    if category.items:
        kept_parent: List[str] = []
        for item in category.items:
            if str(item).strip().lower() == key:
                canonical = canonical or str(item).strip()
                if category.item_weights:
                    category.item_weights.pop(item, None)
            else:
                kept_parent.append(item)
        category.items = kept_parent

    for sub in (category.subcategories or []):
        if not sub.items:
            continue
        kept_sub: List[str] = []
        for item in sub.items:
            if str(item).strip().lower() == key:
                canonical = canonical or str(item).strip()
                if sub.item_weights:
                    sub.item_weights.pop(item, None)
            else:
                kept_sub.append(item)
        sub.items = kept_sub

    return canonical


def sync_proposal_items_from_mapping(
    proposal: GradebookProposal | None,
    confirmed_mapping: List[dict] | None,
    course_activities: List[CourseActivity] | None = None,
) -> bool:
    """Merge mapped grade items into proposal categories. Returns True when proposal changed."""
    if proposal is None or not confirmed_mapping:
        return False

    activity_by_gid: Dict[int, CourseActivity] = {}
    for activity in course_activities or []:
        grade_item_id = getattr(activity, "grade_item_id", None)
        if grade_item_id is not None and str(grade_item_id).strip() != "":
            try:
                activity_by_gid[int(grade_item_id)] = activity
            except (TypeError, ValueError):
                continue

    categories_by_key = {
        str(category.name).strip().lower(): category
        for category in (proposal.categories or [])
        if str(category.name).strip()
    }

    changed = False
    for row in confirmed_mapping:
        if not isinstance(row, dict):
            continue

        category_name = str(row.get("category") or row.get("confirmed_category") or "").strip()
        if not category_name or category_name.lower() in NOT_GRADED_MAPPING_CATEGORIES:
            continue

        target = categories_by_key.get(category_name.lower())
        if target is None:
            continue

        item_name = str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
        grade_item_id = row.get("grade_item_id")
        if not item_name and grade_item_id is not None and str(grade_item_id).strip() != "":
            try:
                activity = activity_by_gid.get(int(grade_item_id))
            except (TypeError, ValueError):
                activity = None
            if activity is not None and getattr(activity, "name", None):
                item_name = str(activity.name).strip()

        if not item_name:
            continue

        subcategory_name = str(
            row.get("subcategory")
            or row.get("confirmed_subcategory")
            or row.get("suggested_subcategory")
            or ""
        ).strip()
        item_key = item_name.strip().lower()

        location, current_sub = _find_item_location_in_category(target, item_key)
        target_sub = (
            _find_subcategory_in_category(target, subcategory_name)
            if subcategory_name
            else None
        )

        if target_sub is not None:
            if location == "sub" and current_sub is target_sub:
                stale_in_parent = any(
                    str(item).strip().lower() == item_key
                    for item in (target.items or [])
                )
                if stale_in_parent:
                    target.items = [
                        item
                        for item in (target.items or [])
                        if str(item).strip().lower() != item_key
                    ]
                    changed = True
                continue
        elif location != "none":
            continue

        canonical_name = _remove_all_item_instances_from_category_tree(target, item_key)
        place_name = canonical_name or item_name

        if target_sub is not None:
            if target_sub.items is None:
                target_sub.items = []
            existing_sub = {str(i).strip().lower() for i in target_sub.items}
            if item_key not in existing_sub:
                target_sub.items.append(place_name)
            changed = True
            continue

        if target.items is None:
            target.items = []

        existing = {str(item).strip().lower() for item in target.items}
        if item_key not in existing:
            target.items.append(place_name)
        changed = True

    return changed


def sync_proposal_not_graded_from_confirmed_mapping(
    proposal: GradebookProposal | None,
    confirmed_mapping: List[dict] | None,
    course_activities: List[CourseActivity] | None = None,
) -> bool:
    """Sync proposal.not_graded_items from mapping UI rows marked Not graded."""
    if proposal is None or not confirmed_mapping:
        return False

    generator = ProposalGenerator()
    changed = False
    tracked_not_graded = {
        str(name).strip().lower()
        for name in (proposal.not_graded_items or [])
        if str(name).strip()
    }

    for row in confirmed_mapping:
        if not isinstance(row, dict):
            continue

        category_name = str(row.get("category") or row.get("confirmed_category") or "").strip()
        category_key = category_name.lower()
        item_name = str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
        if not item_name:
            continue

        name_key = item_name.lower()
        is_not_graded_row = (
            category_key in NOT_GRADED_MAPPING_CATEGORIES
            or category_key == NOT_GRADED_CATEGORY
            or bool(row.get("not_graded"))
        )

        if is_not_graded_row:
            if not generator._is_moodle_activity_item(item_name, course_activities):
                continue
            for category in proposal.categories or []:
                if generator._remove_item_from_category_tree(category, item_name):
                    changed = True
            if name_key not in tracked_not_graded:
                if proposal.not_graded_items is None:
                    proposal.not_graded_items = []
                proposal.not_graded_items.append(item_name)
                tracked_not_graded.add(name_key)
                changed = True
            continue

        if name_key in tracked_not_graded:
            generator._remove_from_not_graded_items(proposal, item_name)
            tracked_not_graded.discard(name_key)
            changed = True

    return changed


def sync_mapping_from_proposal_manual_items(
    proposal: GradebookProposal | None,
    content_mapping: dict | None,
    course_activities: List[CourseActivity] | None = None,
) -> bool:
    """Add proposal-only manual grade items to graded_activities for mapping/finalize."""
    if proposal is None:
        return False

    mapping_payload = content_mapping if isinstance(content_mapping, dict) else {}
    graded_activities = list(mapping_payload.get("graded_activities") or [])
    if mapping_payload is not content_mapping:
        mapping_payload = dict(mapping_payload)

    canonical_names: set[str] = set()
    for row in graded_activities:
        if not isinstance(row, dict):
            continue
        name_key = str(row.get("activity_name") or row.get("grade_item_name") or "").strip().lower()
        if not name_key:
            continue
        source = str(row.get("item_source") or "").strip().lower()
        itemtype = str(row.get("itemtype") or "").strip().lower()
        has_cmid = row.get("moodle_cmid") not in (None, "", 0)
        has_module = bool(str(row.get("module_type") or row.get("module") or "").strip())
        if source not in {"proposal_manual", "proposal"} and (itemtype == "mod" or has_cmid or has_module):
            canonical_names.add(name_key)

    if canonical_names:
        filtered: List[dict] = []
        for row in graded_activities:
            if not isinstance(row, dict):
                continue
            name_key = str(row.get("activity_name") or row.get("grade_item_name") or "").strip().lower()
            source = str(row.get("item_source") or "").strip().lower()
            if name_key and name_key in canonical_names and source in {"proposal_manual", "proposal"}:
                continue
            filtered.append(row)
        if len(filtered) != len(graded_activities):
            graded_activities = filtered
            mapping_payload["graded_activities"] = graded_activities
            if content_mapping is not None:
                content_mapping["graded_activities"] = graded_activities

    activity_by_gid: Dict[int, CourseActivity] = {}
    activity_by_name: Dict[str, CourseActivity] = {}
    for activity in course_activities or []:
        grade_item_id = getattr(activity, "grade_item_id", None)
        if grade_item_id is not None and str(grade_item_id).strip() != "":
            try:
                activity_by_gid[int(grade_item_id)] = activity
            except (TypeError, ValueError):
                pass
        name_key = str(getattr(activity, "name", "") or "").strip().lower()
        if name_key and name_key not in activity_by_name:
            activity_by_name[name_key] = activity

    existing_names = {
        str(row.get("activity_name") or row.get("grade_item_name") or "").strip().lower()
        for row in graded_activities
        if isinstance(row, dict) and str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
    }

    changed = False
    for category in proposal.categories or []:
        category_name = str(category.name or "").strip()
        if not category_name:
            continue

        for raw_item in category.items or []:
            item_name = str(raw_item or "").strip()
            if not item_name:
                continue

            item_key = item_name.lower()
            if item_key in existing_names:
                continue

            matched_activity = activity_by_name.get(item_key)
            if matched_activity is not None:
                # Name already belongs to a Moodle activity (assign/quiz/etc.) — not a proposal-only manual.
                module = str(getattr(matched_activity, "module", None) or "").strip()
                cmid = getattr(matched_activity, "cmid", None)
                if module or (cmid is not None and str(cmid).strip() not in {"", "0"}):
                    continue

            grade_item_id = None
            moodle_cmid = None
            module = None
            if matched_activity is not None:
                grade_item_id = getattr(matched_activity, "grade_item_id", None)
                moodle_cmid = getattr(matched_activity, "cmid", None)
                module = getattr(matched_activity, "module", None)

            graded_activities.append(
                {
                    "moodle_cmid": moodle_cmid,
                    "grade_item_id": grade_item_id,
                    "module_type": module,
                    "itemtype": "manual",
                    "activity_name": item_name,
                    "activity_key": f"proposal-manual:{category_name.lower()}:{item_key}",
                    "item_source": "proposal_manual",
                    "suggested_category": category_name,
                    "confirmed_category": category_name,
                    "finalized": False,
                    "confidence": 1.0,
                    "reasoning": "Manual grade item from proposal (conversation or mapping).",
                    "mapping_method": "proposal_manual",
                }
            )
            existing_names.add(item_key)
            changed = True

    if changed:
        mapping_payload["graded_activities"] = graded_activities
        if content_mapping is not None:
            content_mapping["graded_activities"] = graded_activities

    return changed


def _collapse_subcategory_label_for_mapping(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (label or "").strip().lower())


def subcategory_labels_match_for_mapping(candidate: str, query: str) -> bool:
    """Fuzzy subcategory label match used when syncing content mapping after renames."""
    candidate_norm = re.sub(r"\s+", " ", (candidate or "").strip().lower())
    query_norm = re.sub(r"\s+", " ", (query or "").strip().lower())
    if not candidate_norm or not query_norm:
        return False
    if candidate_norm == query_norm:
        return True

    candidate_collapsed = _collapse_subcategory_label_for_mapping(candidate)
    query_collapsed = _collapse_subcategory_label_for_mapping(query)
    if candidate_collapsed and candidate_collapsed == query_collapsed:
        return True

    def _variants(name: str) -> set[str]:
        variants = {name}
        if name.endswith("ies") and len(name) > 3:
            variants.add(name[:-3] + "y")
        if name.endswith("s") and len(name) > 1:
            variants.add(name[:-1])
        if name.endswith("y") and len(name) > 1:
            variants.add(name[:-1] + "ies")
        return variants

    if _variants(candidate_norm) & _variants(query_norm):
        return True
    if difflib.SequenceMatcher(None, candidate_norm, query_norm).ratio() >= 0.84:
        return True
    return False


def _category_item_names_union(category: GradebookCategory) -> set[str]:
    names = {str(item).strip().lower() for item in (category.items or []) if str(item).strip()}
    for sub in category.subcategories or []:
        names.update(
            str(item).strip().lower()
            for item in (sub.items or [])
            if str(item).strip()
        )
    return names


def _proposal_tracked_item_names(proposal: GradebookProposal | None) -> set[str]:
    """All grade items tracked in the proposal tree plus explicit not-graded activities."""
    if proposal is None:
        return set()
    names: set[str] = set()
    for category in proposal.categories or []:
        names.update(_category_item_names_union(category))
    names.update(
        str(item).strip().lower()
        for item in (proposal.not_graded_items or [])
        if str(item).strip()
    )
    return names


def _set_row_not_graded(row: dict) -> None:
    row["suggested_category"] = NOT_GRADED_CATEGORY
    row["confirmed_category"] = NOT_GRADED_CATEGORY
    _set_row_subcategory(row, "")
    row["not_graded"] = True


def snapshot_subcategories(proposal: GradebookProposal | None) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if proposal is None:
        return out
    for category in proposal.categories or []:
        parent = str(category.name or "").strip()
        if not parent:
            continue
        out[parent.lower()] = [
            str(sub.name).strip()
            for sub in (category.subcategories or [])
            if str(sub.name).strip()
        ]
    return out


def infer_subcategory_renames(
    before: Dict[str, List[str]],
    after: Dict[str, List[str]],
) -> List[Tuple[str, str, str]]:
    """Infer subcategory renames as (parent_key_lower, old_name, new_name)."""
    renames: List[Tuple[str, str, str]] = []
    seen: set[Tuple[str, str]] = set()

    for parent_key in set(before.keys()) | set(after.keys()):
        old_names = before.get(parent_key, [])
        new_names = after.get(parent_key, [])
        old_by_lower = {name.lower(): name for name in old_names}
        new_by_lower = {name.lower(): name for name in new_names}
        removed = [old_by_lower[key] for key in old_by_lower if key not in new_by_lower]
        added = [new_by_lower[key] for key in new_by_lower if key not in old_by_lower]
        if not removed or not added:
            continue

        matched_added: set[str] = set()
        for old_name in removed:
            for new_name in added:
                if new_name in matched_added:
                    continue
                if (
                    len(removed) == 1
                    and len(added) == 1
                ) or subcategory_labels_match_for_mapping(old_name, new_name):
                    pair_key = (parent_key, old_name.lower())
                    if pair_key in seen:
                        continue
                    renames.append((parent_key, old_name, new_name))
                    seen.add(pair_key)
                    matched_added.add(new_name)
                    break

    return renames


def infer_subcategory_removals(
    before: Dict[str, List[str]],
    after: Dict[str, List[str]],
    renames: List[Tuple[str, str, str]] | None = None,
) -> List[Tuple[str, str]]:
    """Infer removed subcategories as (parent_key_lower, removed_name)."""
    renamed_old = {
        (parent_key, old_name.strip().lower())
        for parent_key, old_name, _new_name in (renames or [])
    }
    removals: List[Tuple[str, str]] = []

    for parent_key in set(before.keys()) | set(after.keys()):
        old_names = before.get(parent_key, [])
        new_names = after.get(parent_key, [])
        old_by_lower = {name.lower(): name for name in old_names}
        new_by_lower = {name.lower(): name for name in new_names}
        for key, old_name in old_by_lower.items():
            if key in new_by_lower:
                continue
            if (parent_key, key) in renamed_old:
                continue
            if any(subcategory_labels_match_for_mapping(old_name, added) for added in new_names):
                continue
            removals.append((parent_key, old_name))

    return removals


def is_subcategory_only_proposal_change(
    before: GradebookProposal | None,
    after: GradebookProposal | None,
) -> bool:
    """True when only subcategory labels/weights changed (top-level categories stable)."""
    if before is None or after is None:
        return False
    if snapshot_subcategories(before) == snapshot_subcategories(after):
        return False
    return not should_invalidate_content_mapping(before, after)


def should_invalidate_content_mapping(
    before: GradebookProposal | None,
    after: GradebookProposal | None,
) -> bool:
    """True when content mapping should be rebuilt (category add/remove or grade-item structure change)."""
    if before is None or after is None:
        return True

    before_by = {
        str(category.name).strip().lower(): category
        for category in (before.categories or [])
        if str(category.name).strip()
    }
    after_by = {
        str(category.name).strip().lower(): category
        for category in (after.categories or [])
        if str(category.name).strip()
    }
    if set(before_by) != set(after_by):
        return True

    if _proposal_tracked_item_names(before) != _proposal_tracked_item_names(after):
        return True

    for key in before_by:
        before_cat = before_by[key]
        after_cat = after_by[key]
        if (before_cat.calculation_formula or None) != (after_cat.calculation_formula or None):
            return True
        if dict(before_cat.item_weights or {}) != dict(after_cat.item_weights or {}):
            return True

    return False


def _row_subcategory_label(row: dict) -> str:
    return str(
        row.get("confirmed_subcategory")
        or row.get("suggested_subcategory")
        or row.get("subcategory")
        or ""
    ).strip()


def _set_row_subcategory(row: dict, subcategory_name: str) -> None:
    value = str(subcategory_name or "").strip()
    row["suggested_subcategory"] = value
    row["confirmed_subcategory"] = value
    row["subcategory"] = value


def sync_content_mapping_with_proposal_subcategory_changes(
    content_mapping: dict,
    proposal: GradebookProposal | None,
    renames: List[Tuple[str, str, str]] | None = None,
    removals: List[Tuple[str, str]] | None = None,
) -> bool:
    """Update mapping rows when subcategories are renamed or removed."""
    if proposal is None or not isinstance(content_mapping, dict):
        return False

    graded_activities = content_mapping.get("graded_activities") or []
    if not graded_activities:
        return False

    rename_lookup: Dict[Tuple[str, str], str] = {}
    for parent_key, old_name, new_name in (renames or []):
        rename_lookup[(parent_key, old_name.strip().lower())] = new_name

    removal_lookup = {
        (parent_key, removed.strip().lower())
        for parent_key, removed in (removals or [])
    }

    valid_subs_by_parent: Dict[str, Dict[str, str]] = {}
    for category in proposal.categories or []:
        parent_key = str(category.name or "").strip().lower()
        if not parent_key:
            continue
        valid_subs_by_parent[parent_key] = {
            str(sub.name).strip().lower(): str(sub.name).strip()
            for sub in (category.subcategories or [])
            if str(sub.name).strip()
        }

    changed = False
    for row in graded_activities:
        if not isinstance(row, dict):
            continue

        parent_name = str(row.get("confirmed_category") or row.get("suggested_category") or "").strip()
        if not parent_name:
            continue
        parent_key = parent_name.lower()
        sub_name = _row_subcategory_label(row)
        if not sub_name:
            continue

        sub_key = sub_name.lower()
        new_sub_name: str | None = None

        direct = rename_lookup.get((parent_key, sub_key))
        if direct:
            new_sub_name = direct
        else:
            for (rename_parent, old_key), renamed in rename_lookup.items():
                if rename_parent == parent_key and subcategory_labels_match_for_mapping(sub_name, old_key):
                    new_sub_name = renamed
                    break

        if new_sub_name is not None:
            _set_row_subcategory(row, new_sub_name)
            changed = True
            continue

        if (parent_key, sub_key) in removal_lookup or any(
            parent == parent_key and subcategory_labels_match_for_mapping(sub_name, removed)
            for parent, removed in removal_lookup
        ):
            _set_row_subcategory(row, "")
            changed = True
            continue

        valid_subs = valid_subs_by_parent.get(parent_key, {})
        if sub_key not in valid_subs:
            resolved = None
            for candidate_key, candidate_name in valid_subs.items():
                if subcategory_labels_match_for_mapping(sub_name, candidate_key):
                    resolved = candidate_name
                    break
            if resolved:
                _set_row_subcategory(row, resolved)
                changed = True
            else:
                _set_row_subcategory(row, "")
                changed = True

    if changed:
        content_mapping["graded_activities"] = graded_activities

    return changed


def _valid_subcategories_by_parent(proposal: GradebookProposal | None) -> Dict[str, Dict[str, str]]:
    valid_subs_by_parent: Dict[str, Dict[str, str]] = {}
    for category in (proposal.categories or []) if proposal else []:
        parent_key = str(category.name or "").strip().lower()
        if not parent_key:
            continue
        valid_subs_by_parent[parent_key] = {
            str(sub.name).strip().lower(): str(sub.name).strip()
            for sub in (category.subcategories or [])
            if str(sub.name).strip()
        }
    return valid_subs_by_parent


def _canonicalize_subcategory_for_parent(
    subcategory: str,
    parent_name: str,
    valid_subs_by_parent: Dict[str, Dict[str, str]],
) -> str:
    sub_name = str(subcategory or "").strip()
    if not sub_name:
        return ""
    parent_key = str(parent_name or "").strip().lower()
    valid_subs = valid_subs_by_parent.get(parent_key, {})
    sub_key = sub_name.lower()
    if sub_key in valid_subs:
        return valid_subs[sub_key]
    for candidate_key, candidate_name in valid_subs.items():
        if subcategory_labels_match_for_mapping(sub_name, candidate_key):
            return candidate_name
    return ""


def _normalize_mapping_cmid(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _match_confirmed_row_to_mapping_item(
    confirmed: dict,
    graded_activities: List[dict],
    activity_by_cmid: Dict[int, dict],
    activity_by_grade_item_id: Dict[int, dict],
) -> dict | None:
    confirmed_grade_item_id = confirmed.get("grade_item_id")
    if confirmed_grade_item_id is not None and str(confirmed_grade_item_id).strip() != "":
        try:
            existing = activity_by_grade_item_id.get(int(confirmed_grade_item_id))
            if existing is not None:
                return existing
        except (TypeError, ValueError):
            pass

    confirmed_cmid = _normalize_mapping_cmid(confirmed.get("moodle_cmid"))
    if confirmed_cmid is not None:
        existing = activity_by_cmid.get(confirmed_cmid)
        if existing is not None:
            return existing

    confirmed_name = str(
        confirmed.get("activity_name") or confirmed.get("grade_item_name") or ""
    ).strip().lower()
    if confirmed_name:
        for item in graded_activities:
            if str(item.get("activity_name") or "").strip().lower() == confirmed_name:
                return item
    return None


def sync_confirmed_mapping_into_content_mapping(
    content_mapping: dict,
    confirmed_mapping: List[dict],
    proposal: GradebookProposal | None,
) -> bool:
    """Merge UI-confirmed mapping rows (incl. subcategory) into session content_mapping."""
    if not isinstance(content_mapping, dict) or not confirmed_mapping:
        return False

    graded_activities = list(content_mapping.get("graded_activities") or [])
    if not graded_activities:
        return False

    activity_by_cmid = {}
    for item in graded_activities:
        cmid = _normalize_mapping_cmid(item.get("moodle_cmid"))
        if cmid is not None:
            activity_by_cmid[cmid] = item
    activity_by_grade_item_id = {
        int(item.get("grade_item_id")): item
        for item in graded_activities
        if item.get("grade_item_id") is not None
        and str(item.get("grade_item_id")).strip() != ""
    }
    valid_subs_by_parent = _valid_subcategories_by_parent(proposal)
    changed = False

    for confirmed in confirmed_mapping:
        if not isinstance(confirmed, dict):
            continue
        existing = _match_confirmed_row_to_mapping_item(
            confirmed,
            graded_activities,
            activity_by_cmid,
            activity_by_grade_item_id,
        )
        if existing is None:
            continue

        category_name = str(
            confirmed.get("category")
            or confirmed.get("confirmed_category")
            or existing.get("confirmed_category")
            or existing.get("suggested_category")
            or ""
        ).strip()
        category_key = category_name.lower()
        previous_category = str(existing.get("confirmed_category") or existing.get("suggested_category") or "")
        previous_subcategory = _row_subcategory_label(existing)

        if category_key in NOT_GRADED_MAPPING_CATEGORIES or category_key == NOT_GRADED_CATEGORY:
            if previous_category != NOT_GRADED_CATEGORY or previous_subcategory:
                _set_row_not_graded(existing)
                changed = True
            continue

        raw_subcategory = str(
            confirmed.get("subcategory")
            or confirmed.get("confirmed_subcategory")
            or ""
        ).strip()
        parent_name = str(
            existing.get("confirmed_category") or existing.get("suggested_category") or category_name
        ).strip()
        canonical_sub = _canonicalize_subcategory_for_parent(
            raw_subcategory,
            parent_name,
            valid_subs_by_parent,
        ) if raw_subcategory else ""

        if category_name:
            existing["suggested_category"] = category_name
            existing["confirmed_category"] = category_name
        _set_row_subcategory(existing, canonical_sub)

        if (
            (category_name and previous_category != category_name)
            or previous_subcategory != canonical_sub
        ):
            changed = True

    if changed:
        content_mapping["graded_activities"] = graded_activities
    return changed


def _find_mapping_row_by_activity_query(graded_activities: List[dict], item_query: str) -> dict | None:
    query_lower = str(item_query or "").strip().lower()
    if not query_lower:
        return None

    best_row: dict | None = None
    best_score = 0.0
    for row in graded_activities:
        if not isinstance(row, dict):
            continue
        activity_name = str(row.get("activity_name") or "").strip().lower()
        if not activity_name:
            continue
        if query_lower == activity_name:
            return row
        if query_lower in activity_name or activity_name in query_lower:
            score = min(len(query_lower), len(activity_name)) / max(len(query_lower), len(activity_name))
            if score > best_score:
                best_score = score
                best_row = row
    return best_row if best_score >= 0.5 else None


def _extract_scoped_parent_label(prompt: str) -> str:
    match = re.search(
        r"\b(?:in|under|within)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\s*(?:[.,]|$)",
        prompt,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _resolve_subcategory_target(
    proposal: GradebookProposal,
    parent_name: str,
    target_hint: str,
) -> str:
    parent_key = str(parent_name or "").strip().lower()
    target_hint = str(target_hint or "").strip()
    if not parent_key or not target_hint:
        return ""

    valid_subs = _valid_subcategories_by_parent(proposal).get(parent_key, {})
    target_key = target_hint.lower()
    if target_key in valid_subs:
        return valid_subs[target_key]
    for candidate_key, candidate_name in valid_subs.items():
        if subcategory_labels_match_for_mapping(target_hint, candidate_key):
            return candidate_name
    return ""


def sync_mapping_subcategories_from_proposal(
    content_mapping: dict,
    proposal: GradebookProposal,
) -> bool:
    """Align mapping rows with subcategory item placement in the proposal tree."""
    if not isinstance(content_mapping, dict) or proposal is None:
        return False

    graded_activities = list(content_mapping.get("graded_activities") or [])
    if not graded_activities:
        return False

    changed = False
    for row in graded_activities:
        if not isinstance(row, dict):
            continue
        activity_name = str(row.get("activity_name") or "").strip()
        if not activity_name:
            continue

        matched_parent = ""
        matched_sub = ""
        valid_subs_by_parent = _valid_subcategories_by_parent(proposal)
        for category in proposal.categories or []:
            for sub in category.subcategories or []:
                if activity_name in (sub.items or []):
                    matched_parent = category.name
                    matched_sub = sub.name
                    break
            if matched_parent:
                break
            if activity_name in (category.items or []):
                matched_parent = category.name
                # Keep mapping subcategory when the item is still listed on the parent
                # but mapping already points at a valid child subcategory (e.g. after accept).
                matched_sub = _canonicalize_subcategory_for_parent(
                    _row_subcategory_label(row),
                    category.name,
                    valid_subs_by_parent,
                )
                break

        if not matched_parent:
            continue

        row_parent = str(row.get("confirmed_category") or row.get("suggested_category") or "").strip()
        row_sub = _row_subcategory_label(row)
        if row_parent != matched_parent:
            row["suggested_category"] = matched_parent
            row["confirmed_category"] = matched_parent
            changed = True
        if row_sub != matched_sub:
            _set_row_subcategory(row, matched_sub)
            changed = True

    if changed:
        content_mapping["graded_activities"] = graded_activities
    return changed


def sync_mapping_not_graded_items(
    content_mapping: dict,
    proposal: GradebookProposal,
    course_activities: List[CourseActivity] | None = None,
) -> bool:
    """Align mapping rows with proposal.not_graded_items and create rows when missing."""
    if not isinstance(content_mapping, dict) or proposal is None:
        return False

    graded_activities = list(content_mapping.get("graded_activities") or [])
    not_graded_by_key = {
        str(name).strip().lower(): str(name).strip()
        for name in (proposal.not_graded_items or [])
        if str(name).strip()
    }
    if not not_graded_by_key and not graded_activities:
        return False

    graded_in_tree = set()
    for category in proposal.categories or []:
        graded_in_tree.update(_category_item_names_union(category))

    changed = False
    existing_keys: set[str] = set()
    for row in graded_activities:
        if not isinstance(row, dict):
            continue
        activity_name = str(row.get("activity_name") or row.get("grade_item_name") or "").strip()
        if not activity_name:
            continue
        name_key = activity_name.lower()
        existing_keys.add(name_key)

        if name_key in graded_in_tree:
            mapped = str(row.get("confirmed_category") or row.get("suggested_category") or "").strip()
            if mapped == NOT_GRADED_CATEGORY:
                row.pop("not_graded", None)
                changed = True
            continue

        if name_key in not_graded_by_key:
            mapped = str(row.get("confirmed_category") or row.get("suggested_category") or "").strip()
            if mapped != NOT_GRADED_CATEGORY:
                _set_row_not_graded(row)
                changed = True

    generator = ProposalGenerator()
    for name_key, canonical_name in not_graded_by_key.items():
        if name_key in graded_in_tree or name_key in existing_keys:
            continue
        activity = generator._match_course_activity(canonical_name, course_activities)
        if activity is None:
            continue
        graded_activities.append(
            {
                "moodle_cmid": activity.cmid,
                "grade_item_id": activity.grade_item_id,
                "module_type": activity.module,
                "itemtype": activity.itemtype or "mod",
                "activity_name": activity.name,
                "activity_key": f"cmid:{activity.cmid}" if activity.cmid is not None else activity.name,
                "item_source": "activity",
                "suggested_category": NOT_GRADED_CATEGORY,
                "confirmed_category": NOT_GRADED_CATEGORY,
                "suggested_subcategory": "",
                "confirmed_subcategory": "",
                "subcategory": "",
                "finalized": False,
                "confidence": 1.0,
                "reasoning": "Marked not graded per instructor instruction.",
                "mapping_method": "proposal_not_graded",
                "not_graded": True,
            }
        )
        changed = True

    if changed:
        content_mapping["graded_activities"] = graded_activities
    return changed


def apply_mapping_subcategory_operations(
    content_mapping: dict,
    proposal: GradebookProposal,
    prompt: str,
    course_activities: List[CourseActivity] | None = None,
) -> bool:
    """Keep mapping rows aligned after proposal item placement changes."""
    _ = prompt
    changed = sync_mapping_subcategories_from_proposal(content_mapping, proposal)
    changed = sync_mapping_not_graded_items(content_mapping, proposal, course_activities) or changed
    return changed


class ProposalGenerator:
    DEFAULT_CATEGORIES = [
        ("Assignments", 25.0),
        ("Labs", 15.0),
        ("Midterm", 30.0),
        ("Final Exam", 30.0),
    ]
    CATEGORY_ALIASES = {
        "assignment": "Assignments",
        "assignments": "Assignments",
        "homework": "Assignments",
        "hw": "Assignments",
        "lab": "Labs",
        "labs": "Labs",
        "lap": "Labs",
        "midterm": "Midterm",
        "mid term": "Midterm",
        "exam": "Final Exam",
        "final": "Final Exam",
        "final exam": "Final Exam",
        "quiz": "Quizzes",
        "quizzes": "Quizzes",
        "quize": "Quizzes",
        "quizes": "Quizzes",
    }
    CATEGORY_ALIAS_PATTERNS = (
        (re.compile(r"\b(assign(?:ment)?s?|home\s*work|hw)\b", flags=re.IGNORECASE), "Assignments"),
        (re.compile(r"\b(quiz(?:zes)?|quize(?:s)?)\b", flags=re.IGNORECASE), "Quizzes"),
        (re.compile(r"\b(lab(?:s)?|lap(?:s)?|laboratory)\b", flags=re.IGNORECASE), "Labs"),
        (re.compile(r"\b(mid\s*term|midterm)\b", flags=re.IGNORECASE), "Midterm"),
        (re.compile(r"\b(final(?:\s*exam)?|exam)\b", flags=re.IGNORECASE), "Final Exam"),
    )

    def __init__(self) -> None:
        self._category_aliases = dict(self.CATEGORY_ALIASES)
        self._category_alias_patterns = list(self.CATEGORY_ALIAS_PATTERNS)
        self._load_aliases_from_env()

    @staticmethod
    def _iter_tree_children(node: object) -> List[dict]:
        if not isinstance(node, dict):
            return []
        children = node.get("children")
        if isinstance(children, dict):
            def _child_sort_key(entry: tuple[object, object]) -> tuple[int, object]:
                raw_key = entry[0]
                try:
                    return (0, int(raw_key))
                except (TypeError, ValueError):
                    return (1, str(raw_key))

            return [child for _, child in sorted(children.items(), key=_child_sort_key) if isinstance(child, dict)]
        if isinstance(children, list):
            return [child for child in children if isinstance(child, dict)]
        return []

    @staticmethod
    def _to_float(value: object) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: object) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _extract_baseline_weight_percent(self, node: dict, aggregation_method: int) -> float:
        coef = node.get("aggregationcoef")
        coef2 = node.get("aggregationcoef2")
        weight_override = bool(node.get("weightoverride"))

        try:
            coef = float(coef) if coef is not None else None
        except (TypeError, ValueError):
            coef = None

        try:
            coef2 = float(coef2) if coef2 is not None else None
        except (TypeError, ValueError):
            coef2 = None

        if aggregation_method in {10, 11} and coef is not None:
            return max(0.0, min(100.0, coef))

        if aggregation_method == 13:
            if coef2 is not None and (weight_override or coef2 > 0.0):
                return max(0.0, min(100.0, coef2 * 100.0))
            if coef is not None:
                if 0.0 <= coef <= 1.0:
                    return max(0.0, min(100.0, coef * 100.0))
                return max(0.0, min(100.0, coef))

        # Some Moodle baselines place category weights on the synthetic
        # category-total item (itemtype='category') rather than the category node.
        total_item = self._find_baseline_category_total_item(node)
        if isinstance(total_item, dict):
            item_pct = self._extract_baseline_item_weight_percent(total_item, aggregation_method)
            if item_pct is not None:
                return max(0.0, min(100.0, float(item_pct)))

        return 0.0

    def _collect_baseline_item_names(self, node: dict) -> List[str]:
        """Collect grade items that are direct children of this category node only."""
        names: List[str] = []
        for child in self._iter_tree_children(node):
            child_type = str(child.get("type") or "").strip().lower()
            if child_type == "category":
                continue

            # Skip synthetic category-total rows; only keep real gradable items.
            child_item_type = str(child.get("itemtype") or "").strip().lower()
            if child_item_type == "category":
                continue

            name = str(child.get("name") or "").strip()
            if name:
                names.append(name)
        return list(dict.fromkeys(names))

    def _extract_baseline_category_grade_max(self, node: dict) -> Optional[float]:
        direct = self._to_float(node.get("grademax"))
        if direct is not None and direct > 0.0:
            return direct

        # Moodle often stores category-total point maxima on the synthetic child item.
        for child in self._iter_tree_children(node):
            child_item_type = str(child.get("itemtype") or "").strip().lower()
            if child_item_type != "category":
                continue
            child_max = self._to_float(child.get("grademax"))
            if child_max is not None and child_max > 0.0:
                return child_max
        return None

    def _find_baseline_category_total_item(self, node: dict) -> Optional[dict]:
        for child in self._iter_tree_children(node):
            if str(child.get("itemtype") or "").strip().lower() == "category":
                return child
        return None

    def _extract_baseline_item_weight_percent(self, item_node: dict, aggregation_method: int) -> Optional[float]:
        coef = self._to_float(item_node.get("aggregationcoef"))
        coef2 = self._to_float(item_node.get("aggregationcoef2"))
        weight_override = bool(item_node.get("weightoverride"))

        if aggregation_method in {10, 11}:
            if coef is None:
                return None
            if 0.0 <= coef <= 1.0:
                return max(0.0, min(100.0, coef * 100.0))
            return max(0.0, min(100.0, coef))

        if aggregation_method == 13:
            if coef2 is not None and (weight_override or coef2 > 0.0):
                return max(0.0, min(100.0, coef2 * 100.0))
            if coef is not None:
                if 0.0 <= coef <= 1.0:
                    return max(0.0, min(100.0, coef * 100.0))
                return max(0.0, min(100.0, coef))

        return None

    def _collect_baseline_item_weights(self, node: dict, aggregation_method: int) -> Dict[str, float]:
        weights: Dict[str, float] = {}
        for child in self._iter_tree_children(node):
            child_type = str(child.get("type") or "").strip().lower()
            if child_type == "category":
                # Item weights belong to their immediate parent category;
                # do not roll up nested subcategory item weights here.
                continue

            child_item_type = str(child.get("itemtype") or "").strip().lower()
            if child_item_type == "category":
                continue

            name = str(child.get("name") or "").strip()
            if not name:
                continue

            pct = self._extract_baseline_item_weight_percent(child, aggregation_method)
            if pct is None or pct <= 0.0:
                continue
            weights[name] = round(float(pct), 2)

        return weights

    def _category_setting(self, node: dict, key: str, default: object = None) -> object:
        if key in node and node.get(key) is not None:
            return node.get(key)
        return default

    def _category_total_setting(self, node: dict, key: str, default: object = None) -> object:
        if key in node and node.get(key) is not None:
            return node.get(key)
        total_item = self._find_baseline_category_total_item(node)
        if isinstance(total_item, dict) and key in total_item and total_item.get(key) is not None:
            return total_item.get(key)
        return default

    def _collect_direct_real_item_children(self, node: dict) -> List[dict]:
        items: List[dict] = []
        for child in self._iter_tree_children(node):
            child_type = str(child.get("type") or "").strip().lower()
            if child_type == "category":
                continue
            child_item_type = str(child.get("itemtype") or "").strip().lower()
            if child_item_type == "category":
                continue
            items.append(child)
        return items

    def _iter_descendant_category_nodes(self, node: dict):
        for child in self._iter_tree_children(node):
            if str(child.get("type") or "").strip().lower() != "category":
                continue
            yield child
            yield from self._iter_descendant_category_nodes(child)

    def _infer_category_for_item(self, item_name: str, item_module: str = "") -> Optional[str]:
        name_l = (item_name or "").strip().lower()
        module_l = (item_module or "").strip().lower()

        if module_l == "quiz" or any(k in name_l for k in ("quiz", "knowledge check", "test")):
            return "Quizzes"
        if activity_name_matches_lab(name_l):
            return "Labs"
        if "midterm" in name_l or "mid term" in name_l:
            return "Midterm"
        if "final" in name_l or "exam" in name_l:
            return "Final Exam"
        if any(k in name_l for k in ("assign", "homework", "hw", "submission")):
            return "Assignments"
        return None

    def _reroute_root_items_to_matching_categories(
        self,
        categories: List[GradebookCategory],
        root_uncategorized_items: List[dict],
    ) -> List[dict]:
        by_name = {self._normalize_name(cat.name): cat for cat in categories if self._normalize_name(cat.name)}
        leftovers: List[dict] = []

        for item in root_uncategorized_items:
            item_name = str(item.get("name") or "").strip()
            if not item_name:
                leftovers.append(item)
                continue

            inferred = self._infer_category_for_item(item_name, str(item.get("itemmodule") or ""))
            if not inferred:
                leftovers.append(item)
                continue

            resolved = self._normalize_name(inferred)
            target = by_name.get(resolved)
            if target is None:
                leftovers.append(item)
                continue

            if item_name not in target.items:
                target.items.append(item_name)

        return leftovers

    def _apply_inferred_category_default_weights(self, categories: List[GradebookCategory]) -> None:
        inferred = [cat for cat in categories if float(getattr(cat, "weight", 0.0)) <= 0.001 and bool(cat.items)]
        if not inferred:
            return

        for cat in inferred:
            desired = 15.0 if self._normalize_name(cat.name) == "quizzes" else 10.0
            if desired <= 0.0:
                continue

            donor = next((c for c in categories if self._normalize_name(c.name) == "assignments" and c is not cat), None)
            if donor is None or float(getattr(donor, "weight", 0.0)) <= 0.001:
                donor = max((c for c in categories if c is not cat and float(getattr(c, "weight", 0.0)) > 0.001), key=lambda c: float(c.weight), default=None)
            if donor is None:
                continue

            transfer = min(desired, max(0.0, float(donor.weight)))
            if transfer <= 0.0:
                continue

            donor.weight = round(float(donor.weight) - transfer, 2)
            cat.weight = round(float(cat.weight) + transfer, 2)

    def generate_from_baseline_snapshot(
        self,
        baseline_snapshot: object,
        activities: List[CourseActivity],
    ) -> GradebookProposal:
        if not isinstance(baseline_snapshot, dict):
            return self.generate_initial(activities)

        if not bool(baseline_snapshot.get("available")):
            return self.generate_initial(activities)

        root = baseline_snapshot.get("tree")
        root_children = self._iter_tree_children(root)
        root_category = baseline_snapshot.get("root_category") if isinstance(baseline_snapshot.get("root_category"), dict) else {}
        root_aggregation_method = int(root_category.get("aggregation") or 13)

        categories: List[GradebookCategory] = []
        category_nodes: List[dict] = []
        top_level_aggregations: List[int] = []
        root_uncategorized_items: List[dict] = []

        for root_child in root_children:
            root_child_type = str(root_child.get("type") or "").strip().lower()
            if root_child_type == "category":
                continue
            if root_child_type == "courseitem":
                continue
            root_item_type = str(root_child.get("itemtype") or "").strip().lower()
            if root_item_type == "category":
                continue
            if root_item_type == "course":
                continue
            root_uncategorized_items.append(root_child)

        for node in root_children:
            if str(node.get("type") or "").strip().lower() != "category":
                continue

            name = str(node.get("name") or "").strip()
            if not name:
                continue

            node_aggregation_raw = self._to_int(node.get("aggregation"))
            if node_aggregation_raw is not None:
                top_level_aggregations.append(node_aggregation_raw)

            real_item_children = self._collect_direct_real_item_children(node)

            grade_max = self._extract_baseline_category_grade_max(node)
            drop_lowest = max(0, int(self._category_setting(node, "droplow", root_category.get("droplow") or 0) or 0))
            keep_highest = max(0, int(self._category_setting(node, "keephigh", root_category.get("keephigh") or 0) or 0))
            aggregate_only_graded = bool(self._category_setting(node, "aggregateonlygraded", root_category.get("aggregateonlygraded", True)))
            aggregate_outcomes = bool(self._category_setting(node, "aggregateoutcomes", root_category.get("aggregateoutcomes", False)))
            hidden_value = self._category_total_setting(node, "hidden", node.get("hidden") or 0)
            hidden_until_raw = self._category_total_setting(node, "hiddenuntil", node.get("hiddenuntil"))
            lock_time_raw = self._category_total_setting(node, "locktime", node.get("locktime"))
            locked_value = self._category_total_setting(node, "locked", node.get("locked") is True)
            display_type_raw = self._category_total_setting(node, "display", node.get("display"))
            decimals_raw = self._category_total_setting(node, "decimals", node.get("decimals"))
            grade_min_raw = self._category_total_setting(node, "grademin", node.get("grademin"))
            grade_pass_raw = self._category_total_setting(node, "gradepass", node.get("gradepass"))

            hidden_until_val = self._to_int(hidden_until_raw)
            lock_time_val = self._to_int(lock_time_raw)
            display_type_val = self._to_int(display_type_raw)
            decimals_val = self._to_int(decimals_raw)

            # Preserve important item-level visibility/locking semantics in baseline view.
            item_hidden = any(int(self._to_int(child.get("hidden")) or 0) != 0 for child in real_item_children)
            item_hidden_until_candidates = [
                int(v) for v in (self._to_int(child.get("hiddenuntil")) for child in real_item_children)
                if v is not None and int(v) > 0
            ]
            item_locked = any(bool(child.get("locked")) for child in real_item_children)
            item_locktime_candidates = [
                int(v) for v in (self._to_int(child.get("locktime")) for child in real_item_children)
                if v is not None and int(v) > 0
            ]

            hidden_until = hidden_until_val if hidden_until_val is not None and hidden_until_val > 0 else (max(item_hidden_until_candidates) if item_hidden_until_candidates else None)
            lock_time = lock_time_val if lock_time_val is not None and lock_time_val > 0 else (max(item_locktime_candidates) if item_locktime_candidates else None)
            display_type = display_type_val if display_type_val is not None else 0
            # Do not surface Moodle defaults as explicit formatting settings.
            decimals = decimals_val if (decimals_val is not None and decimals_val > 0) else -1
            grade_min_val = float(grade_min_raw) if grade_min_raw is not None else None
            if grade_min_val is not None and abs(grade_min_val) < 1e-9:
                grade_min_val = None
            grade_pass_val = float(grade_pass_raw) if grade_pass_raw is not None else None
            if grade_pass_val is not None and grade_pass_val <= 0.0:
                grade_pass_val = None
            category = GradebookCategory(
                name=name,
                weight=self._extract_baseline_weight_percent(node, root_aggregation_method),
                items=self._collect_baseline_item_names(node),
                item_weights=self._collect_baseline_item_weights(
                    node,
                    int(node.get("aggregation") or root_aggregation_method),
                ),
                subcategories=[],
                drop_lowest=drop_lowest,
                keep_highest=keep_highest,
                aggregate_only_graded=aggregate_only_graded,
                aggregate_outcomes=aggregate_outcomes,
                hidden=bool(int(hidden_value or 0) != 0) or (hidden_until is not None) or item_hidden,
                hidden_until=hidden_until,
                locked=bool(locked_value) or item_locked or (lock_time is not None),
                lock_time=lock_time,
                display_type=display_type,
                decimals=decimals,
                calculation_formula=str(node.get("calculation") or "").strip() or None,
                grade_min=grade_min_val,
                grade_max=grade_max if grade_max is not None else 100.0,
                grade_pass=grade_pass_val,
            )

            subcategories: List[GradebookSubcategory] = []
            seen_subcategories: set[str] = set()
            for child in self._iter_descendant_category_nodes(node):
                sub_name = str(child.get("name") or "").strip()
                if not sub_name:
                    continue
                sub_key = sub_name.lower()
                if sub_key in seen_subcategories:
                    continue
                seen_subcategories.add(sub_key)
                sub_agg = int(child.get("aggregation")) if child.get("aggregation") is not None else root_aggregation_method
                subcategories.append(
                    GradebookSubcategory(
                        name=sub_name,
                        weight=self._extract_baseline_weight_percent(child, root_aggregation_method),
                        items=self._collect_baseline_item_names(child),
                        item_weights=self._collect_baseline_item_weights(child, sub_agg),
                        aggregation_method=int(child.get("aggregation")) if child.get("aggregation") is not None else None,
                    )
                )

            if subcategories:
                category.subcategories = subcategories

            categories.append(category)
            category_nodes.append(node)

        if root_uncategorized_items:
            root_uncategorized_items = self._reroute_root_items_to_matching_categories(
                categories,
                root_uncategorized_items,
            )

        if root_uncategorized_items:
            uncategorized_node = {
                "children": root_uncategorized_items,
            }
            uncategorized_names = []
            for item in root_uncategorized_items:
                item_name = str(item.get("name") or "").strip()
                if item_name:
                    uncategorized_names.append(item_name)
            uncategorized_names = list(dict.fromkeys(uncategorized_names))
            if uncategorized_names:
                categories.append(
                    GradebookCategory(
                        name="Uncategorized",
                        weight=0.0,
                        items=uncategorized_names,
                        item_weights=self._collect_baseline_item_weights(
                            uncategorized_node,
                            root_aggregation_method,
                        ),
                        subcategories=[],
                        drop_lowest=0,
                        keep_highest=0,
                        aggregate_only_graded=True,
                        aggregate_outcomes=False,
                        hidden=False,
                        hidden_until=None,
                        locked=False,
                        lock_time=None,
                        display_type=0,
                        decimals=-1,
                        calculation_formula=None,
                        grade_min=None,
                        grade_max=100.0,
                        grade_pass=None,
                    )
                )

        if not categories:
            return self.generate_initial(activities)

        # In Natural aggregation, explicit weight overrides can be absent. In that case,
        # derive display weights from category point maxima to avoid showing all categories at 0%.
        total_weight = sum(max(0.0, float(getattr(cat, "weight", 0.0))) for cat in categories)
        if total_weight <= 0.01:
            fallback_maxima: List[float] = []
            for node in category_nodes:
                max_points = self._extract_baseline_category_grade_max(node)
                fallback_maxima.append(max_points if (max_points is not None and max_points > 0.0) else 0.0)

            total_points = sum(fallback_maxima)
            if total_points > 0.0:
                for idx, max_points in enumerate(fallback_maxima):
                    categories[idx].weight = (max_points / total_points) * 100.0

        has_imported_rules = any(
            (cat.drop_lowest > 0)
            or (cat.keep_highest > 0)
            or (not cat.aggregate_only_graded)
            or bool(cat.aggregate_outcomes)
            or bool(cat.hidden)
            or bool(cat.locked)
            or bool(cat.calculation_formula)
            or bool(cat.item_weights)
            or (cat.grade_min is not None)
            or (cat.grade_max != 100.0)
            or (cat.grade_pass is not None)
            or (cat.display_type != 0)
            or (cat.decimals >= 0)
            for cat in categories
        )

        notes = [
            "Initial proposal mirrors the existing Moodle gradebook baseline.",
            "Professor can refine category names, weights, and mapping before acceptance.",
        ]
        if has_imported_rules:
            notes.append(
                "Baseline category rules were imported (drop/keep, visibility/locking, points, and display settings where present)."
            )
        if root_uncategorized_items:
            notes.append(
                "Baseline root-level activities were imported under an 'Uncategorized' category for mapping review."
            )

        proposal_aggregation_method = root_aggregation_method
        if top_level_aggregations:
            unique_aggs = sorted(set(top_level_aggregations))
            if len(unique_aggs) == 1:
                proposal_aggregation_method = unique_aggs[0]

        return GradebookProposal(
            categories=categories,
            notes=notes,
            aggregation_method=proposal_aggregation_method,
        )

    def generate_initial(self, activities: List[CourseActivity]) -> GradebookProposal:
        categories = [GradebookCategory(name=name, weight=weight, items=[]) for name, weight in self.DEFAULT_CATEGORIES]
        by_name = {category.name: category for category in categories}

        def ensure_category(name: str) -> GradebookCategory:
            existing = by_name.get(name)
            if existing is not None:
                return existing

            # Inferred categories start at zero weight so we don't disturb the
            # default 100% baseline before the professor reviews the proposal.
            category = GradebookCategory(name=name, weight=0.0, items=[])
            categories.append(category)
            by_name[name] = category
            return category

        for activity in activities:
            if is_syllabus_like_name(activity.name):
                continue

            activity_name = activity.name.lower()
            module_name = (activity.module or "").lower()

            if module_name == "quiz" or any(keyword in activity_name for keyword in ("quiz", "knowledge check", "test")):
                ensure_category("Quizzes").items.append(activity.name)
            elif activity_name_matches_lab(activity_name):
                categories[1].items.append(activity.name)
            elif "midterm" in activity_name:
                categories[2].items.append(activity.name)
            elif "final" in activity_name or "exam" in activity_name:
                categories[3].items.append(activity.name)
            else:
                categories[0].items.append(activity.name)

        self._apply_inferred_category_default_weights(categories)

        return GradebookProposal(
            categories=categories,
            notes=[
                "Initial proposal is generated from available course context and Moodle activities.",
                "Professor can refine category names, weights, and mapping before acceptance.",
            ],
        )

    # Notes that are auto-generated (not user-visible descriptive text) get stripped each turn.
    _EPHEMERAL_NOTE_PREFIXES = (
        "weight check:",
        "item weight check:",
        "auto-normalized",
        "removed category",
        "split check:",
        "formula ignored:",
        "formula warning:",
        "formula was valid, but no target category was found.",
    )

    @staticmethod
    def _is_ephemeral_note(note: str) -> bool:
        low = note.lower()
        return any(low.startswith(prefix) for prefix in ProposalGenerator._EPHEMERAL_NOTE_PREFIXES)

    @staticmethod
    def _is_category_not_found_effect_note(note: str) -> bool:
        text = str(note or "")
        if not text.startswith("Effect:"):
            return False
        low = text.lower()
        return "category was not found" in low and "tried to" in low

    @staticmethod
    def _is_subcategory_not_found_effect_note(note: str) -> bool:
        text = str(note or "")
        if not text.startswith("Effect:"):
            return False
        low = text.lower()
        return "subcategory" in low and "was not found" in low and "tried to" in low

    @staticmethod
    def _append_unique_note(proposal: GradebookProposal, note: str) -> None:
        notes = proposal.notes or []
        if note not in notes:
            notes.append(note)
        proposal.notes = notes

    @staticmethod
    def _effect_topic_key(effect: str) -> str:
        """Return a stable topic key so later effects override earlier ones on same topic."""
        text = (effect or "").strip()
        low = text.lower()

        m = re.match(r"^in\s+(.+?),\s*split\s+into\b", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"split:{label}"

        m = re.match(r"^keep\s+highest\s+\d+\s+from\s+(.+)$", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"keephigh:{label}"

        m = re.match(r"^drop\s+lowest\s+\d+\s+from\s+(.+)$", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"droplow:{label}"

        if low.startswith("aggregation method set to"):
            return "aggregation_method"

        m = re.match(r"^added\s+'?(.+?)'?\s+with\s+weight\s+\d+(?:\.\d+)?%", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '")
            return f"category_add:{label}"

        m = re.match(r"^(?:set|changed?|adjusted?|updated?)\s+(.+?)\s+to\s+\d+(?:\.\d+)?%", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '")
            return f"weight:{label}"

        # Formula effects on the same category should share one topic key.
        m = re.match(r"^(?:applied|cleared)\s+formula\s+(?:to|from)\s+(.+?)(?::|$)", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '\"")
            return f"formula:{label}"

        m = re.match(r"^stored\s+formula\s+for\s+(.+?)(?:\s*\(|:|$)", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip(" '\"")
            return f"formula:{label}"

        m = re.match(r"^(.+?)\s+hidden\s+until\b", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            return f"hiddenuntil:{label}"

        m = re.match(r"^moved '(.+?)'", low)
        if m:
            return f"item_move:{m.group(1).lower()}"

        m = re.match(r"^set item weight for '(.+?)'", low)
        if m:
            return f"item_weight:{m.group(1).lower()}"

        m = re.match(r"^removed '(.+?)' from", low)
        if m:
            return f"item_remove:{m.group(1).lower()}"

        m = re.match(r"^tried to .+? '(.+?)', but category was not found", low)
        if m:
            label = re.sub(r"\s+", " ", m.group(1)).strip().lower()
            return f"not_found:{label}"

        return low

    @staticmethod
    def _append_effect_note(proposal: GradebookProposal, effect: str) -> None:
        notes = proposal.notes or []
        clean_effect = effect.strip()
        entry = f"Effect: {clean_effect}"
        topic_key = ProposalGenerator._effect_topic_key(clean_effect)

        # Keep only the latest entry per topic (e.g., latest Labs split overrides older Labs split).
        filtered = []
        for n in notes:
            if not str(n).startswith("Effect:"):
                filtered.append(n)
                continue
            old_effect = str(n)[len("Effect:"):].strip()
            if ProposalGenerator._effect_topic_key(old_effect) == topic_key:
                continue
            filtered.append(n)

        if not filtered or filtered[-1] != entry:
            filtered.append(entry)
        proposal.notes = filtered

    def update_from_prompt(
        self,
        proposal: GradebookProposal,
        prompt: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> GradebookProposal:
        updated = GradebookProposal.parse_obj(proposal.model_dump())
        prompt_lower = prompt.lower()

        # Strip ephemeral per-turn notes so they don't accumulate.
        updated.notes = [n for n in (updated.notes or []) if not self._is_ephemeral_note(n)]
        # Category/subcategory-not-found effects are per-turn; guard re-adds only for the current prompt.
        updated.notes = [
            n for n in (updated.notes or [])
            if not self._is_category_not_found_effect_note(n)
            and not self._is_subcategory_not_found_effect_note(n)
        ]

        # Detect split context early so weight extraction skips subcategory names.
        # Support both:
        # - "split ... into ..."
        # - "add subcategories: ..."
        is_split_prompt = bool(re.search(r"\b(?:split|divide)\b.+\binto\b|\badd\s+subcategories\b", prompt_lower))

        # Subcategory rename before item rename so "rename Projects to Project" targets splits.
        renamed_subcategory_sources = self._apply_subcategory_renames(updated, prompt)

        # Apply explicit grade-item remove/rename intents before category removal
        # so phrases like "remove fina grade item" do not remove a whole category.
        self._apply_item_remove_rename(
            updated,
            prompt,
            renamed_subcategory_sources=renamed_subcategory_sources,
            course_activities=course_activities,
        )
        self._apply_not_graded_operations(updated, prompt, course_activities=course_activities)

        self._apply_removals(updated, prompt)

        prompt_for_category_weights, subcategory_weight_intents = self._extract_subcategory_weight_intents(prompt)
        weights = self._parse_weight_assignments(
            updated,
            prompt_for_category_weights,
            skip_split_pieces=is_split_prompt,
        )

        # Creation-style prompts that provide a full category set should replace categories,
        # not mutate previous defaults in-place.
        if self._should_rebuild_category_set(prompt, weights):
            updated.categories = self._rebuild_categories_from_weights(updated, weights)
            weights = {}

        # Support directives like "give remaining weight to midterm".
        remaining_target = self._parse_remaining_target(updated, prompt)
        if remaining_target:
            weights = self._apply_remaining_weight_directive(updated, weights, remaining_target)

        if weights:
            self._apply_weight_updates(updated, weights)

        self._apply_add_category_requests(updated, prompt, explicit_updates=weights)

        self._apply_directive_normalization(updated, prompt, explicit_updates=weights)

        # Apply per-category settings (drop/keep/extra credit/exclude empty)
        self._apply_category_settings(updated, prompt)

        # Parse and persist optional formula-driven grading requests.
        self._apply_formula_request(updated, prompt, course_activities=course_activities or [])

        # Parse clear/remove formula directives after apply, so replacement prompts work.
        self._apply_formula_clear_request(updated, prompt)

        # Persist aggregation method requests (e.g., "use weighted mean").
        self._apply_aggregation_method(updated, prompt)

        if is_split_prompt:
            self._apply_split_request(updated, prompt)

        self._apply_subcategory_additions(updated, prompt)
        self._apply_subcategory_weight_intents(updated, subcategory_weight_intents)
        self._apply_subcategory_weight_updates(updated, prompt_for_category_weights)
        self._apply_item_additions(updated, prompt)
        self._apply_item_operations(updated, prompt)

        self._post_update_checks(updated, prompt)
        self._guard_scoped_category_intents(updated, prompt)

        return updated

    def _detect_formula_target_category(self, proposal: GradebookProposal, prompt: str, item_refs: List[str]) -> Optional[GradebookCategory]:
        """Best-effort target category detection for a formula prompt."""
        prompt_l = (prompt or "").lower()

        explicit = re.search(r"\b(?:set|use|apply)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+(?:as|formula|calculation)", prompt_l)
        if explicit:
            hint = explicit.group(1).strip()
            targets = self._resolve_category_targets(proposal, hint)
            if targets:
                return targets[0]

        for category in proposal.categories:
            if category.name.lower() in prompt_l:
                return category

        for ref in item_refs:
            ref_targets = self._resolve_category_targets(proposal, ref)
            if ref_targets:
                return ref_targets[0]

        final_targets = self._resolve_category_targets(proposal, "final")
        if final_targets:
            return final_targets[0]

        return proposal.categories[0] if proposal.categories else None

    def _apply_formula_request(
        self,
        proposal: GradebookProposal,
        prompt: str,
        course_activities: List[CourseActivity],
    ) -> None:
        """Extract, validate, and store a formula request in the proposal when present."""
        detected = FormulaParser.extract_formula_and_detect(prompt)
        if not detected:
            return

        raw_formula = str(detected.get("formula") or "").strip()
        if not raw_formula:
            return

        validation = FormulaParser.parse_formula(raw_formula)
        if not validation.is_valid:
            self._append_unique_note(
                proposal,
                f"Formula ignored: {validation.error_message}",
            )
            return

        target = self._detect_formula_target_category(
            proposal=proposal,
            prompt=prompt,
            item_refs=validation.item_references or [],
        )
        if target is None:
            self._append_unique_note(
                proposal,
                "Formula was valid, but no target category was found.",
            )
            return

        normalized = validation.normalized_formula or raw_formula.lstrip("=").strip()
        if course_activities:
            category_context_names = []
            for c in (proposal.categories or []):
                if getattr(c, "name", None):
                    category_context_names.append(c.name)
                for sub in (getattr(c, "subcategories", None) or []):
                    if getattr(sub, "name", None):
                        category_context_names.append(sub.name)
            resolved_formula, unresolved_refs, suggestions = FormulaResolver.resolve_formula(
                formula=f"={normalized}",
                activities=course_activities,
                category_names=category_context_names,
            )
        else:
            # Backward-compatible behavior: when activity context is unavailable,
            # keep normalized refs as-is and treat the formula as applied.
            resolved_formula = FormulaParser.moodle_compatible_formula(f"={normalized}")
            unresolved_refs = []
            suggestions = {}

        target.calculation_formula = resolved_formula
        target.formula_override = self._prompt_requests_formula_override(prompt)
        target.formula_item_refs = list(validation.item_references or [])
        target.formula_unresolved_refs = list(unresolved_refs or [])

        if unresolved_refs:
            unresolved_text = ", ".join(f"[{ref}]" for ref in unresolved_refs)
            self._append_unique_note(
                proposal,
                f"Formula warning: unresolved references {unresolved_text}.",
            )
            # Include compact "did you mean" hints for the first unresolved ref.
            first_ref = unresolved_refs[0]
            hint = suggestions.get(first_ref) or []
            if hint:
                self._append_unique_note(
                    proposal,
                    f"Formula warning: did you mean one of {', '.join(hint[:3])} for [{first_ref}]?",
                )
            self._append_effect_note(
                proposal,
                f"Stored formula for '{target.name}' (unresolved refs: {unresolved_text}).",
            )
        else:
            self._append_effect_note(
                proposal,
                f"Applied formula to {target.name}: {target.calculation_formula}",
            )

    def _apply_formula_clear_request(self, proposal: GradebookProposal, prompt: str) -> None:
        """Clear existing formulas based on explicit clear/remove formula directives."""
        text_l = (prompt or "").lower()
        if not re.search(r"\b(?:clear|remove|delete|unset)\b.*\bformula\b", text_l):
            return

        target_hint = ""
        target_match = re.search(
            r"\b(?:clear|remove|delete|unset)\s+(?:the\s+)?formula\s+(?:from|for|on)\s+([a-zA-Z][a-zA-Z ]{1,40})",
            text_l,
        )
        if not target_match:
            target_match = re.search(
                r"\b(?:clear|remove|delete|unset)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,40})\s+formula\b",
                text_l,
            )
        if target_match:
            target_hint = target_match.group(1).strip().rstrip(".,")

        targets = self._resolve_category_targets(proposal, target_hint)
        for cat in targets:
            if not getattr(cat, "calculation_formula", None):
                continue
            cat.calculation_formula = None
            cat.formula_override = False
            cat.formula_item_refs = []
            cat.formula_unresolved_refs = []
            self._append_effect_note(proposal, f"Cleared formula from {cat.name}")

    @staticmethod
    def _prompt_requests_formula_override(prompt: str) -> bool:
        text_l = (prompt or "").lower()
        return bool(
            re.search(r"\b(?:override|replace|force)\b.*\bformula\b", text_l)
            or re.search(r"\bformula\b.*\b(?:override|replace|force)\b", text_l)
        )

    @staticmethod
    def _parse_aggregation_method(prompt: str) -> Optional[int]:
        text_l = (prompt or "").lower()
        _VALID_METHOD_IDS = {0, 10, 11, 12, 13}

        # Direct numeric method ID: "method number 13", "method 13", "number 13", "use number 13"
        numeric_m = re.search(r'\b(?:method\s+(?:number\s+)?|number\s+)(\d+)\b', text_l)
        if numeric_m:
            n = int(numeric_m.group(1))
            if n in _VALID_METHOD_IDS:
                return n

        # Only treat text as aggregation intent when method/aggregation context is explicit.
        has_agg_context = bool(
            re.search(r"\b(?:aggregation|aggregate|method|grade\s+aggregation)\b", text_l)
            or re.search(r"\b(?:use|set|switch|change)\b", text_l)
            or any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted", "mean of grades", "simple mean", "natural"))
        )

        if has_agg_context and any(term in text_l for term in ("weighted mean", "weighted average", "simple weighted")):
            if "simple" in text_l:
                return 11  # Simple weighted mean
            return 10  # Weighted mean

        if has_agg_context and any(term in text_l for term in ("mean of grades", "simple mean", "mean")):
            if "extra credit" in text_l or "extra credits" in text_l:
                return 12  # Mean with extra credits
            return 0  # Mean of grades

        if has_agg_context and any(term in text_l for term in ("natural", "moodle default", "default aggregation")):
            return 13

        if has_agg_context and ("extra credit" in text_l or "extra credits" in text_l):
            return 12

        return None

    @staticmethod
    def _get_aggregation_method_name(method: int) -> str:
        return {
            0: "Mean of grades",
            10: "Weighted mean of grades",
            11: "Simple weighted mean of grades",
            12: "Mean of grades (with extra credits)",
            13: "Natural",
        }.get(int(method), f"Method {method}")

    def _apply_aggregation_method(self, proposal: GradebookProposal, prompt: str) -> None:
        method = self._parse_aggregation_method(prompt)
        if method is None:
            return
        old_method = proposal.aggregation_method
        proposal.aggregation_method = method
        if old_method != method:
            name = self._get_aggregation_method_name(method)
            self._append_effect_note(proposal, f"Aggregation method set to {name}")

    def _apply_category_settings(self, proposal: GradebookProposal, prompt: str) -> None:
        """Parse and apply drop_lowest, keep_highest, extra_credit, aggregate_only_graded per category."""
        prompt_lower = prompt.lower()

        # Scoped phrasing: "For Assignments, set drop lowest to 1"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*set\s+drop\s+lowest\s+(?:to\s+)?(\d+)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            n = int(m.group(2))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.drop_lowest = n
                cat.keep_highest = 0
                self._append_effect_note(proposal, f"Drop lowest {n} from {cat.name}")

        # drop lowest N [from category]
        for m in re.finditer(
            r"\bdrop\s+(?:the\s+)?(?:lowest\s+)?(\d+)\s+(?:lowest\s+)?(?:grade[s]?\s+)?(?:from\s+)?([a-zA-Z][a-zA-Z ]{1,40})?",
            prompt_lower,
        ):
            n = int(m.group(1))
            cat_hint = (m.group(2) or "").strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.drop_lowest = n
                cat.keep_highest = 0  # mutually exclusive
                self._append_effect_note(proposal, f"Drop lowest {n} from {cat.name}")

        # Scoped phrasing: "For Labs, keep highest 2 items"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*keep\s+(?:the\s+)?(?:highest|top|best)\s+(\d+)(?:\s+items?)?",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            n = int(m.group(2))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.keep_highest = n
                cat.drop_lowest = 0
                self._append_effect_note(proposal, f"Keep highest {n} from {cat.name}")

        # keep highest / best N [from category]
        for m in re.finditer(
            r"\bkeep\s+(?:the\s+)?(?:(?:top|best|highest)\s+)?(\d+)\s+(?:top\s+|best\s+|highest\s+)?(?:grade[s]?\s+)?(?:from\s+)?([a-zA-Z][a-zA-Z ]{1,40})?",
            prompt_lower,
        ):
            n = int(m.group(1))
            cat_hint = (m.group(2) or "").strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.keep_highest = n
                cat.drop_lowest = 0  # mutually exclusive
                self._append_effect_note(proposal, f"Keep highest {n} from {cat.name}")

        # extra credit: "assignments count as extra credit" / "extra credit for labs"
        for m in re.finditer(
            r"\b([a-zA-Z][a-zA-Z ]{1,30})\s+(?:(?:count|counts|is|are)\s+(?:as\s+)?)?extra\s+credit",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.extra_credit = True

        for m in re.finditer(
            r"\bextra\s+credit\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.extra_credit = True

        # include/exclude empty grades: "include empty" / "exclude empty for quizzes"
        if re.search(r"\binclude\s+empty\b", prompt_lower):
            cat_m = re.search(r"\binclude\s+empty\s+(?:grades?\s+)?(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = False
                self._append_effect_note(proposal, f"{cat.name} includes empty grades in aggregation")

        # category-first phrasing: "for assignments, include empty grades"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*(?:set\s+)?include\s+empty(?:\s+grades?)?\b",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = False
                self._append_effect_note(proposal, f"{cat.name} includes empty grades in aggregation")

        if re.search(r"\bexclude\s+empty\b", prompt_lower):
            cat_m = re.search(r"\bexclude\s+empty\s+(?:grades?\s+)?(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = True
                self._append_effect_note(proposal, f"{cat.name} aggregates only non-empty grades")

        # category-first phrasing: "for assignments, exclude empty grades"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*(?:set\s+)?exclude\s+empty(?:\s+grades?)?\b",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = True
                self._append_effect_note(proposal, f"{cat.name} aggregates only non-empty grades")

        # Alternate phrasing: "aggregate only non-empty grades" / "only graded"
        for m in re.finditer(
            r"\bfor\s+([a-zA-Z][a-zA-Z ]{1,30})\s*,?\s*(?:set\s+)?aggregate\s+only\s+(?:non[-\s]?empty|graded)\s+grades?",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip(".,")
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_only_graded = True
                self._append_effect_note(proposal, f"{cat.name} aggregates only non-empty grades")

        # aggregate outcomes: include or exclude outcome items in aggregation
        if re.search(r"\b(?:include|aggregate)\s+outcomes?\b", prompt_lower):
            cat_m = re.search(r"\b(?:include|aggregate)\s+outcomes?\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_outcomes = True

        if re.search(r"\b(?:exclude|ignore|do\s+not\s+aggregate)\s+outcomes?\b", prompt_lower):
            cat_m = re.search(r"\b(?:exclude|ignore|do\s+not\s+aggregate)\s+outcomes?\s+(?:for\s+)?([a-zA-Z][a-zA-Z ]{1,30})?", prompt_lower)
            cat_hint = (cat_m.group(1) or "").strip() if cat_m else ""
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.aggregate_outcomes = False

        # grade_max: "max grade for assignments is 150" / "assignments out of 150"
        for m in re.finditer(
            r"(?:max(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(\d+(?:\.\d+)?)"
            r"|max(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?)"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+(?:is\s+)?out\s+of\s+(\d+(?:\.\d+)?)"
            r"|set\s+(?:max|maximum)\s+(?:grade\s+)?for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            elif m.group(3) and m.group(4):
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            elif m.group(5) and m.group(6):
                cat_hint, val = m.group(5).strip(), float(m.group(6))
            else:
                cat_hint, val = m.group(7).strip(), float(m.group(8))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_max = val

        # grade_pass: "passing grade for labs is 60" / "pass labs at 60%"
        for m in re.finditer(
            r"(?:passing\s+(?:grade|mark|score)\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(\d+(?:\.\d+)?)"
            r"|passing\s+(?:grade|mark|score)\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(\d+(?:\.\d+)?)"
            r"|pass\s+([a-zA-Z][a-zA-Z ]{1,30})\s+(?:at|with)\s+(\d+(?:\.\d+)?)"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+pass(?:ing)?\s+(?:is\s+)?(?:at\s+)?(\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            elif m.group(3) and m.group(4):
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            elif m.group(5) and m.group(6):
                cat_hint, val = m.group(5).strip(), float(m.group(6))
            else:
                cat_hint, val = m.group(7).strip(), float(m.group(8))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_pass = val

        # grade_min: "minimum grade for labs is 0" / "set min for assignments to 10"
        for m in re.finditer(
            r"(?:min(?:imum)?\s+(?:grade|point|mark)s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+is\s+(-?\d+(?:\.\d+)?)"
            r"|set\s+min(?:imum)?\s+(?:grade\s+)?for\s+([a-zA-Z][a-zA-Z ]{1,30})\s+to\s+(-?\d+(?:\.\d+)?))",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                cat_hint, val = m.group(1).strip(), float(m.group(2))
            else:
                cat_hint, val = m.group(3).strip(), float(m.group(4))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.grade_min = val

        # hide until: "hide assignments until 2026-12-20"
        for m in re.finditer(
            r"\bhide\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:until|untill)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {m.group(2).strip()}")

        # "set/make X as hidden until 2026-05-19"
        for m in re.finditer(
            r"\b(?:set|make)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:as\s+)?hidden\s+(?:until|untill)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {m.group(2).strip()}")

        # Relative-date phrasing for both "set/make X as hidden until ..." and "hide X until ...".
        # Supports: "next week", "in 2 months", "3 weeks from now", "tomorrow", "in 5 days"
        for m in re.finditer(
            r"\b(?:set|make|hide)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:as\s+)?(?:hidden\s+)?(?:until|untill)\s+"
            r"(next\s+week|tomorrow|next\s+month|(?:in\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:weeks?|months?|days?)(?:\s+from\s+now)?)(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            date_part = m.group(2).strip()
            ts = self._parse_relative_date_to_timestamp(date_part)
            display_text = date_part
            
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {display_text}")

        # Continuation phrasing after a hide-until directive:
        # "hide midterm until next week and final until next month"
        for m in re.finditer(
            r"\band\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:as\s+)?(?:hidden\s+)?(?:until|untill)\s+"
            r"([a-zA-Z0-9\- ]{2,40}?)(?=(?:\s+and\s+|\.|,|;|$))",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            date_part = m.group(2).strip()
            ts, display_text = self._parse_hide_until_expression(date_part)
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                cat.hidden_until = ts
                self._append_effect_note(proposal, f"{cat.name} hidden until {display_text}")

        # hidden: "hide assignments from students" / "hide midterm"
        # Keep this after hide-until handlers so date expressions are not swallowed.
        for m in re.finditer(
            r"\bhide\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?=\s+(?:category|from\s+students?)\b|\s*$)(?:\s+(?:category|from\s+students?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip().rstrip()
            if re.search(r"\b(?:until|untill)\b", cat_hint):
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.hidden = True
                self._append_effect_note(proposal, f"{cat.name} hidden from students")

        is_proposal_view_prompt = bool(
            re.search(r"\bshow\s+proposal\b", prompt_lower)
            or re.search(
                r"\b(?:show|display|list|print)\b.*\b(?:full\s+)?(?:proposal|gradebook\s+structure)\b",
                prompt_lower,
            )
        )
        if not is_proposal_view_prompt:
            for m in re.finditer(
                r"\b(?:show|unhide|reveal)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+category)?(?:\b|$)",
                prompt_lower,
            ):
                cat_hint = m.group(1).strip()
                targets = self._resolve_category_targets(proposal, cat_hint)
                for cat in targets:
                    cat.hidden = False
                    cat.hidden_until = None
                    self._append_effect_note(proposal, f"{cat.name} is visible to students")

        # locked: "lock the final exam" / "unlock assignments"
        for m in re.finditer(
            r"\block\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:category|grades?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = True
                cat.lock_time = None

        # schedule lock: "lock labs until 2026-10-01"
        for m in re.finditer(
            r"\block\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:until|on|at)\s+(\d{4}-\d{2}-\d{2})(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            ts = self._parse_iso_date_to_timestamp(m.group(2).strip())
            if ts is None:
                continue
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = False
                cat.lock_time = ts

        for m in re.finditer(
            r"\bunlock\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)(?:\s+(?:category|grades?))?(?:\b|$)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.locked = False
                cat.lock_time = None

        # display_type: "show assignments as percentage" / "display labs as letter"
        display_map = {
            r"\bpercentage\b": GRADE_DISPLAY_TYPE_PERCENTAGE,
            r"\bletter\b": GRADE_DISPLAY_TYPE_LETTER,
            r"\breal\b|\bnumeric\b|\bnumber\b": GRADE_DISPLAY_TYPE_REAL,
            r"\breal\s+and\s+percentage\b|\bpercentage\s+and\s+real\b": GRADE_DISPLAY_TYPE_REAL_PERCENTAGE,
            r"\breal\s+and\s+letter\b|\bletter\s+and\s+real\b": GRADE_DISPLAY_TYPE_REAL_LETTER,
            r"\bdefault\s+display\b": GRADE_DISPLAY_TYPE_DEFAULT,
        }
        for m in re.finditer(
            r"(?:show|display|format)\s+(?:the\s+)?([a-zA-Z][a-zA-Z ]{1,30}?)\s+(?:category\s+)?(?:grades?\s+)?as\s+([a-zA-Z][a-zA-Z +]*)",
            prompt_lower,
        ):
            cat_hint = m.group(1).strip()
            disp_text = m.group(2).strip()
            resolved_type = None
            for pattern, dtype in display_map.items():
                if re.search(pattern, disp_text):
                    resolved_type = dtype
                    break
            if resolved_type is not None:
                targets = self._resolve_category_targets(proposal, cat_hint)
                for cat in targets:
                    cat.display_type = resolved_type

        # decimals: "use 2 decimal places for assignments" / "show 0 decimals for labs"
        for m in re.finditer(
            r"(?:use|show|set)\s+(\d)\s+decimal(?:\s+place)?s?\s+for\s+([a-zA-Z][a-zA-Z ]{1,30})"
            r"|([a-zA-Z][a-zA-Z ]{1,30})\s+(?:use|show|with)\s+(\d)\s+decimal(?:\s+place)?s?",
            prompt_lower,
        ):
            if m.group(1) and m.group(2):
                val, cat_hint = int(m.group(1)), m.group(2).strip()
            else:
                val, cat_hint = int(m.group(4)), m.group(3).strip()
            val = max(0, min(5, val))
            targets = self._resolve_category_targets(proposal, cat_hint)
            for cat in targets:
                cat.decimals = val

    @staticmethod
    def _parse_iso_date_to_timestamp(date_text: str) -> Optional[int]:
        """Parse YYYY-MM-DD into a UTC timestamp at 00:00:00."""
        try:
            dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return int(dt.timestamp())

    @staticmethod
    def _parse_relative_date_to_timestamp(relative_text: str) -> Optional[int]:
        text = (relative_text or "").strip().lower()
        now = datetime.now(timezone.utc)
        _WORD_NUMS = {
            'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        }

        if text == "tomorrow":
            target = now + timedelta(days=1)
        elif text == "next week":
            target = now + timedelta(days=7)
        elif text == "next month":
            target = now + timedelta(days=30)
        else:
            # "two weeks from now" / "in 2 weeks" / "3 weeks from now"
            m_weeks = re.match(
                r'^(?:in\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+weeks?(?:\s+from\s+now)?$',
                text,
            )
            if m_weeks:
                raw = m_weeks.group(1)
                n = int(raw) if raw.isdigit() else _WORD_NUMS.get(raw, 1)
                target = now + timedelta(weeks=n)
            else:
                # "in N months" / "N months from now" / "next month" already handled
                m_months = re.match(
                    r'^(?:in\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+months?(?:\s+from\s+now)?$',
                    text,
                )
                if m_months:
                    raw = m_months.group(1)
                    n = int(raw) if raw.isdigit() else _WORD_NUMS.get(raw, 1)
                    # Add months by year+month arithmetic to handle month lengths correctly
                    month = now.month + n
                    year = now.year
                    while month > 12:
                        month -= 12
                        year += 1
                    try:
                        target = now.replace(year=year, month=month, day=1)
                    except ValueError:
                        # Handle day overflow (e.g., Jan 31 + 1 month -> Feb 28/29)
                        target = now.replace(year=year, month=month + 1, day=1) - timedelta(days=1)
                else:
                    # "in N days"
                    m_days = re.match(r'^in\s+(\d+)\s+days?$', text)
                    if m_days:
                        target = now + timedelta(days=int(m_days.group(1)))
                    else:
                        return None

        target = target.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(target.timestamp())

    def _parse_hide_until_expression(self, date_text: str) -> Tuple[Optional[int], str]:
        """Parse hide-until text that may be ISO or relative date phrasing."""
        cleaned = (date_text or "").strip().lower()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return self._parse_iso_date_to_timestamp(cleaned), cleaned
        return self._parse_relative_date_to_timestamp(cleaned), cleaned

    def _resolve_category_targets(self, proposal: GradebookProposal, hint: str) -> List:
        """Return matching categories for a hint string, or all categories if hint is empty."""
        if not hint:
            return list(proposal.categories)
        resolved = self._resolve_category_name(proposal, hint)
        if resolved:
            for cat in proposal.categories:
                if cat.name == resolved:
                    return [cat]
        return []

    def _parse_remaining_target(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        match = re.search(
            r"\b(?:give|assign|put|allocate)\s+(?:the\s+)?(?:remaining|remai\w*|left(?:over)?)\s+(?:weight\s+)?(?:to|ot|into)\s+([a-zA-Z][a-zA-Z ]{1,40})",
            prompt,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return self._resolve_category_name(proposal, match.group(1).strip())

    def _apply_remaining_weight_directive(
        self,
        proposal: GradebookProposal,
        weights: Dict[str, float],
        remaining_target: str,
    ) -> Dict[str, float]:
        current = {cat.name: float(cat.weight) for cat in proposal.categories}
        for name, value in weights.items():
            current[name] = float(value)

        remainder = 100.0 - sum(
            weight for name, weight in current.items()
            if name.lower() != remaining_target.lower()
        )
        weights[remaining_target] = round(remainder, 2)
        return weights

    def _rebalance_weights(
        self,
        proposal: GradebookProposal,
        weights: Dict[str, float],
        remaining_target: str,
    ) -> Dict[str, float]:
        current = {cat.name: float(cat.weight) for cat in proposal.categories}
        for name, value in weights.items():
            current[name] = float(value)

        remainder = 100.0 - sum(
            weight for name, weight in current.items()
            if name.lower() != remaining_target.lower()
        )
        weights[remaining_target] = round(remainder, 2)
        return weights

    def _apply_directive_normalization(
        self,
        proposal: GradebookProposal,
        prompt: str,
        explicit_updates: Dict[str, float],
    ) -> None:
        if not explicit_updates:
            return

        prompt_lower = prompt.lower()

        # "decrease final accordingly" / "increase labs ... decrease final accordingly"
        if any(token in prompt_lower for token in ("accordingly", "decrease", "increase")):
            target = self._parse_accordingly_target(proposal, prompt)
            if target:
                self._adjust_single_category_to_target_total(proposal, target)
                return

        # "redistribute proportionally across other categories"
        if "proportion" in prompt_lower and any(word in prompt_lower for word in ("redistribute", "distribute")):
            self._auto_fix_total_to_100(proposal, explicit_updates=explicit_updates)
            return

        # Generic normalization requests.
        if self._NORMALIZE_PATTERNS.search(prompt):
            self._auto_fix_total_to_100(proposal, explicit_updates=explicit_updates)

    def _parse_accordingly_target(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        match = re.search(
            r"\b(?:decrease|increase|adjust|reduce)\s+([a-zA-Z][a-zA-Z ]{1,40})\s+accordingly",
            prompt,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return self._resolve_category_name(proposal, match.group(1).strip())

    def _adjust_single_category_to_target_total(self, proposal: GradebookProposal, category_name: str) -> None:
        target = next((cat for cat in proposal.categories if cat.name.lower() == category_name.lower()), None)
        if target is None:
            return

        other_total = sum(cat.weight for cat in proposal.categories if cat is not target)
        target.weight = round(100.0 - other_total, 2)
        proposal.notes.append(f"Adjusted '{target.name}' to keep total exactly 100%.")

    def _auto_fix_total_to_100(self, proposal: GradebookProposal, explicit_updates: Dict[str, float]) -> None:
        """
        Ensure total category weight remains exactly 100 after explicit updates.

        Strategy:
          - If there are untouched categories, scale ONLY the untouched ones to fill the remaining weight.
            (Keeps the instructor-specified categories exact.)
          - If all categories were explicitly set, adjust the last-updated category by the delta.
        """
        if not proposal.categories:
            return

        # Normalize key set to existing category names (case-insensitive).
        explicit_keys = {name.lower() for name in explicit_updates.keys()}
        cats = list(proposal.categories)
        untouched = [c for c in cats if c.name.lower() not in explicit_keys]
        touched = [c for c in cats if c.name.lower() in explicit_keys]

        total = sum(c.weight for c in cats)
        if abs(total - 100.0) <= 0.1:
            return

        target_remaining = 100.0 - sum(c.weight for c in touched)

        if untouched:
            current_untouched_total = sum(c.weight for c in untouched)
            if abs(current_untouched_total) < 0.001:
                # No usable baseline to scale; distribute evenly.
                even = target_remaining / len(untouched)
                for c in untouched:
                    c.weight = round(even, 2)
            else:
                scale = target_remaining / current_untouched_total
                for c in untouched:
                    c.weight = round(c.weight * scale, 2)

            # Final micro-adjust to eliminate rounding drift (apply to the last untouched).
            new_total = sum(c.weight for c in cats)
            delta = 100.0 - new_total
            if abs(delta) > 0.001:
                untouched[-1].weight = round(untouched[-1].weight + delta, 2)

            proposal.notes.append("Auto-normalized remaining category weights to keep total exactly 100%.")
            return

        # If everything was touched, adjust the last explicit category.
        # Dict preserves insertion order (py3.7+), and our parser overwrites duplicates,
        # so this is a reasonable proxy for "last mentioned".
        last_name = next(reversed(explicit_updates.keys()), None)
        if last_name:
            last = next((c for c in cats if c.name.lower() == last_name.lower()), None)
            if last is not None:
                delta = 100.0 - total
                last.weight = round(last.weight + delta, 2)
                proposal.notes.append(f"Auto-normalized '{last.name}' by {delta:+.2f}% to keep total 100%.")

    # Collect names of split subcategories so they are excluded from weight-parsing
    # (prevents "Homework 15%" being misread as "Assignments 15%" via alias).
    @staticmethod
    def _extract_split_piece_names(prompt: str) -> List[str]:
        match = re.search(r"\b(?:split|divide)\b.+?\binto\b(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"\badd\s+subcategories\s*[:;]?\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            return []
        pieces = re.split(r",| and | & ", match.group(1))
        names = []
        for piece in pieces:
            part = re.sub(
                r"\bwith\s+weight\s+of\s+\d+(?:\.\d+)?\s*%?\b.*",
                "",
                piece,
                flags=re.IGNORECASE,
            ).strip()
            part = re.sub(r"\d+(?:\.\d+)?\s*%.*", "", part).strip()
            part = re.sub(r"\b(but|keep|as|the|parent|category)\b", " ", part, flags=re.IGNORECASE).strip()
            if part:
                names.append(part.lower())
        return names

    @staticmethod
    def _parse_subcategory_weight_value(raw_weight: str, parent_weight: float) -> Optional[float]:
        try:
            value = float(raw_weight)
        except (TypeError, ValueError):
            return None

        # Treat fractional values (e.g., 0.4) as a parent-relative share.
        if 0.0 <= value <= 1.0:
            return float(parent_weight) * value
        return value

    def _find_item_in_proposal(
        self, proposal: GradebookProposal, item_query: str
    ) -> Tuple[Optional[GradebookCategory], Optional[str]]:
        """Fuzzy search for a grade item name across parent and subcategory item lists."""
        query_lower = self._normalize_item_query(item_query).lower()
        if not query_lower:
            return None, None
        best_cat: Optional[GradebookCategory] = None
        best_item: Optional[str] = None
        best_score = 0.0

        def _consider(cat: GradebookCategory, item: str) -> None:
            nonlocal best_cat, best_item, best_score
            item_lower = item.lower()
            if query_lower == item_lower:
                best_cat = cat
                best_item = item
                best_score = 1.0
                return
            if query_lower in item_lower or item_lower in query_lower:
                score = min(len(query_lower), len(item_lower)) / max(len(query_lower), len(item_lower))
                if score > best_score:
                    best_score = score
                    best_cat = cat
                    best_item = item

        for cat in proposal.categories:
            for sub in (cat.subcategories or []):
                for item in (sub.items or []):
                    _consider(cat, item)
                    if best_score >= 1.0:
                        return best_cat, best_item
            for item in (cat.items or []):
                _consider(cat, item)
                if best_score >= 1.0:
                    return best_cat, best_item
        result = (best_cat, best_item) if best_score >= 0.5 else (None, None)
        return result

    @staticmethod
    def _normalize_activity_match_key(name: str) -> str:
        return re.sub(r"\s+", "", str(name or "").strip().lower())

    def _match_course_activity(
        self,
        item_name: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> Optional[CourseActivity]:
        if not item_name or not course_activities:
            return None

        query_lower = self._normalize_item_query(item_name).lower()
        query_key = self._normalize_activity_match_key(item_name)
        if not query_lower:
            return None

        best_activity: Optional[CourseActivity] = None
        best_score = 0.0
        for activity in course_activities:
            name_lower = str(getattr(activity, "name", "") or "").strip().lower()
            name_key = self._normalize_activity_match_key(name_lower)
            if not name_lower:
                continue
            if query_lower == name_lower or (query_key and query_key == name_key):
                return activity
            if query_lower in name_lower or name_lower in query_lower:
                score = min(len(query_lower), len(name_lower)) / max(len(query_lower), len(name_lower))
                if score > best_score:
                    best_score = score
                    best_activity = activity
            elif query_key and name_key and (query_key in name_key or name_key in query_key):
                score = min(len(query_key), len(name_key)) / max(len(query_key), len(name_key))
                if score > best_score:
                    best_score = score
                    best_activity = activity
        return best_activity if best_score >= 0.5 else None

    def _is_moodle_activity_item(
        self,
        item_name: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> bool:
        activity = self._match_course_activity(item_name, course_activities)
        if activity is None:
            return False

        module = str(getattr(activity, "module", None) or "").strip()
        cmid = getattr(activity, "cmid", None)
        itemtype = str(getattr(activity, "itemtype", None) or "mod").strip().lower()
        if itemtype == "manual" and not module and cmid in (None, "", 0):
            return False
        return bool(
            module
            or itemtype == "mod"
            or (cmid is not None and str(cmid).strip() not in {"", "0"})
        )

    def _find_not_graded_item(
        self,
        proposal: GradebookProposal,
        item_query: str,
    ) -> Optional[str]:
        query_lower = self._normalize_item_query(item_query).lower()
        if not query_lower:
            return None

        best_name: Optional[str] = None
        best_score = 0.0
        for item_name in (proposal.not_graded_items or []):
            name_lower = str(item_name).strip().lower()
            if not name_lower:
                continue
            if query_lower == name_lower:
                return str(item_name).strip()
            if query_lower in name_lower or name_lower in query_lower:
                score = min(len(query_lower), len(name_lower)) / max(len(query_lower), len(name_lower))
                if score > best_score:
                    best_score = score
                    best_name = str(item_name).strip()
        return best_name if best_score >= 0.5 else None

    def _resolve_item_for_placement(
        self,
        proposal: GradebookProposal,
        item_query: str,
    ) -> Tuple[Optional[GradebookCategory], Optional[str], bool]:
        src_cat, item_name = self._find_item_in_proposal(proposal, item_query)
        if item_name:
            return src_cat, item_name, False
        not_graded_name = self._find_not_graded_item(proposal, item_query)
        if not_graded_name:
            return None, not_graded_name, True
        return None, None, False

    def _remove_from_not_graded_items(self, proposal: GradebookProposal, item_name: str) -> None:
        if not proposal.not_graded_items:
            return
        target = str(item_name or "").strip().lower()
        proposal.not_graded_items = [
            name
            for name in proposal.not_graded_items
            if str(name).strip().lower() != target
        ]

    def _set_activity_not_graded(
        self,
        proposal: GradebookProposal,
        item_query: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> bool:
        activity = self._match_course_activity(item_query, course_activities)
        resolved_name: Optional[str] = None
        if activity is not None and getattr(activity, "name", None):
            resolved_name = str(activity.name).strip()
        else:
            _cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if item_name and self._is_moodle_activity_item(item_name, course_activities):
                resolved_name = item_name
            else:
                not_graded_name = self._find_not_graded_item(proposal, item_query)
                if not_graded_name and self._is_moodle_activity_item(not_graded_name, course_activities):
                    resolved_name = not_graded_name

        if not resolved_name:
            return False

        for category in proposal.categories or []:
            self._remove_item_from_category_tree(category, resolved_name)

        if proposal.not_graded_items is None:
            proposal.not_graded_items = []
        existing = {str(name).strip().lower() for name in proposal.not_graded_items}
        if resolved_name.lower() not in existing:
            proposal.not_graded_items.append(resolved_name)

        self._append_effect_note(
            proposal,
            f"Set Moodle activity '{resolved_name}' to Not graded (removed from proposal categories).",
        )
        return True

    def _apply_not_graded_operations(
        self,
        proposal: GradebookProposal,
        prompt: str,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> None:
        patterns = (
            r"\b(?:set|mark|make)\s+(.+?)\s+(?:to|as)\s+(?:not\s+graded|ungraded|non[- ]graded)\b",
            r"\b(?:don'?t|do\s+not)\s+grade\s+(.+?)(?:[.,]|$)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, prompt, re.IGNORECASE):
                item_query = match.group(1).strip().rstrip("., ")
                if not item_query:
                    continue
                for query in self._expand_item_queries(proposal, item_query):
                    self._set_activity_not_graded(proposal, query, course_activities=course_activities)

    def _pop_item_from_category_tree(
        self,
        parent: GradebookCategory,
        item_name: str,
    ) -> Optional[float]:
        """Remove an item from a parent or one of its subcategories; return prior weight if any."""
        if parent.items and item_name in parent.items:
            parent.items = [item for item in parent.items if item != item_name]
            return (parent.item_weights or {}).pop(item_name, None)
        for sub in (parent.subcategories or []):
            if sub.items and item_name in sub.items:
                sub.items = [item for item in sub.items if item != item_name]
                return (sub.item_weights or {}).pop(item_name, None)
        return None

    def _remove_item_from_category_tree(
        self,
        parent: GradebookCategory,
        item_name: str,
    ) -> bool:
        """Remove an item from a parent category or one of its subcategories."""
        if parent.items and item_name in parent.items:
            parent.items = [item for item in parent.items if item != item_name]
            if parent.item_weights:
                parent.item_weights.pop(item_name, None)
            return True
        for sub in (parent.subcategories or []):
            if sub.items and item_name in sub.items:
                sub.items = [item for item in sub.items if item != item_name]
                if sub.item_weights:
                    sub.item_weights.pop(item_name, None)
                return True
        return False

    def _remove_grade_item(
        self,
        proposal: GradebookProposal,
        item_query: str,
        location_hint: Optional[str] = None,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> bool:
        cat, item_name = self._find_item_in_proposal(proposal, item_query)
        if not cat or not item_name:
            return False

        if self._is_moodle_activity_item(item_name, course_activities):
            self._append_effect_note(
                proposal,
                (
                    f"Cannot remove Moodle activity '{item_name}'. "
                    "Move it, reassign it, or set it to Not graded instead."
                ),
            )
            return False

        removed = False
        if location_hint:
            sub_match = self._find_subcategory_match(proposal, location_hint, parent_hint=cat.name)
            if sub_match:
                scope_parent, target_sub = sub_match
                if scope_parent.name.lower() == cat.name.lower() and item_name in (target_sub.items or []):
                    target_sub.items = [item for item in target_sub.items if item != item_name]
                    if target_sub.item_weights:
                        target_sub.item_weights.pop(item_name, None)
                    removed = True
            else:
                targets = self._resolve_category_targets(proposal, location_hint)
                if targets and cat.name.lower() == targets[0].name.lower():
                    removed = self._remove_item_from_category_tree(cat, item_name)
        else:
            removed = self._remove_item_from_category_tree(cat, item_name)

        if not removed:
            return False

        self._append_effect_note(proposal, f"Removed '{item_name}' from {cat.name}")
        return True

    def _item_name_exists_in_category_tree(
        self,
        parent: GradebookCategory,
        dest_name: str,
        *,
        except_item: Optional[str] = None,
    ) -> bool:
        dest_lower = str(dest_name or "").strip().lower()
        except_lower = str(except_item or "").strip().lower()
        if not dest_lower:
            return False

        for item in (parent.items or []):
            item_lower = str(item).strip().lower()
            if item_lower == except_lower:
                continue
            if item_lower == dest_lower:
                return True

        for sub in (parent.subcategories or []):
            for item in (sub.items or []):
                item_lower = str(item).strip().lower()
                if item_lower == except_lower:
                    continue
                if item_lower == dest_lower:
                    return True
        return False

    def _rename_item_in_category_tree(
        self,
        parent: GradebookCategory,
        item_name: str,
        dest_name: str,
    ) -> bool:
        if parent.items and item_name in parent.items:
            parent.items = [dest_name if i == item_name else i for i in parent.items]
            if item_name in (parent.item_weights or {}):
                if parent.item_weights is None:
                    parent.item_weights = {}
                parent.item_weights[dest_name] = parent.item_weights.pop(item_name)
            return True

        for sub in (parent.subcategories or []):
            if sub.items and item_name in sub.items:
                sub.items = [dest_name if i == item_name else i for i in sub.items]
                if item_name in (sub.item_weights or {}):
                    if sub.item_weights is None:
                        sub.item_weights = {}
                    sub.item_weights[dest_name] = sub.item_weights.pop(item_name)
                return True
        return False

    def _assign_item_to_subcategory(
        self,
        proposal: GradebookProposal,
        item_query: str,
        sub_hint: str,
        parent_hint: Optional[str] = None,
        content_mapping: Optional[dict] = None,
    ) -> bool:
        src_cat, item_name, from_not_graded = self._resolve_item_for_placement(proposal, item_query)
        if not item_name:
            return False

        parent_name = str(parent_hint or (src_cat.name if src_cat else "") or "").strip()
        sub_match = self._find_subcategory_match(proposal, sub_hint, parent_hint=parent_name)
        if not sub_match:
            return False

        parent_cat, target_sub = sub_match
        if from_not_graded:
            self._remove_from_not_graded_items(proposal, item_name)
            prior_weight = None
        else:
            if not src_cat:
                return False
            prior_weight = self._pop_item_from_category_tree(src_cat, item_name)
        if target_sub.items is None:
            target_sub.items = []
        if item_name not in target_sub.items:
            target_sub.items.append(item_name)
        if prior_weight is not None:
            if target_sub.item_weights is None:
                target_sub.item_weights = {}
            target_sub.item_weights[item_name] = prior_weight

        if isinstance(content_mapping, dict):
            for row in list(content_mapping.get("graded_activities") or []):
                if str(row.get("activity_name") or "").strip().lower() != item_name.strip().lower():
                    continue
                row["suggested_category"] = parent_cat.name
                row["confirmed_category"] = parent_cat.name
                row.pop("not_graded", None)
                _set_row_subcategory(row, target_sub.name)

        self._append_effect_note(
            proposal,
            f"Moved '{item_name}' to subcategory '{target_sub.name}' in {parent_cat.name}",
        )
        return True

    def _expand_item_queries(self, proposal: GradebookProposal, item_query: str) -> List[str]:
        """Expand a move/remove item phrase into one or more item queries.

        Supports separators like comma, "and", and "&" while preserving
        single-item phrases when they already match an item exactly.
        """
        raw = str(item_query or "").strip().rstrip("., ")
        if not raw:
            return []

        # Keep as single query when it already resolves exactly.
        _, matched = self._find_item_in_proposal(proposal, raw)
        if matched and matched.strip().lower() == raw.lower():
            return [raw]

        parts = [
            part.strip().strip("'\"").rstrip("., ")
            for part in re.split(r"\s*(?:,|\band\b|&)\s*", raw, flags=re.IGNORECASE)
            if part and part.strip()
        ]
        if len(parts) <= 1:
            return [raw]

        # Return unique parts preserving order.
        unique_parts: List[str] = []
        seen: set[str] = set()
        for part in parts:
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_parts.append(part)
        return unique_parts or [raw]

    def _clear_item_subcategory_assignment(
        self,
        proposal: GradebookProposal,
        item_query: str,
        parent_hint: Optional[str] = None,
        content_mapping: Optional[dict] = None,
    ) -> bool:
        src_cat, item_name = self._find_item_in_proposal(proposal, item_query)
        if not src_cat or not item_name:
            return False

        prior_weight = self._pop_item_from_category_tree(src_cat, item_name)
        if src_cat.items is None:
            src_cat.items = []
        if item_name not in src_cat.items:
            src_cat.items.append(item_name)
        if prior_weight is not None:
            if src_cat.item_weights is None:
                src_cat.item_weights = {}
            src_cat.item_weights[item_name] = prior_weight

        if isinstance(content_mapping, dict):
            for row in list(content_mapping.get("graded_activities") or []):
                if str(row.get("activity_name") or "").strip().lower() != item_name.strip().lower():
                    continue
                _set_row_subcategory(row, "")

        self._append_effect_note(
            proposal,
            f"Moved '{item_name}' to parent category '{src_cat.name}'",
        )
        return True

    def _apply_subcategory_item_assignments(
        self,
        proposal: GradebookProposal,
        prompt: str,
        content_mapping: Optional[dict] = None,
    ) -> None:
        """Assign grade items to subcategories from chat prompts."""
        parent_hint = self._extract_scoped_parent_label(prompt)

        clear_patterns = (
            re.compile(
                r"\b(?:clear|remove|reset)\s+(?:the\s+)?subcategory\s+(?:for|of|on)\s+(.+?)(?:\s+(?:in|under)\s+.+)?\s*(?:[.,]|$)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:move|put|place)\s+(.+?)\s+(?:to|into)\s+(?:the\s+)?(?:parent\s+)?category(?:\s+(?:in|under)\s+.+)?\s*(?:[.,]|$)",
                re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:move|put|place)\s+(.+?)\s+(?:to|into)\s+none(?:\s+(?:in|under)\s+.+)?\s*(?:[.,]|$)",
                re.IGNORECASE,
            ),
        )
        for pattern in clear_patterns:
            for match in pattern.finditer(prompt):
                item_query = match.group(1).strip().rstrip("., ")
                for query in self._expand_item_queries(proposal, item_query):
                    self._clear_item_subcategory_assignment(
                        proposal,
                        query,
                        parent_hint=parent_hint,
                        content_mapping=content_mapping,
                    )

        scoped_move = re.compile(
            r"\b(?:move|transfer|reassign|put|place|assign)\s+(.+?)"
            r"\s+(?:from\s+[a-zA-Z][a-zA-Z ]{0,40}\s+)?(?:to|into|under)\s+"
            r"([a-zA-Z][a-zA-Z \-]{1,40}?)(?:\s+subcategory)?"
            r"\s+(?:in|under)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\s*(?:[.,]|$)",
            re.IGNORECASE,
        )
        for match in scoped_move.finditer(prompt):
            item_query = match.group(1).strip().rstrip("., ")
            sub_hint = match.group(2).strip().rstrip("., ")
            scoped_parent = match.group(3).strip().rstrip("., ")
            if re.search(r"\b(?:parent|none)\s+category\b", sub_hint, re.IGNORECASE):
                continue
            for query in self._expand_item_queries(proposal, item_query):
                self._assign_item_to_subcategory(
                    proposal,
                    query,
                    sub_hint,
                    parent_hint=scoped_parent,
                    content_mapping=content_mapping,
                )

        direct_move = re.compile(
            r"\b(?:move|transfer|reassign|put|place|assign)\s+(.+?)"
            r"\s+(?:from\s+[a-zA-Z][a-zA-Z ]{0,40}\s+)?(?:to|into|under)\s+"
            r"([a-zA-Z][a-zA-Z \-]{1,40}?)(?:\s+subcategory)?\s*(?:[.,]|$)",
            re.IGNORECASE,
        )
        for match in direct_move.finditer(prompt):
            if re.search(r"\s+(?:in|under)\s+", match.group(0), re.IGNORECASE):
                continue
            item_query = match.group(1).strip().rstrip("., ")
            target_hint = match.group(2).strip().rstrip("., ")
            if re.search(r"\b(?:parent|none)\s+category\b", target_hint, re.IGNORECASE):
                continue
            for query in self._expand_item_queries(proposal, item_query):
                src_cat, _ = self._find_item_in_proposal(proposal, query)
                sub_parent_hint = parent_hint or (src_cat.name if src_cat else "")
                if self._find_subcategory_match(proposal, target_hint, parent_hint=sub_parent_hint):
                    self._assign_item_to_subcategory(
                        proposal,
                        query,
                        target_hint,
                        parent_hint=sub_parent_hint,
                        content_mapping=content_mapping,
                    )
                    continue
                if self._resolve_category_targets(proposal, target_hint):
                    continue
                self._assign_item_to_subcategory(
                    proposal,
                    query,
                    target_hint,
                    parent_hint=parent_hint,
                    content_mapping=content_mapping,
                )

    def _apply_item_operations(self, proposal: GradebookProposal, prompt: str) -> None:
        """Handle move / weight-set / remove operations on grade items inside categories."""
        self._apply_subcategory_item_assignments(proposal, prompt)

        # --- Move: "move <item> to <category>" / "move <item> from <A> to <B>" --------
        for m in re.finditer(
            r"\b(?:move|transfer|reassign|put|place)\s+"
            r"(.+?)"
            r"\s+(?:from\s+[a-zA-Z][a-zA-Z ]{0,30}\s+)?(?:to|into|under)\s+"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\s*(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            target_hint = m.group(2).strip().rstrip("., ")
            if re.search(r"\s+(?:in|under)\s+", prompt[m.start():m.end()], re.IGNORECASE):
                continue
            for query in self._expand_item_queries(proposal, item_query):
                src_cat, item_name, from_not_graded = self._resolve_item_for_placement(proposal, query)
                if not item_name:
                    continue
                if self._find_subcategory_match(proposal, target_hint, parent_hint=src_cat.name if src_cat else None):
                    continue
                targets = self._resolve_category_targets(proposal, target_hint)
                if not targets:
                    continue
                target_cat = targets[0]
                if src_cat and src_cat.name == target_cat.name:
                    # Move from subcategory to parent level when target is the same parent category.
                    already_parent = item_name in (target_cat.items or [])
                    if already_parent:
                        continue
                    prior_weight = self._pop_item_from_category_tree(src_cat, item_name)
                    if target_cat.items is None:
                        target_cat.items = []
                    if item_name not in target_cat.items:
                        target_cat.items.append(item_name)
                    if prior_weight is not None:
                        if target_cat.item_weights is None:
                            target_cat.item_weights = {}
                        target_cat.item_weights[item_name] = prior_weight
                    self._remove_from_not_graded_items(proposal, item_name)
                    self._append_effect_note(
                        proposal,
                        f"Moved '{item_name}' to parent category '{target_cat.name}'",
                    )
                    continue
                if from_not_graded:
                    self._remove_from_not_graded_items(proposal, item_name)
                    prior_weight = None
                elif src_cat:
                    prior_weight = self._pop_item_from_category_tree(src_cat, item_name)
                    if prior_weight is not None:
                        if target_cat.item_weights is None:
                            target_cat.item_weights = {}
                        target_cat.item_weights[item_name] = prior_weight
                if target_cat.items is None:
                    target_cat.items = []
                if item_name not in target_cat.items:
                    target_cat.items.append(item_name)
                if from_not_graded:
                    from_label = " from not graded"
                else:
                    from_label = f" from {src_cat.name}" if src_cat else ""
                self._append_effect_note(proposal, f"Moved '{item_name}'{from_label} to {target_cat.name}")

        # --- Item weight: "set weight of <item> in <category> to N" -------------------
        for m in re.finditer(
            r"\b(?:set|assign|give)\s+(?:the\s+)?weight\s+of\s+(?:(?:manual\s+)?grade\s+item\s+)?(.+?)\s+in\s+"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)\s+to\s+(\d+(?:\.\d+)?)\s*%?(?:\s|,|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            parent_hint = m.group(2).strip().rstrip("., ")
            raw_val = float(m.group(3))
            parent_targets = self._resolve_category_targets(proposal, parent_hint)
            if not parent_targets:
                continue
            parent_cat = parent_targets[0]
            cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if not cat or not item_name or cat.name.lower() != parent_cat.name.lower():
                continue
            weight_val = raw_val if raw_val > 1.0 else round(raw_val * 100, 2)
            self._set_item_weight_with_validation(proposal, cat, item_name, weight_val)

        # --- Item weight: "set weight of <item> to N" ---------------------------------
        for m in re.finditer(
            r"\b(?:set|assign|give)\s+(?:the\s+)?weight\s+(?:of\s+)(.+?)\s+to\s+(\d+(?:\.\d+)?)\s*%?(?:\s|,|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            raw_val = float(m.group(2))
            cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if not cat or not item_name:
                continue
            weight_val = raw_val if raw_val > 1.0 else round(raw_val * 100, 2)
            self._set_item_weight_with_validation(proposal, cat, item_name, weight_val)

        # --- Item weight: "assign <item> to N" --------------------------------------
        for m in re.finditer(
            r"\bassign\s+(.+?)\s+(?:to|=)\s+(\d+(?:\.\d+)?)\s*%?(?:\s|,|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            raw_val = float(m.group(2))
            # Keep split-style "assign 0.4 to Midterm Quiz" for subcategory handler.
            # Do not skip item-level weights like "assign quiz 1 to 40".
            if raw_val <= 1.0 and self._resolve_category_targets(proposal, item_query):
                continue
            cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if not cat or not item_name:
                continue
            weight_val = raw_val if raw_val > 1.0 else round(raw_val * 100, 2)
            self._set_item_weight_with_validation(proposal, cat, item_name, weight_val)

        # --- Item weight: "<item> weight N%" -----------------------------------------
        for m in re.finditer(
            r"(.+?)\s+weight\s+(\d+(?:\.\d+)?)\s*%",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            raw_val = float(m.group(2))
            # Skip if item_query matches a category name (let category-weight handler take it)
            if self._resolve_category_targets(proposal, item_query):
                continue
            cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if not cat or not item_name:
                continue
            self._set_item_weight_with_validation(proposal, cat, item_name, raw_val)

    def _apply_item_remove_rename(
        self,
        proposal: GradebookProposal,
        prompt: str,
        renamed_subcategory_sources: Optional[set[str]] = None,
        course_activities: Optional[List[CourseActivity]] = None,
    ) -> None:
        """Handle explicit grade-item remove/rename commands before category removal logic."""
        remove_kwargs = {"course_activities": course_activities}
        # Remove with explicit category context: "remove X from Final Exam"
        for m in re.finditer(
            r"\b(?:remove|delete|unassign)\s+(.+?)\s+from\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\s*(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            cat_hint = m.group(2).strip().rstrip("., ")
            if not item_query:
                continue
            self._remove_grade_item(proposal, item_query, location_hint=cat_hint, **remove_kwargs)

        # Remove with prefix phrasing: "remove grade item X"
        for m in re.finditer(
            r"\b(?:remove|delete|unassign)\s+(?:the\s+)?(?:(?:manual\s+)?grade\s+item)\s+(.+?)(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            if not item_query:
                continue
            self._remove_grade_item(proposal, item_query, **remove_kwargs)

        # Remove without explicit category: "remove fina grade item" / "delete grade item fina"
        for m in re.finditer(
            r"\b(?:remove|delete|unassign)\s+(?:the\s+)?(?:(?:manual\s+)?grade\s+item\s+)?(.+?)\s+(?:grade\s+item|item)\b",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            if not item_query:
                continue
            self._remove_grade_item(proposal, item_query, **remove_kwargs)

        # Bare remove by item name: "remove manual_item"
        for m in re.finditer(
            r"\b(?:remove|delete|unassign)\s+(?:the\s+)?(.+?)(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_query = m.group(1).strip().rstrip("., ")
            if not item_query or re.search(r"\bfrom\b", item_query, flags=re.IGNORECASE):
                continue
            if re.search(r"\b(?:grade\s+item|item)\b", item_query, flags=re.IGNORECASE):
                continue
            if any(
                self._normalize_name(cat.name) == self._normalize_name(item_query)
                for cat in proposal.categories
            ):
                continue
            cat, item_name = self._find_item_in_proposal(proposal, item_query)
            if not cat or not item_name:
                continue
            self._remove_grade_item(proposal, item_query, **remove_kwargs)

        # Rename item: "rename grade item fina to final" / "rename fina item to final"
        for m in re.finditer(
            r"\brename\s+(?:the\s+)?(?:(?:manual\s+)?grade\s+item\s+)?(.+?)\s+(?:grade\s+item\s+|item\s+)?to\s+(.+?)(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            src_query = m.group(1).strip().rstrip("., ")
            dest_name = m.group(2).strip().rstrip("., ")
            if not src_query or not dest_name:
                continue

            rename_phrase = m.group(0) or ""
            explicit_item_rename = bool(
                re.search(r"\b(?:grade\s+item|\bitem\b)\b", rename_phrase, flags=re.IGNORECASE)
            )

            # Guard against category rename phrases only when the source text
            # matches an existing category name directly (not fuzzy aliases).
            src_norm = self._normalize_name(src_query)
            if any(self._normalize_name(cat.name) == src_norm for cat in proposal.categories):
                continue

            src_key = self._normalize_name(src_query)
            renamed_sub_sources = renamed_subcategory_sources or set()

            # Subcategory renames run first; avoid renaming grade items when the
            # source label matches a subcategory (e.g. "Homework" vs "Homework 1").
            if not explicit_item_rename and (
                src_key in renamed_sub_sources
                or any(
                    self._subcategory_labels_match(sub.name, src_query)
                    for category in proposal.categories
                    for sub in (category.subcategories or [])
                )
            ):
                continue

            cat, item_name = self._find_item_in_proposal(proposal, src_query)
            if not cat or not item_name:
                continue

            if self._item_name_exists_in_category_tree(cat, dest_name, except_item=item_name):
                continue

            if not self._rename_item_in_category_tree(cat, item_name, dest_name):
                continue

            self._append_effect_note(proposal, f"Renamed '{item_name}' to '{dest_name}' in {cat.name}")

    def _apply_item_additions(self, proposal: GradebookProposal, prompt: str) -> None:
        """Handle direct creation of manual grade items from chat prompts."""
        for m in re.finditer(
            r"\b(?:add|create|make|insert)\s+(?:a\s+)?(?:(?:manual\s+)?grade\s+item|manual\s+item)\s+"
            r"(.+?)\s+(?:to|into|under|for)\s+(?:(?:sub)?category\s+)?([a-zA-Z][a-zA-Z0-9 '\-]{1,40}?)(?:\s+(?:sub)?category)?\s*(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_name = m.group(1).strip().rstrip("., ")
            target_hint = m.group(2).strip().rstrip("., ")
            if not item_name:
                continue

            sub_match = self._find_subcategory_match(proposal, target_hint)
            if sub_match:
                parent_cat, target_sub = sub_match
                self._append_manual_grade_item_to_sub(proposal, item_name, parent_cat, target_sub)
                continue

            targets = self._resolve_category_targets(proposal, target_hint)
            if not targets:
                continue
            self._append_manual_grade_item(proposal, item_name, targets[0])

        # Bare form: "add x to y" should create a manual grade item,
        # unless the user explicitly asked for subcategory/category creation.
        for m in re.finditer(
            r"\b(?:add|create|make|insert)\s+([a-zA-Z][a-zA-Z0-9 '\-]{0,60}?)\s+"
            r"(?:to|into|under|for)\s+(?:(?:sub)?category\s+)?([a-zA-Z][a-zA-Z0-9 '\-]{1,40}?)(?:\s+(?:sub)?category)?\s*(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            item_name = m.group(1).strip().rstrip("., ")
            target_hint = m.group(2).strip().rstrip("., ")
            if not item_name:
                continue
            if re.search(r"\b(?:subcategor(?:y|ies)|category|grade\s+item|manual\s+item)\b", item_name, re.IGNORECASE):
                continue
            if re.search(r"\bwith\s+(?:weight\s+)?(?:of\s+)?\d+(?:\.\d+)?\s*%\b", m.group(0), re.IGNORECASE):
                continue

            sub_match = self._find_subcategory_match(proposal, target_hint)
            if sub_match:
                parent_cat, target_sub = sub_match
                self._append_manual_grade_item_to_sub(proposal, item_name, parent_cat, target_sub)
                continue

            targets = self._resolve_category_targets(proposal, target_hint)
            if not targets:
                continue
            self._append_manual_grade_item(proposal, item_name, targets[0])

        for m in re.finditer(
            r"\b(?:add|create|make|insert)\s+(?:a\s+)?(?:(?:manual\s+)?grade\s+item|manual\s+item)\s+"
            r"([a-zA-Z][a-zA-Z0-9 '\-]{0,60}?)\s*(?:[.,]|$)",
            prompt,
            re.IGNORECASE,
        ):
            if re.search(r"\s+(?:to|into|under|for)\s+", m.group(0), re.IGNORECASE):
                continue
            item_name = m.group(1).strip().rstrip("., ")
            if not item_name:
                continue
            target_cat = self._default_manual_item_parent(proposal)
            if target_cat is None:
                continue
            self._append_manual_grade_item(proposal, item_name, target_cat)

    def _set_item_weight_with_validation(
        self,
        proposal: GradebookProposal,
        category: GradebookCategory,
        item_name: str,
        weight_pct: float,
    ) -> None:
        """Set per-item weight with range and category-total validation."""
        value = round(float(weight_pct), 2)
        if value < 0.0 or value > 100.0:
            self._append_unique_note(
                proposal,
                f"Item weight check: '{item_name}' in {category.name} must be between 0% and 100%.",
            )
            return

        current = float((category.item_weights or {}).get(item_name, 0.0))
        current_total = sum(float(v or 0.0) for v in (category.item_weights or {}).values())
        new_total = current_total - current + value
        if new_total > 100.0 + 0.001:
            self._append_unique_note(
                proposal,
                f"Item weight check: {category.name} item weights would total {new_total:.1f}% (max 100%).",
            )
            return

        category.item_weights[item_name] = value
        self._append_effect_note(proposal, f"Set item weight for '{item_name}' in {category.name} to {value:.1f}%")

    def _extract_subcategory_weight_intents(
        self,
        prompt: str,
    ) -> Tuple[str, List[Dict[str, object]]]:
        """Pull scoped subcategory-weight clauses out before category-weight parsing."""
        if not prompt:
            return "", []

        intents: List[Dict[str, object]] = []
        cleaned = prompt

        patterns: List[Tuple[re.Pattern, str]] = [
            (
                re.compile(
                    r"\b(?:set|assign|update|change|make)\s+(?:the\s+)?weight\s+of\s+"
                    r"(?:subcategory\s+)?(.+?)\s+in\s+([a-zA-Z][a-zA-Z '\-]{1,40}?)\s+"
                    r"(?:to|as|=)\s+(\d+(?:\.\d+)?)\s*%?\b",
                    flags=re.IGNORECASE,
                ),
                "scoped",
            ),
            (
                re.compile(
                    r"\b(?:set|assign|update|change|make)\s+(?:subcategory\s+)?(.+?)\s+subcategory\s+weight\s+"
                    r"(?:in\s+([a-zA-Z][a-zA-Z '\-]{1,40}?)\s+)?(?:to|as|=)\s+(\d+(?:\.\d+)?)\s*%?\b",
                    flags=re.IGNORECASE,
                ),
                "keyword",
            ),
            (
                re.compile(
                    r"\b(?:set|assign|update|change|make)\s+subcategory\s+(.+?)\s+"
                    r"(?:in\s+([a-zA-Z][a-zA-Z '\-]{1,40}?)\s+)?(?:weight\s+)?(?:to|as|=)\s+(\d+(?:\.\d+)?)\s*%?\b",
                    flags=re.IGNORECASE,
                ),
                "subkeyword",
            ),
        ]

        for pattern, kind in patterns:
            def _collect(match: re.Match, intent_kind: str = kind) -> str:
                if intent_kind == "scoped":
                    sub_name = match.group(1).strip().rstrip(".,;:!?")
                    parent_hint = match.group(2).strip().rstrip(".,;:!?")
                    weight = float(match.group(3))
                else:
                    sub_name = match.group(1).strip().rstrip(".,;:!?")
                    parent_hint = (match.group(2) or "").strip().rstrip(".,;:!?")
                    weight = float(match.group(3))
                sub_name = re.sub(r"^subcategory\s+", "", sub_name, flags=re.IGNORECASE).strip()
                if sub_name:
                    intents.append(
                        {
                            "sub_name": sub_name,
                            "parent_hint": parent_hint or None,
                            "weight": weight,
                        }
                    )
                return " "

            cleaned = pattern.sub(_collect, cleaned)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned, intents

    def _apply_subcategory_weight_intents(
        self,
        proposal: GradebookProposal,
        intents: List[Dict[str, object]],
    ) -> None:
        for intent in intents or []:
            if not isinstance(intent, dict):
                continue
            sub_name = str(intent.get("sub_name") or "").strip()
            parent_hint = intent.get("parent_hint")
            parent_hint_str = str(parent_hint).strip() if parent_hint else None
            try:
                parsed_weight = float(intent.get("weight"))
            except (TypeError, ValueError):
                continue
            if not sub_name:
                continue

            match = self._find_subcategory_match(
                proposal,
                sub_name,
                parent_hint=parent_hint_str,
            )
            if match is None:
                self._append_unique_note(
                    proposal,
                    f"Subcategory weight check: could not find subcategory '{sub_name}'"
                    + (f" in {parent_hint_str}" if parent_hint_str else "")
                    + ".",
                )
                continue

            matched_parent, matched_sub = match
            resolved_weight = self._parse_subcategory_weight_value(
                str(parsed_weight),
                float(matched_parent.weight),
            )
            if resolved_weight is None:
                continue

            matched_sub.weight = round(float(resolved_weight), 2)
            self._append_effect_note(
                proposal,
                f"Set {matched_sub.name} split weight in {matched_parent.name} to {matched_sub.weight:.1f}%",
            )

    def _apply_subcategory_weight_updates(self, proposal: GradebookProposal, prompt: str) -> None:
        patterns = (
            re.compile(
                r"\b(?:set|assign|update|change|make)\s+weight\s+(?:of\s+)?([a-zA-Z][a-zA-Z\- ]{1,60}?)\s+(?:to|as|=)\s+(\d+(?:\.\d+)?)\s*%?\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:set|assign|update|change|make)\s+(\d+(?:\.\d+)?)\s*%?\s+(?:to|for)\s+([a-zA-Z][a-zA-Z\- ]{1,60})\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b([a-zA-Z][a-zA-Z\- ]{1,60})\s+weight\s+(?:is|to|=)\s+(\d+(?:\.\d+)?)\s*%?\b",
                flags=re.IGNORECASE,
            ),
        )

        updates: List[Tuple[str, float]] = []
        for idx, pattern in enumerate(patterns):
            for match in pattern.finditer(prompt):
                if idx == 1:
                    raw_weight = match.group(1)
                    raw_name = match.group(2)
                else:
                    raw_name = match.group(1)
                    raw_weight = match.group(2)
                if not raw_name or not raw_weight:
                    continue
                raw_name = re.sub(r"\s+subcategory\s*$", "", raw_name.strip(), flags=re.IGNORECASE).strip()
                updates.append((raw_name.strip().rstrip(".,;:!?"), float(raw_weight)))

        if not updates:
            return

        for raw_name, parsed_weight in updates:
            normalized_target = self._normalize_name(raw_name)
            if not normalized_target:
                continue

            matched_parent: Optional[GradebookCategory] = None
            matched_sub: Optional[GradebookSubcategory] = None

            for category in proposal.categories:
                for sub in (category.subcategories or []):
                    if self._subcategory_labels_match(sub.name, raw_name):
                        matched_parent = category
                        matched_sub = sub
                        break
                if matched_sub is not None:
                    break

            if matched_parent is None or matched_sub is None:
                continue

            resolved_weight = self._parse_subcategory_weight_value(str(parsed_weight), float(matched_parent.weight))
            if resolved_weight is None:
                continue

            matched_sub.weight = round(float(resolved_weight), 2)
            self._append_effect_note(
                proposal,
                f"Set {matched_sub.name} split weight in {matched_parent.name} to {matched_sub.weight:.1f}%",
            )

    def _parse_weight_assignments(
        self,
        proposal: GradebookProposal,
        prompt: str,
        skip_split_pieces: bool = False,
    ) -> Dict[str, float]:
        # Strip item-weight clauses so they don't get misread as category weights.
        # Example: "set weight of quiz 1 to 40" must not become "Quizzes = 1".
        item_clause_patterns = (
            re.compile(
                r"\b(?:set|assign|give)\s+(?:the\s+)?weight\s+of\s+(.+?)\s+to\s+\d+(?:\.\d+)?\s*%?(?:\s|,|$)",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\bassign\s+(.+?)\s+(?:to|=)\s+\d+(?:\.\d+)?\s*%?(?:\s|,|$)",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(.+?)\s+weight\s+\d+(?:\.\d+)?\s*%(?:\s|,|$)",
                flags=re.IGNORECASE,
            ),
        )
        prompt_for_categories = prompt
        for pattern in item_clause_patterns:
            def _strip_item_clause(match: re.Match) -> str:
                item_query = (match.group(1) or "").strip().rstrip(".,;:!?")
                _, item_name = self._find_item_in_proposal(proposal, item_query)
                return " " if item_name else match.group(0)

            prompt_for_categories = pattern.sub(_strip_item_clause, prompt_for_categories)

        # Handle "from X% to Y%" — only use the destination value Y.
        clean_prompt = re.sub(
            r"(\bfrom\s+\d+(?:\.\d+)?\s*%\s*to\b)",
            "to",
            prompt_for_categories,
            flags=re.IGNORECASE,
        )

        split_piece_names: List[str] = []
        if skip_split_pieces:
            split_piece_names = self._extract_split_piece_names(prompt)
            # Also strip split payload text to avoid parsing subcategory weights as top-level weights.
            clean_prompt = re.sub(r"\bsplit\b.+", "", clean_prompt, flags=re.IGNORECASE).strip()
            clean_prompt = re.sub(r"\bdivide\b.+", "", clean_prompt, flags=re.IGNORECASE).strip()
            clean_prompt = re.sub(r"\badd\s+subcategories\b\s*[:;]?.+", "", clean_prompt, flags=re.IGNORECASE).strip()

        # Guard against parsing non-weight numeric settings (grade min/max/pass, dates, decimals, hide/lock).
        settings_context = bool(re.search(
            r"\b("
            r"minimum|maximum|max|min|passing|grade\s+pass|grade\s+max|grade\s+min"
            r"|decimal|display|percentage|letter|real"
            r"|hide|hidden|show|unhide|reveal|lock|unlock|until|on\s+\d{4}-\d{2}-\d{2}"
            r"|drop\s+lowest|keep\s+(?:top|best|highest)"
            r"|include\s+empty|exclude\s+empty|outcomes?|extra\s+credit"
            r")\b",
            clean_prompt,
            flags=re.IGNORECASE,
        ))

        matches = []

        # Explicit percentage always counts as weight intent.
        percent_pattern = re.compile(
            r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\s*%",
            flags=re.IGNORECASE,
        )
        matches.extend(percent_pattern.findall(clean_prompt))
        matches.extend(re.findall(
            r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s+(\d+(?:\.\d+)?)\s*%(?=\D|$)",
            clean_prompt,
            flags=re.IGNORECASE,
        ))

        # Explicit "weight" keyword counts even without %.
        weight_keyword_pattern = re.compile(
            r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
            r"([a-zA-Z][a-zA-Z ]{1,40}?)\s+weight\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\b",
            flags=re.IGNORECASE,
        )
        matches.extend(weight_keyword_pattern.findall(clean_prompt))

        # Generic no-% parsing is only safe when prompt is not about other numeric settings.
        if not settings_context:
            # Imperative no-% phrasing: "make labs 20" / "set assignments 35".
            # This restores compact commands without requiring "to/is" or "%".
            direct_no_percent_pattern = re.compile(
                r"\b(?:set|make|change|adjust|update|increase|decrease)\s+"
                r"([a-zA-Z][a-zA-Z ]{1,40}?)\s+(\d+(?:\.\d+)?)\b",
                flags=re.IGNORECASE,
            )
            matches.extend(direct_no_percent_pattern.findall(clean_prompt))

            # Chained no-% assignments: "make midterm 24 and final 36".
            # Parse each category-number pair so both updates are applied.
            chained_no_percent_pattern = re.compile(
                r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s+(\d+(?:\.\d+)?)(?=\s*(?:,|and\b|$))",
                flags=re.IGNORECASE,
            )
            matches.extend(chained_no_percent_pattern.findall(clean_prompt))

            generic_pattern = re.compile(
                r"(?:set|make|change|adjust|update|keep|use|increase|decrease)?\s*"
                r"([a-zA-Z][a-zA-Z ]{1,40}?)\s*(?:is|are|to|=|:)\s*(\d+(?:\.\d+)?)\b",
                flags=re.IGNORECASE,
            )
            matches.extend(generic_pattern.findall(clean_prompt))

            # Support compact category lists like "Assignments 35, Labs 15".
            if re.search(r"\b(?:gradebook\s+with|categories|weights?)\b", clean_prompt, flags=re.IGNORECASE):
                matches.extend(re.findall(
                    r"\b([a-zA-Z][a-zA-Z ]{1,30}?)\s*(\d+(?:\.\d+)?)\s*%?\b",
                    clean_prompt,
                    flags=re.IGNORECASE,
                ))

        weights: Dict[str, float] = {}
        allow_new_categories = self._allow_new_categories_from_prompt(prompt)
        for raw_name, raw_weight in matches:
            if skip_split_pieces and raw_name.strip().lower() in split_piece_names:
                continue
            if re.search(r"\bin\b", raw_name, flags=re.IGNORECASE):
                parts = re.split(r"\bin\b", raw_name, maxsplit=1, flags=re.IGNORECASE)
                if len(parts) == 2:
                    sub_part = parts[0].strip().rstrip(".,;:!?")
                    parent_part = parts[1].strip().rstrip(".,;:!?")
                    if sub_part and parent_part and self._find_subcategory_match(
                        proposal,
                        sub_part,
                        parent_hint=parent_part,
                    ):
                        continue
            resolved = self._resolve_category_name(proposal, raw_name)
            if resolved is None and allow_new_categories:
                candidate = self._clean_new_category_name(raw_name)
                if candidate:
                    resolved = candidate
            if resolved is None:
                continue
            # Last occurrence wins for the same category (handles repeat mentions).
            weights[resolved] = float(raw_weight)
        return weights

    @staticmethod
    def _allow_new_categories_from_prompt(prompt: str) -> bool:
        prompt_l = prompt.lower()
        if re.search(r"\b(do\s+not\s+create|don't\s+create|dont\s+create|dont\s+add|don't\s+add|keep\s+categories\s+unchanged|only\s+rebalance|keep\s+all\s+else)\b", prompt_l):
            return False
        return bool(re.search(r"\b(create|build|propose|start\s+with|gradebook\s+with|categories?)\b", prompt_l))

    @staticmethod
    def _clean_new_category_name(raw_name: str) -> Optional[str]:
        name = re.sub(r"\s+", " ", (raw_name or "").strip())
        name = re.sub(r"^[^a-zA-Z]+|[^a-zA-Z]+$", "", name)
        if not name:
            return None
        if len(name.split()) > 3:
            return None
        return name.title()

    @staticmethod
    def _clean_manual_item_name(raw_name: str) -> str:
        name = re.sub(r"\s+", " ", (raw_name or "").strip()).rstrip("., ")
        name = re.sub(r"^(?:grade\s+item|manual\s+item|item)\s+", "", name, flags=re.IGNORECASE)
        return name.strip()

    @staticmethod
    def _normalize_item_query(item_query: str) -> str:
        cleaned = ProposalGenerator._clean_manual_item_name(item_query)
        return cleaned or (item_query or "").strip()

    def _default_manual_item_parent(self, proposal: GradebookProposal) -> Optional[GradebookCategory]:
        for category in proposal.categories:
            if category.subcategories:
                return category
        return proposal.categories[0] if proposal.categories else None

    def _append_manual_grade_item(
        self,
        proposal: GradebookProposal,
        item_name: str,
        target_cat: GradebookCategory,
    ) -> bool:
        cleaned_name = self._clean_manual_item_name(item_name)
        if not cleaned_name:
            return False
        if any(str(category.name).strip().lower() == cleaned_name.lower() for category in proposal.categories):
            return False
        if any(str(existing).strip().lower() == cleaned_name.lower() for existing in (target_cat.items or [])):
            return False
        for sub in (target_cat.subcategories or []):
            if any(str(existing).strip().lower() == cleaned_name.lower() for existing in (sub.items or [])):
                return False
        if target_cat.items is None:
            target_cat.items = []
        target_cat.items.append(cleaned_name)
        self._append_effect_note(
            proposal,
            f"Added manual grade item '{cleaned_name}' to {target_cat.name}",
        )
        return True

    def _append_manual_grade_item_to_sub(
        self,
        proposal: GradebookProposal,
        item_name: str,
        parent_cat: GradebookCategory,
        target_sub: "GradebookSubcategory",
    ) -> bool:
        """Add a manual grade item directly into a subcategory."""
        cleaned_name = self._clean_manual_item_name(item_name)
        if not cleaned_name:
            return False
        if any(str(existing).strip().lower() == cleaned_name.lower() for existing in (target_sub.items or [])):
            return False
        if target_sub.items is None:
            target_sub.items = []
        target_sub.items.append(cleaned_name)
        self._append_effect_note(
            proposal,
            f"Added manual grade item '{cleaned_name}' to {parent_cat.name} > {target_sub.name}",
        )
        return True

    @staticmethod
    def _should_rebuild_category_set(prompt: str, weights: Dict[str, float]) -> bool:
        if len(weights) < 3:
            return False
        prompt_l = prompt.lower()
        if re.search(r"\b(change|adjust|update|increase|decrease|rebalance|remove|split|undo|redo)\b", prompt_l):
            return False
        return bool(re.search(r"\b(create|build|propose|start\s+with)\b", prompt_l))

    @staticmethod
    def _rebuild_categories_from_weights(proposal: GradebookProposal, weights: Dict[str, float]) -> List[GradebookCategory]:
        existing = {cat.name.lower(): cat for cat in proposal.categories}
        rebuilt: List[GradebookCategory] = []
        for name, weight in weights.items():
            src = existing.get(name.lower())
            rebuilt.append(
                GradebookCategory(
                    name=name,
                    weight=float(weight),
                    items=list(src.items) if src else [],
                    drop_lowest=getattr(src, "drop_lowest", 0) if src else 0,
                    keep_highest=getattr(src, "keep_highest", 0) if src else 0,
                    aggregate_only_graded=getattr(src, "aggregate_only_graded", True) if src else True,
                    aggregate_outcomes=getattr(src, "aggregate_outcomes", False) if src else False,
                    extra_credit=getattr(src, "extra_credit", False) if src else False,
                    grade_min=getattr(src, "grade_min", None) if src else None,
                    grade_max=getattr(src, "grade_max", 100.0) if src else 100.0,
                    grade_pass=getattr(src, "grade_pass", None) if src else None,
                    hidden=getattr(src, "hidden", False) if src else False,
                    hidden_until=getattr(src, "hidden_until", None) if src else None,
                    locked=getattr(src, "locked", False) if src else False,
                    lock_time=getattr(src, "lock_time", None) if src else None,
                    display_type=getattr(src, "display_type", GRADE_DISPLAY_TYPE_DEFAULT) if src else GRADE_DISPLAY_TYPE_DEFAULT,
                    decimals=getattr(src, "decimals", -1) if src else -1,
                    calculation_formula=getattr(src, "calculation_formula", None) if src else None,
                    formula_override=bool(getattr(src, "formula_override", False)) if src else False,
                    formula_item_refs=list(getattr(src, "formula_item_refs", []) or []) if src else [],
                    formula_unresolved_refs=list(getattr(src, "formula_unresolved_refs", []) or []) if src else [],
                )
            )
        return rebuilt

    def _apply_weight_updates(self, proposal: GradebookProposal, weights: Dict[str, float]) -> None:
        normalized_map = {cat.name.lower(): cat for cat in proposal.categories}
        for name, weight in weights.items():
            key = name.lower()
            if key in normalized_map:
                existing = normalized_map[key]
                if abs(float(existing.weight) - float(weight)) > 0.001:
                    existing.weight = weight
                    self._append_effect_note(proposal, f"Set {existing.name} to {weight:.1f}%")
            else:
                # Allow adding a new category when the user explicitly sets its weight
                # (e.g., "add quizzes 5%" / "quizzes 5%").
                proposal.categories.append(GradebookCategory(name=name, weight=weight, items=[]))
                self._append_effect_note(proposal, f"Added '{name}' with weight {weight:.1f}%")

    def _parse_add_category_requests(self, proposal: GradebookProposal, prompt: str) -> List[str]:
        requested: List[str] = []
        seen: set = set()

        for match in re.finditer(
            r"\b(?:add|added)\s+(?:new\s+)?(?:category\s+)?(?:as\s+)?([a-z][a-z\s]{1,40}?)(?=(?:\s+with\b|\s+at\b|\s+to\b|\s*[:.,;!?]|$))",
            prompt,
            flags=re.IGNORECASE,
        ):
            raw = re.sub(r"\s+", " ", (match.group(1) or "").strip())
            if not raw:
                continue
            if re.search(r"\b(?:it|them|its\s+weight|subcategor(?:y|ies))\b", raw, flags=re.IGNORECASE):
                continue
            if re.search(r"\bgrade\s+items?\b", raw, flags=re.IGNORECASE):
                continue

            # Scoped phrases like "add test to Final exam" should be handled as
            # item/subcategory intent, not as top-level category creation.
            trailing = prompt[match.end(): match.end() + 120]
            scoped_target = re.match(
                r"\s+(?:to|into|under|in)\s+([a-zA-Z][a-zA-Z ]{1,40})(?:\s+category)?\b",
                trailing,
                flags=re.IGNORECASE,
            )
            if scoped_target and self._resolve_existing_category_name(proposal, scoped_target.group(1).strip()):
                continue

            cleaned = self._clean_new_category_name(raw)
            if not cleaned:
                continue
            if self._find_existing_name(proposal, cleaned):
                continue
            resolved = cleaned

            key = resolved.lower()
            if key in seen:
                continue
            seen.add(key)
            requested.append(resolved)

        return requested

    def _extract_recent_removed_transfer(self, proposal: GradebookProposal, category_name: str) -> Optional[Tuple[float, str]]:
        category_l = (category_name or "").strip().lower()
        if not category_l:
            return None

        for note in reversed(proposal.notes or []):
            text = str(note or "")
            if text.startswith("Effect:"):
                text = text[len("Effect:"):].strip()
            match = re.match(
                r"Removed\s+'([^']+)'\s+and\s+assigned\s+([0-9]+(?:\.[0-9]+)?)%\s+to\s+(.+)$",
                text,
                flags=re.IGNORECASE,
            )
            if not match:
                continue

            removed_name = (match.group(1) or "").strip().lower()
            if removed_name != category_l:
                continue

            try:
                weight = float(match.group(2))
            except Exception:
                continue

            target_name = self._resolve_category_name(proposal, (match.group(3) or "").strip())
            if not target_name:
                continue

            target = next((c for c in proposal.categories if c.name.lower() == target_name.lower()), None)
            if target is None or float(getattr(target, "weight", 0.0)) < weight:
                continue

            return weight, target.name

        return None

    def _apply_add_category_requests(
        self,
        proposal: GradebookProposal,
        prompt: str,
        explicit_updates: Dict[str, float],
    ) -> None:
        requested = self._parse_add_category_requests(proposal, prompt)
        if not requested:
            return

        explicit_keys = {k.lower() for k in (explicit_updates or {}).keys()}
        existing_map = {cat.name.lower(): cat for cat in proposal.categories}

        for name in requested:
            key = name.lower()
            if key in explicit_keys or key in existing_map:
                continue

            inferred_weight = 0.0
            proposal.categories.append(GradebookCategory(name=name, weight=inferred_weight, items=[]))
            self._append_effect_note(proposal, f"Added '{name}' with weight {inferred_weight:.1f}%")

    _SPLIT_PARENT_LABEL_STOPWORDS = frozenset(
        {"it", "this", "that", "them", "each", "the", "a", "an", "one", "ones"}
    )

    def _extract_split_parent_label(self, prompt: str) -> Optional[str]:
        """Extract the raw parent category label from a split/subcategory prompt."""
        # Prefer scoped "In Labs, divide/split ..." before "divide it into ..." pronoun forms.
        patterns = (
            re.compile(
                r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,.*\b(?:split|divide)\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,.*\badd\s+subcategories\b",
                flags=re.IGNORECASE,
            ),
            re.compile(r"\b(?:split|divide)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(|\s+into\b)", flags=re.IGNORECASE),
        )
        for pattern in patterns:
            match = pattern.search(prompt)
            if not match:
                continue
            label = re.sub(r"\s+", " ", (match.group(1) or "").strip())
            if not label:
                continue
            if self._normalize_name(label) in self._SPLIT_PARENT_LABEL_STOPWORDS:
                continue
            return label
        return None

    def _resolve_existing_category_name(self, proposal: GradebookProposal, raw_name: str) -> Optional[str]:
        """Resolve a category label only when that category exists in the proposal."""
        resolved = self._resolve_category_name(proposal, raw_name)
        if not resolved:
            return None
        return self._find_existing_name(proposal, resolved)

    def _append_category_not_found_effect(self, proposal: GradebookProposal, raw_name: str, action: str) -> None:
        clean_name = re.sub(r"\s+", " ", (raw_name or "").strip()) or "category"
        clean_action = re.sub(r"\s+", " ", (action or "modify").strip()) or "modify"
        self._append_effect_note(
            proposal,
            f"Tried to {clean_action} '{clean_name}', but category was not found. No changes made.",
        )

    def _has_category_not_found_effect(self, proposal: GradebookProposal, raw_name: str) -> bool:
        target = self._normalize_name(raw_name)
        if not target:
            return False
        for note in (proposal.notes or []):
            if not str(note).startswith("Effect:"):
                continue
            low = str(note).lower()
            if "not found" in low and target in self._normalize_name(low):
                return True
        return False

    _MISSING_CATEGORY_LABEL_STOP_PHRASES = (
        "the number of",
        "in particular",
        "teaching and learning",
        "references needed",
        "number of references",
    )

    @staticmethod
    def _prompt_has_scoped_category_modification_intent(prompt: str) -> bool:
        """True when the user is trying to change a specific category, not narrating syllabus text."""
        prompt_l = (prompt or "").lower().strip()
        if not prompt_l:
            return False

        if re.search(
            r"\b(?:give me (?:a )?proposal|show proposal|generate proposal|build proposal|create proposal|make proposal)\b",
            prompt_l,
        ):
            return False

        if re.search(
            r"\b(?:in|for)\s+[a-z][a-z0-9 ]{0,40}?\s*,\s*(?:keep|set|split|divide|drop|hide|show|lock|add|assign|change|adjust|update)\b",
            prompt_l,
        ):
            return True
        if re.search(r"\b(?:in|for)\s+[a-z][a-z0-9 ]{0,40}?\s+(?:keep|set|split|divide|drop|hide|show|lock|add)\b", prompt_l):
            return True
        if re.search(r"\b(?:split|divide)\b.+\binto\b", prompt_l):
            return True
        if re.search(
            r"\b(?:set|assign|change|adjust|update|make)\s+(?:the\s+)?(?:assignments?|labs?|midterm|final|quizzes?|projects?|participation)\b",
            prompt_l,
        ):
            return True
        return False

    def _is_plausible_missing_category_label(self, label: str) -> bool:
        clean = re.sub(r"\s+", " ", (label or "").strip().lower())
        if not clean or len(clean) > 48:
            return False
        if len(clean.split()) > 4:
            return False
        if self._normalize_name(clean) in self._SPLIT_PARENT_LABEL_STOPWORDS:
            return False
        if any(phrase in clean for phrase in self._MISSING_CATEGORY_LABEL_STOP_PHRASES):
            return False
        if re.search(r"\b(?:the|and|of|for|in|to|a|an)\b", clean) and not re.search(
            r"\b(?:assignment|assignments|lab|labs|quiz|quizzes|midterm|final|exam|project|homework|participation)\b",
            clean,
        ):
            return False
        return True

    def _extract_scoped_category_labels(self, prompt: str) -> List[str]:
        labels: List[str] = []
        seen: set[str] = set()
        patterns = (
            re.compile(
                r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,\s*(?:keep|set|split|divide|drop|hide|show|lock|add|assign|change|adjust|update)\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)\s+(?:keep|set|split|divide|drop|hide|show|lock|add)\b",
                flags=re.IGNORECASE,
            ),
        )
        for pattern in patterns:
            for match in pattern.finditer(prompt):
                label = re.sub(r"\s+", " ", (match.group(1) or "").strip())
                key = label.lower()
                if not label or key in seen:
                    continue
                if not self._is_plausible_missing_category_label(label):
                    continue
                seen.add(key)
                labels.append(label)
        return labels

    def _infer_scoped_category_action(self, prompt: str) -> str:
        prompt_l = (prompt or "").lower()
        if re.search(r"\b(?:split|divide)\b.+\binto\b", prompt_l) or re.search(r"\badd\s+subcategories\b", prompt_l):
            return "split"
        if re.search(r"\b(?:formula|calculation)\b", prompt_l):
            return "set a formula for"
        if re.search(r"\b(?:hide|hidden|show|unhide|lock|unlock)\b", prompt_l):
            return "change visibility for"
        if re.search(r"\b(?:drop|keep)\s+(?:lowest|highest|top|best)\b", prompt_l):
            return "change drop/keep rules for"
        return "modify"

    def _guard_scoped_category_intents(self, proposal: GradebookProposal, prompt: str) -> None:
        """Warn when a scoped In/For <category> request targets a missing proposal category."""
        if not self._prompt_has_scoped_category_modification_intent(prompt):
            return

        prompt_l = (prompt or "").lower()
        if re.search(r"\b(?:remove|delete|drop)\s+(?:the\s+)?[a-z]", prompt_l):
            return

        labels = self._extract_scoped_category_labels(prompt)
        if not labels:
            split_label = self._extract_split_parent_label(prompt)
            if split_label and re.search(r"\b(?:split|divide)\b.+\binto\b", prompt_l):
                labels = [split_label]

        if not labels:
            return

        action = self._infer_scoped_category_action(prompt)
        for raw_label in labels:
            if self._category_exists_in_proposal(proposal, raw_label):
                continue
            if self._has_category_not_found_effect(proposal, raw_label):
                continue
            self._append_category_not_found_effect(proposal, raw_label, action)

    def _category_exists_in_proposal(self, proposal: GradebookProposal, raw_name: str) -> bool:
        return self._resolve_existing_category_name(proposal, raw_name) is not None

    def _detect_split_parent(self, proposal: GradebookProposal, prompt: str) -> Optional[str]:
        label = self._extract_split_parent_label(prompt)
        if not label:
            return None
        return self._resolve_existing_category_name(proposal, label)

    def _parse_split_categories(self, prompt: str) -> List[Tuple[str, Optional[float]]]:
        # Try "split/divide X into Y 10%, Z 15%" pattern
        match = re.search(r"\b(?:split|divide)\b.+?\binto\b\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            # Try "In X, add subcategories: Y 10%, Z 15%" pattern
            match = re.search(r"\badd\s+subcategories\s*[:;]?\s*(.+)", prompt, flags=re.IGNORECASE)
        if not match:
            return []

        raw_list = re.sub(r"\bbut\b.+", "", match.group(1), flags=re.IGNORECASE).strip()
        each_weight = None
        each_match = re.search(r"\b(?:assign|set|make)?\s*(\d+(?:\.\d+)?)\s*%\s*(?:for\s*)?each\b", raw_list, flags=re.IGNORECASE)
        if each_match:
            each_weight = float(each_match.group(1))
            raw_list = re.sub(r"\b(?:and\s+)?(?:assign|set|make)?\s*\d+(?:\.\d+)?\s*%\s*(?:for\s*)?each\b", "", raw_list, flags=re.IGNORECASE)

        pieces = re.split(r",| and | & ", raw_list)
        parsed: List[Tuple[str, Optional[float]]] = []
        for piece in pieces:
            cleaned = piece.strip()
            if not cleaned:
                continue
            weighted_with_phrase = re.match(
                r"([a-zA-Z][a-zA-Z\- ]*?)\s+with\s+weight\s+of\s+(\d+(?:\.\d+)?)\s*%?\b",
                cleaned,
                flags=re.IGNORECASE,
            )
            if weighted_with_phrase:
                name = weighted_with_phrase.group(1).strip()
                name = re.sub(r"^[^a-zA-Z]+", "", name)
                name = re.sub(r"[^a-zA-Z]+$", "", name)
                if name:
                    parsed.append((name.title(), float(weighted_with_phrase.group(2))))
                continue

            weighted = re.match(r"([a-zA-Z][a-zA-Z\- ]*?)\s*(\d+(?:\.\d+)?)\s*%", cleaned, flags=re.IGNORECASE)
            if weighted:
                name = weighted.group(1).strip()
                name = re.sub(r"^[^a-zA-Z]+", "", name)
                name = re.sub(r"[^a-zA-Z]+$", "", name)
                if name:
                    parsed.append((name.title(), float(weighted.group(2))))
            elif re.search(r"[a-zA-Z]", cleaned):
                name = re.sub(
                    r"\bwith\s+weight\s+of\s+\d+(?:\.\d+)?\s*%?\b",
                    "",
                    cleaned,
                    flags=re.IGNORECASE,
                ).strip()
                name = re.sub(r"\d+(?:\.\d+)?\s*%", "", name).strip()
                name = re.sub(r"^[^a-zA-Z]+", "", name)
                name = re.sub(r"[^a-zA-Z]+$", "", name)
                if name:
                    parsed.append((name.title(), None))

        if each_weight is not None and parsed and all(weight is None for _, weight in parsed):
            parsed = [(name, each_weight) for name, _ in parsed]

        return parsed

    def _apply_split_request(self, proposal: GradebookProposal, prompt: str) -> None:
        split_parts = self._parse_split_categories(prompt)
        if not split_parts:
            return

        parent_name = self._detect_split_parent(proposal, prompt)
        if not parent_name:
            raw_parent = self._extract_split_parent_label(prompt)
            if (
                raw_parent
                and self._is_plausible_missing_category_label(raw_parent)
                and not self._has_category_not_found_effect(proposal, raw_parent)
            ):
                self._append_category_not_found_effect(proposal, raw_parent, "split")
            return

        parent = next((cat for cat in proposal.categories if cat.name.lower() == parent_name.lower()), None)
        if parent is None:
            if not self._has_category_not_found_effect(proposal, parent_name):
                self._append_category_not_found_effect(proposal, parent_name, "split")
            return

        # Always overwrite previous subcategories and remove prior split notes for this parent
        parent.subcategories = []
        proposal.notes = [n for n in (proposal.notes or []) if not (str(n).startswith(f"Effect: In {parent.name}, split into") or str(n).startswith(f"{parent.name} split requested"))]

        # Pre-compute parsed weights; distribute remainder evenly among unspecified items.
        parsed_pairs: List[Tuple[str, Optional[float]]] = []
        for name, weight in split_parts:
            if weight is not None:
                pw = self._parse_subcategory_weight_value(str(weight), float(parent.weight))
                parsed_pairs.append((name, float(pw) if pw is not None else None))
            else:
                parsed_pairs.append((name, None))

        explicit_total = sum(w for _, w in parsed_pairs if w is not None)
        none_count = sum(1 for _, w in parsed_pairs if w is None)
        if none_count > 0:
            default_weight = round(max(0.0, float(parent.weight) - explicit_total) / none_count, 2)
        else:
            default_weight = 0.0

        subcategories: List[GradebookSubcategory] = []
        labels: List[str] = []
        for name, w in parsed_pairs:
            actual_w = w if w is not None else default_weight
            subcategories.append(GradebookSubcategory(name=name, weight=actual_w))
            labels.append(f"{name} {actual_w:.1f}%")

        parent.subcategories = subcategories
        self._append_effect_note(
            proposal,
            f"In {parent.name}, split into " + ", ".join(labels),
        )
        self._append_unique_note(
            proposal,
            f"{parent.name} split requested (treated as internal allocation): " + ", ".join(labels) + ".",
        )

    # Redistribution intent keywords for removal prompts.
    _REDISTRIBUTE_EVENLY_PATTERNS = re.compile(
        r"\b(?:evenly|equally|proportionally|split\s+evenly|distribute\s+evenly|"
        r"redistribute|divide\s+(?:it\s+)?equally|share\s+equally|spread\s+equally)\b",
        re.IGNORECASE,
    )
    _REDISTRIBUTE_TO_PATTERNS = re.compile(
        r"\band\s+(?:give\s+(?:its\s+)?weight\s+to|give\s+to|assign\s+(?:it\s+)?to|"
        r"move\s+(?:weight\s+)?to|reallocate\s+to|redistribute\s+(?:to|among)|"
        r"distribute\s+to|split\s+(?:weight\s+)?among|split\s+(?:it\s+)?between|"
        r"add\s+(?:it\s+)?to)\s+([a-z\s,&]+)",
        re.IGNORECASE,
    )

    @staticmethod
    def _collapse_subcategory_label(label: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", ProposalGenerator._normalize_name(label))

    def _subcategory_labels_match(self, candidate: str, query: str) -> bool:
        candidate_norm = self._normalize_name(candidate)
        query_norm = self._normalize_name(query)
        if not candidate_norm or not query_norm:
            return False
        if candidate_norm == query_norm:
            return True

        candidate_collapsed = self._collapse_subcategory_label(candidate)
        query_collapsed = self._collapse_subcategory_label(query)
        if candidate_collapsed and candidate_collapsed == query_collapsed:
            return True

        def _variants(name: str) -> set[str]:
            variants = {name}
            if name.endswith("ies") and len(name) > 3:
                variants.add(name[:-3] + "y")
            if name.endswith("s") and len(name) > 1:
                variants.add(name[:-1])
            else:
                variants.add(name + "s")
            return {v for v in variants if v}

        if _variants(candidate_collapsed) & _variants(query_collapsed):
            return True

        close = difflib.get_close_matches(
            query_collapsed,
            [candidate_collapsed],
            n=1,
            cutoff=0.84,
        )
        return bool(close)

    def _extract_scoped_parent_label(self, prompt: str) -> Optional[str]:
        patterns = (
            re.compile(
                r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\b(?:remove|drop|delete)\s+.+?\s+from\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\b",
                flags=re.IGNORECASE,
            ),
            re.compile(
                r"\badd\s+(?:subcategory\s+)?[a-zA-Z][a-zA-Z\- ]{1,40}?\s+(?:to|into|under|in)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?\b",
                flags=re.IGNORECASE,
            ),
        )
        for pattern in patterns:
            match = pattern.search(prompt)
            if not match:
                continue
            label = re.sub(r"\s+", " ", (match.group(1) or "").strip())
            if label and self._normalize_name(label) not in self._SPLIT_PARENT_LABEL_STOPWORDS:
                return label
        return None

    def _find_subcategory_match(
        self,
        proposal: GradebookProposal,
        raw_name: str,
        parent_hint: Optional[str] = None,
    ) -> Optional[Tuple[GradebookCategory, GradebookSubcategory]]:
        clean_name = re.sub(r"\s+", " ", (raw_name or "").strip())
        if not clean_name:
            return None

        parents: List[GradebookCategory] = []
        if parent_hint:
            parent_name = self._resolve_existing_category_name(proposal, parent_hint)
            if not parent_name:
                return None
            parents = [
                cat for cat in proposal.categories
                if cat.name.lower() == parent_name.lower()
            ]
        else:
            parents = list(proposal.categories)

        matches: List[Tuple[GradebookCategory, GradebookSubcategory]] = []
        for parent in parents:
            for sub in (parent.subcategories or []):
                if self._subcategory_labels_match(sub.name, clean_name):
                    matches.append((parent, sub))

        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        if parent_hint:
            return None
        return matches[0]

    def _append_subcategory_not_found_effect(
        self,
        proposal: GradebookProposal,
        raw_name: str,
        action: str,
        parent_hint: Optional[str] = None,
    ) -> None:
        clean_name = re.sub(r"\s+", " ", (raw_name or "").strip()) or "subcategory"
        clean_action = re.sub(r"\s+", " ", (action or "modify").strip()) or "modify"
        parent_suffix = ""
        if parent_hint:
            parent_name = self._resolve_existing_category_name(proposal, parent_hint)
            if parent_name:
                parent_suffix = f" under {parent_name}"
        self._append_effect_note(
            proposal,
            f"Tried to {clean_action} subcategory '{clean_name}'{parent_suffix}, but it was not found. No changes made.",
        )

    def _remove_subcategory_from_parent(
        self,
        proposal: GradebookProposal,
        parent: GradebookCategory,
        sub_to_remove: GradebookSubcategory,
        prompt: str,
    ) -> None:
        subs = list(parent.subcategories or [])
        if sub_to_remove not in subs:
            return

        removed_weight = float(sub_to_remove.weight or 0.0)
        if sub_to_remove.items:
            if parent.items is None:
                parent.items = []
            for item_name in list(sub_to_remove.items):
                if item_name not in parent.items:
                    parent.items.append(item_name)
                item_weight = (sub_to_remove.item_weights or {}).get(item_name)
                if item_weight is not None:
                    if parent.item_weights is None:
                        parent.item_weights = {}
                    parent.item_weights[item_name] = item_weight
        remaining = [sub for sub in subs if sub is not sub_to_remove]
        parent.subcategories = remaining

        tail = prompt[prompt.lower().find(sub_to_remove.name.lower()):] if sub_to_remove.name else prompt
        to_match = self._REDISTRIBUTE_TO_PATTERNS.search(tail)
        if to_match and remaining:
            raw_targets = re.sub(
                r"\b(?:the|a|an|its|lowest|highest|subcategory|subcategories|weight|one|single)\b",
                " ",
                to_match.group(1) or "",
                flags=re.IGNORECASE,
            )
            raw_targets = re.sub(r"\s+", " ", raw_targets).strip(" ,.")
            target_name = raw_targets.split(",")[0].strip()
            target_match = self._find_subcategory_match(proposal, target_name, parent_hint=parent.name)
            if target_match and target_match[0] is parent:
                target_match[1].weight = round(float(target_match[1].weight or 0.0) + removed_weight, 2)
                self._append_effect_note(
                    proposal,
                    f"Removed subcategory '{sub_to_remove.name}' from {parent.name} and assigned "
                    f"{removed_weight:.1f}% to {target_match[1].name}",
                )
                return

        if remaining and removed_weight > 0 and (
            self._REDISTRIBUTE_EVENLY_PATTERNS.search(tail)
            or re.search(r"\bevenly\b|\bequally\b", prompt, flags=re.IGNORECASE)
        ):
            share = removed_weight / len(remaining)
            for sub in remaining:
                sub.weight = round(float(sub.weight or 0.0) + share, 2)
            self._append_effect_note(
                proposal,
                f"Removed subcategory '{sub_to_remove.name}' from {parent.name} and distributed "
                f"{removed_weight:.1f}% evenly",
            )
            return

        if remaining and removed_weight > 0:
            share = removed_weight / len(remaining)
            for sub in remaining:
                sub.weight = round(float(sub.weight or 0.0) + share, 2)

        if not remaining:
            parent.subcategories = []
            self._append_effect_note(
                proposal,
                f"Removed subcategory '{sub_to_remove.name}' from {parent.name}",
            )
            return

        self._append_effect_note(
            proposal,
            f"Removed subcategory '{sub_to_remove.name}' from {parent.name} and redistributed "
            f"{removed_weight:.1f}% across remaining subcategories",
        )

    def _apply_subcategory_removals(self, proposal: GradebookProposal, prompt: str) -> set[str]:
        def _clean_phrase(value: str) -> str:
            cleaned = re.sub(
                r"\b(?:the|a|an|its|lowest|highest|subcategory|subcategories|weight|one|single)\b",
                " ",
                value,
                flags=re.IGNORECASE,
            )
            return re.sub(r"\s+", " ", cleaned).strip(" ,.")

        patterns = (
            re.compile(
                r"\b(?:remove|delete|drop)\s+(?:the\s+)?(?:subcategory\s+)?([a-z][a-z\s\-]{0,40}?)"
                r"(?:\s+subcategory)?(?:\s+from\s+([a-zA-Z][a-zA-Z ]{1,40}?))?(?:\s+category)?"
                r"(?:\s+and\s|\s+evenly|\s+equally|\s+proportionally|[,:;.!?]|$)",
                re.IGNORECASE,
            ),
        )
        parent_hint = self._extract_scoped_parent_label(prompt)
        handled: set[str] = set()

        for pattern in patterns:
            for match in pattern.finditer(prompt):
                raw_name = _clean_phrase(match.group(1) or "")
                if not raw_name:
                    continue
                if re.search(r"\b(?:grade\s+item|item)\b", raw_name, flags=re.IGNORECASE):
                    continue

                explicit_parent = (match.group(2) or "").strip() or parent_hint
                handle_key = f"{self._normalize_name(raw_name)}::{self._normalize_name(explicit_parent or '')}"
                if handle_key in handled:
                    continue

                match_result = self._find_subcategory_match(proposal, raw_name, explicit_parent)
                if match_result:
                    handled.add(handle_key)
                    parent, sub = match_result
                    self._remove_subcategory_from_parent(proposal, parent, sub, prompt)
                    continue

                # Defer to top-level category removal (including fuzzy quiz -> Quizzes).
                if self._resolve_category_name(proposal, raw_name):
                    continue

                has_subcategories = any((cat.subcategories or []) for cat in proposal.categories)
                if has_subcategories or explicit_parent:
                    self._append_subcategory_not_found_effect(
                        proposal,
                        raw_name,
                        "remove",
                        parent_hint=explicit_parent,
                    )
                    handled.add(handle_key)

        return handled

    def _apply_subcategory_renames(self, proposal: GradebookProposal, prompt: str) -> set[str]:
        parent_hint = self._extract_scoped_parent_label(prompt)
        handled: set[str] = set()
        renamed_sources: set[str] = set()

        scoped_pattern = re.compile(
            r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,\s*"
            r"rename\s+(?:subcategory\s+)?(.+?)\s+(?:subcategory\s+)?to\s+(.+?)(?:[.,]|$)",
            flags=re.IGNORECASE,
        )
        simple_pattern = re.compile(
            r"\brename\s+(?:subcategory\s+)?(.+?)\s+(?:subcategory\s+)?to\s+(.+?)(?:[.,]|$)",
            flags=re.IGNORECASE,
        )

        def _apply_rename(src_name: str, dest_name: str, parent_hint_for_match: Optional[str]) -> None:
            handle_key = (
                f"{self._normalize_name(src_name)}::{self._normalize_name(dest_name)}::"
                f"{self._normalize_name(parent_hint_for_match or '')}"
            )
            if handle_key in handled:
                return
            handled.add(handle_key)

            if not src_name or not dest_name:
                return
            if re.search(r"\b(?:grade\s+item|item)\b", src_name, flags=re.IGNORECASE):
                return

            match_result = self._find_subcategory_match(proposal, src_name, parent_hint_for_match)
            if not match_result:
                resolved_top = self._resolve_existing_category_name(proposal, src_name)
                if (
                    resolved_top
                    and self._normalize_name(resolved_top) == self._normalize_name(src_name)
                ):
                    return
                self._append_subcategory_not_found_effect(
                    proposal,
                    src_name,
                    "rename",
                    parent_hint=parent_hint_for_match,
                )
                return

            parent, sub = match_result
            dest_clean = re.sub(r"\s+", " ", dest_name).strip()
            if not dest_clean:
                return
            if any(
                self._subcategory_labels_match(existing.name, dest_clean)
                for existing in (parent.subcategories or [])
                if existing is not sub
            ):
                return

            old_name = sub.name
            sub.name = dest_clean.title() if dest_clean.islower() else dest_clean
            renamed_sources.add(self._normalize_name(old_name))
            renamed_sources.add(self._normalize_name(src_name))
            self._append_effect_note(
                proposal,
                f"Renamed subcategory '{old_name}' to '{sub.name}' in {parent.name}",
            )

        for match in scoped_pattern.finditer(prompt):
            scoped_parent = (match.group(1) or "").strip()
            src_name = (match.group(2) or "").strip().rstrip("., ")
            dest_name = (match.group(3) or "").strip().rstrip("., ")
            _apply_rename(src_name, dest_name, scoped_parent or parent_hint)

        for match in simple_pattern.finditer(prompt):
            src_name = (match.group(1) or "").strip().rstrip("., ")
            dest_name = (match.group(2) or "").strip().rstrip("., ")
            _apply_rename(src_name, dest_name, parent_hint)

        return renamed_sources

    def _apply_subcategory_additions(self, proposal: GradebookProposal, prompt: str) -> None:
        parent_hint = self._extract_scoped_parent_label(prompt)
        handled: set[str] = set()

        scoped_pattern = re.compile(
            r"\b(?:in|for)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s*\(\s*\d+(?:\.\d+)?\s*%\s*\))?\s*,\s*"
            r"add\s+(?:subcategory\s+)?([a-zA-Z][a-zA-Z\- ]{1,40}?)(?:\s+subcategory)?"
            r"(?:\s+(?:with\s+)?(?:weight\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*%?)?(?:[.,]|$)",
            flags=re.IGNORECASE,
        )
        trailing_parent_pattern = re.compile(
            r"\badd\s+(?:subcategory\s+)?([a-zA-Z][a-zA-Z\- ]{1,40}?)(?:\s+subcategory)?"
            r"(?:\s+(?:with\s+)?(?:weight\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*%?)?"
            r"\s+(?:to|into|under|in)\s+([a-zA-Z][a-zA-Z ]{1,40}?)(?:\s+category)?(?:[.,]|$)",
            flags=re.IGNORECASE,
        )

        def _apply_add(parent_label: str, sub_name: str, raw_weight: Optional[str]) -> None:
            parent_label = parent_label or parent_hint or ""
            sub_name = re.sub(r"\s+", " ", sub_name).strip(" ,.")
            if not sub_name or not parent_label:
                return

            # "add grade item X to Y" belongs to manual-item flow, not subcategory CRUD.
            if re.search(r"\b(?:grade\s+item|manual\s+item)\b", sub_name, re.IGNORECASE):
                return

            # Prevent ambiguous "add X to Y" from creating subcategories unless
            # the user explicitly says subcategory/subcategories or provides a weight.
            has_explicit_subcategory_intent = bool(
                re.search(r"\bsubcategor(?:y|ies)\b", prompt, re.IGNORECASE)
            )
            if (raw_weight is None or str(raw_weight).strip() == "") and not has_explicit_subcategory_intent:
                return

            handle_key = f"{self._normalize_name(sub_name)}::{self._normalize_name(parent_label)}"
            if handle_key in handled:
                return
            handled.add(handle_key)

            parent_name = self._resolve_existing_category_name(proposal, parent_label)
            if not parent_name:
                return

            parent = next((cat for cat in proposal.categories if cat.name.lower() == parent_name.lower()), None)
            if parent is None:
                return

            if any(self._subcategory_labels_match(existing.name, sub_name) for existing in (parent.subcategories or [])):
                return

            weight_value: Optional[float] = None
            if raw_weight is not None and str(raw_weight).strip() != "":
                parsed = self._parse_subcategory_weight_value(str(raw_weight), float(parent.weight))
                if parsed is not None:
                    weight_value = float(parsed)

            if weight_value is None:
                weight_value = 0.0

            parent.subcategories = list(parent.subcategories or [])
            display_name = sub_name.title() if sub_name.islower() else sub_name
            parent.subcategories.append(GradebookSubcategory(name=display_name, weight=weight_value))
            self._append_effect_note(
                proposal,
                f"Added subcategory '{display_name}' ({weight_value:.1f}%) to {parent.name}",
            )

        for match in scoped_pattern.finditer(prompt):
            _apply_add(
                (match.group(1) or "").strip(),
                (match.group(2) or "").strip(),
                match.group(3),
            )

        for match in trailing_parent_pattern.finditer(prompt):
            _apply_add(
                (match.group(3) or "").strip(),
                (match.group(1) or "").strip(),
                match.group(2),
            )

    def _apply_removals(self, proposal: GradebookProposal, prompt: str) -> None:
        """Handle category removal with three distinct weight modes.

        Supports:
        1. ``remove X``                     – free the weight (total decreases)
        2. ``remove X evenly``              – redistribute freed weight to remaining categories
        3. ``remove X and give/assign to Y``– transfer freed weight to specific category Y
        """
        subcategory_handled = self._apply_subcategory_removals(proposal, prompt)

        def _clean_phrase(value: str) -> str:
            cleaned = re.sub(
                r"\b(?:the|a|an|its|lowest|highest|category|categories|weight|one|single)\b",
                " ",
                value,
                flags=re.IGNORECASE,
            )
            return re.sub(r"\s+", " ", cleaned).strip(" ,.")

        # Match any remove/delete/drop, capture everything after.
        pattern = re.compile(
            r"\b(?:remove|delete|drop)\s+(?:the\s+)?([a-z][a-z\s]{0,40}?)(?:\s+and\s|\s+evenly|\s+equally|\s+proportionally|[,:;.!?]|$)",
            re.IGNORECASE,
        )

        handled: set = set()


        for match in pattern.finditer(prompt):
            raw_category = _clean_phrase(match.group(1) or "")

            # Guard: item-targeted commands are handled by item logic and should
            # not trigger category deletion fallbacks.
            if re.search(r"\bfrom\b", raw_category, flags=re.IGNORECASE):
                continue
            if re.search(r"\b(?:grade\s+item|item)\b", raw_category, flags=re.IGNORECASE):
                continue

            raw_key = self._normalize_name(raw_category)
            if any(key.startswith(f"{raw_key}::") for key in subcategory_handled):
                continue

            resolved = self._resolve_category_name(proposal, raw_category)
            if not resolved or resolved in handled:
                if not resolved:
                    self._append_category_not_found_effect(proposal, raw_category, "remove")
                continue

            removable = next(
                (cat for cat in proposal.categories if cat.name.lower() == resolved.lower()),
                None,
            )
            if removable is None:
                self._append_category_not_found_effect(proposal, raw_category, "remove")
                continue

            handled.add(resolved)
            removed_weight = removable.weight
            proposal.categories = [cat for cat in proposal.categories if cat is not removable]

            if not proposal.categories:
                self._append_effect_note(proposal, f"Removed '{resolved}' ({removed_weight:.1f}% freed)")
                continue

            # Determine redistribution intent from the full prompt.
            tail = prompt[match.start():]  # look at the tail to catch "and give to Y"
            to_match = self._REDISTRIBUTE_TO_PATTERNS.search(tail)

            if to_match:
                # Mode 3: assign freed weight to explicit target(s).
                raw_targets = _clean_phrase(to_match.group(1) or "")
                target_str = re.sub(r"\b(?:and|or)\b", ",", raw_targets, flags=re.IGNORECASE)
                candidate_names = [t.strip().rstrip(",. ") for t in target_str.split(",") if t.strip()]
                resolved_targets = [
                    self._resolve_category_name(proposal, t)
                    for t in candidate_names
                ]
                resolved_targets = [t for t in resolved_targets if t]
                target_cats = [cat for cat in proposal.categories if cat.name in resolved_targets]

                if target_cats:
                    share = removed_weight / len(target_cats)
                    for cat in target_cats:
                        cat.weight += share
                    target_label = ", ".join(cat.name for cat in target_cats)
                    self._append_effect_note(
                        proposal,
                        f"Removed '{resolved}' and assigned {removed_weight:.1f}% to {target_label}",
                    )
                else:
                    # Target not found — fall back to free (don't silently redistribute).
                    self._append_effect_note(
                        proposal,
                        f"Removed '{resolved}' ({removed_weight:.1f}% freed; target not found)",
                    )

            elif self._REDISTRIBUTE_EVENLY_PATTERNS.search(tail):
                # Mode 2: distribute freed weight evenly across all remaining categories.
                share = removed_weight / len(proposal.categories)
                for cat in proposal.categories:
                    cat.weight += share
                self._append_effect_note(
                    proposal,
                    f"Removed '{resolved}' and distributed {removed_weight:.1f}% evenly",
                )

            else:
                # Mode 1 (default): free the weight — total decreases.
                self._append_effect_note(
                    proposal,
                    f"Removed '{resolved}' ({removed_weight:.1f}% freed)",
                )

    def _resolve_category_name(self, proposal: GradebookProposal, raw_name: str) -> Optional[str]:
        cleaned = self._normalize_name(raw_name)
        if not cleaned:
            return None

        regex_alias = self._resolve_alias_by_regex(raw_name)
        if regex_alias:
            existing = self._find_existing_name(proposal, regex_alias)
            return existing or regex_alias

        if cleaned in self._category_aliases:
            alias = self._category_aliases[cleaned]
            existing = self._find_existing_name(proposal, alias)
            return existing or alias

        existing_names = [cat.name for cat in proposal.categories]
        existing_norm_map = {self._normalize_name(name): name for name in existing_names}

        if cleaned in existing_norm_map:
            return existing_norm_map[cleaned]

        close = difflib.get_close_matches(
            cleaned,
            list(existing_norm_map.keys()) + list(self._category_aliases.keys()),
            n=1,
            cutoff=0.72,
        )
        if close:
            best = close[0]
            if best in existing_norm_map:
                return existing_norm_map[best]
            alias = self._category_aliases.get(best)
            if alias:
                existing = self._find_existing_name(proposal, alias)
                return existing or alias

        # Reject long free-text fragments to avoid creating accidental categories.
        if len(cleaned.split()) > 3:
            return None
        return None

    def _resolve_alias_by_regex(self, raw_name: str) -> Optional[str]:
        for pattern, canonical in self._category_alias_patterns:
            if pattern.search(raw_name):
                return canonical
        return None

    def _load_aliases_from_env(self) -> None:
        aliases_raw = os.environ.get("GRADEBOOK_CATEGORY_ALIASES_JSON", "").strip()
        regex_raw = os.environ.get("GRADEBOOK_CATEGORY_ALIAS_REGEX_JSON", "").strip()

        if aliases_raw:
            try:
                parsed_aliases = json.loads(aliases_raw)
                if isinstance(parsed_aliases, dict):
                    for alias, canonical in parsed_aliases.items():
                        if not isinstance(alias, str) or not isinstance(canonical, str):
                            continue
                        normalized = self._normalize_name(alias)
                        if normalized:
                            self._category_aliases[normalized] = canonical.strip()
            except Exception:
                # Keep defaults if env payload is invalid.
                pass

        if regex_raw:
            try:
                parsed_regex = json.loads(regex_raw)
                if isinstance(parsed_regex, dict):
                    compiled_patterns: List[Tuple[re.Pattern, str]] = []
                    for canonical, pattern in parsed_regex.items():
                        if not isinstance(canonical, str) or not isinstance(pattern, str):
                            continue
                        compiled_patterns.append((re.compile(pattern, flags=re.IGNORECASE), canonical.strip()))
                    if compiled_patterns:
                        # Env patterns are prepended so teams can override matching behavior.
                        self._category_alias_patterns = compiled_patterns + self._category_alias_patterns
            except Exception:
                # Keep defaults if env payload is invalid.
                pass

    def _find_existing_name(self, proposal: GradebookProposal, target_name: str) -> Optional[str]:
        target = self._normalize_name(target_name)

        def _variants(name: str) -> set[str]:
            variants = {name}
            if not name:
                return variants
            if name.endswith("ies") and len(name) > 3:
                variants.add(name[:-3] + "y")
            if name.endswith("s") and len(name) > 1:
                variants.add(name[:-1])
            else:
                variants.add(name + "s")
            return {v for v in variants if v}

        target_variants = _variants(target)
        for cat in proposal.categories:
            category_variants = _variants(self._normalize_name(cat.name))
            if target_variants & category_variants:
                return cat.name

        # Token-level fallback to handle cases like "Final" vs "Final Exam".
        def _tokens(name: str) -> set[str]:
            words = re.findall(r"[a-z0-9]+", name or "")
            stop = {"exam", "category", "grades", "grade", "total"}
            return {w for w in words if w and w not in stop}

        target_tokens = _tokens(target)
        if target_tokens:
            for cat in proposal.categories:
                cat_tokens = _tokens(self._normalize_name(cat.name))
                if not cat_tokens:
                    continue
                if target_tokens == cat_tokens:
                    return cat.name
                if target_tokens.issubset(cat_tokens) or cat_tokens.issubset(target_tokens):
                    return cat.name
        return None

    # Phrases that ask the agent to auto-normalize weights to sum to 100%.
    _NORMALIZE_PATTERNS = re.compile(
        r"\b("
        r"rebalance|normalize|normalise"
        r"|make\s+(?:the\s+)?total\s+(?:weight\s+)?(?:equal\s+)?100"
        r"|set\s+(?:the\s+)?total\s+(?:to\s+)?100"
        r"|round\s+(?:up\s+)?to\s+100"
        r"|total\s+(?:should\s+(?:be|equal)|must\s+be|exactly)\s+100"
        r"|keep\s+total\s+(?:exactly\s+)?100"
        r"|(?:total\s+)?(?:weight\s+)?100\s*%"
        r")\b",
        flags=re.IGNORECASE,
    )

    def _post_update_checks(self, proposal: GradebookProposal, prompt: str) -> None:
        proposal.notes = [
            n for n in (proposal.notes or [])
            if not str(n).lower().startswith("weight check:")
        ]

        total = sum(cat.weight for cat in proposal.categories)

        normalize_requested = bool(self._NORMALIZE_PATTERNS.search(prompt))
        if normalize_requested and proposal.categories and abs(total) > 0.001:
            scale = 100.0 / total
            for cat in proposal.categories:
                cat.weight = round(cat.weight * scale, 2)
            total = sum(cat.weight for cat in proposal.categories)
            self._append_unique_note(proposal, "Auto-normalized category weights to 100%.")

        for issue in validate_proposal_weights(proposal):
            if issue.get("path") == ["proposal"]:
                self._append_unique_note(
                    proposal,
                    f"Weight check: total is {float(issue.get('total_weight', total)):.1f}% (expected 100%)."
                )
                break

        self._check_split_consistency(proposal)
        self._check_item_weight_consistency(proposal)
        self._check_rule_effectiveness(proposal)

    def _check_item_weight_consistency(self, proposal: GradebookProposal) -> None:
        for category in proposal.categories:
            weights = getattr(category, "item_weights", None) or {}
            if not isinstance(weights, dict) or not weights:
                continue

            total = 0.0
            for item_name, raw in weights.items():
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    self._append_unique_note(
                        proposal,
                        f"Item weight check: '{item_name}' in {category.name} has an invalid weight value.",
                    )
                    continue

                if value < 0.0 or value > 100.0:
                    self._append_unique_note(
                        proposal,
                        f"Item weight check: '{item_name}' in {category.name} must be between 0% and 100%.",
                    )
                total += max(0.0, value)

            if total > 100.0 + 0.001:
                self._append_unique_note(
                    proposal,
                    f"Item weight check: {category.name} item weights total {total:.1f}% (max 100%).",
                )

    def _check_split_consistency(self, proposal: GradebookProposal) -> None:
        # Replace split-check notes every turn so stale warnings disappear once fixed.
        proposal.notes = [
            n for n in (proposal.notes or [])
            if not (
                str(n).lower().startswith("split check:")
                or " internal split totals " in str(n).lower()
            )
        ]

        for parent in proposal.categories:
            parts = list(getattr(parent, "subcategories", []) or [])
            if not parts:
                continue
            split_total = sum(float(part.weight or 0) for part in parts)
            if abs(split_total - parent.weight) > 0.1:
                self._append_unique_note(
                    proposal,
                    f"Split check: {parent.name} internal split totals {split_total:.1f}% while parent weight is {parent.weight:.1f}%. "
                    "Please update split weights if you want them to match."
                )

    def _check_rule_effectiveness(self, proposal: GradebookProposal) -> None:
        for category in proposal.categories:
            child_count = 0
            if getattr(category, "subcategories", None):
                child_count = len(category.subcategories)
            elif getattr(category, "items", None):
                child_count = len(category.items)

            if child_count <= 0:
                continue

            keep_highest = int(getattr(category, "keep_highest", 0) or 0)
            if keep_highest > 0 and keep_highest >= child_count:
                self._append_unique_note(
                    proposal,
                    f"{category.name} keep-highest setting has no practical effect right now because the category has only {child_count} graded child {'item' if child_count == 1 else 'items'}."
                )

            drop_lowest = int(getattr(category, "drop_lowest", 0) or 0)
            if drop_lowest > 0 and drop_lowest >= child_count:
                self._append_unique_note(
                    proposal,
                    f"{category.name} drop-lowest setting has no practical effect right now because the category has only {child_count} graded child {'item' if child_count == 1 else 'items'}."
                )

    @staticmethod
    def _normalize_name(value: str) -> str:
        cleaned = value.lower()
        cleaned = re.sub(r"\b(set|make|change|adjust|update|keep|with|to|is|are|exactly|total|category|categories)\b", " ", cleaned)
        cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()
