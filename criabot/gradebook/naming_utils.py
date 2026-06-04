from __future__ import annotations

import re
from typing import Iterable, List

from .schemas import CourseActivity, MoodleResource

_SYLLABUS_NAME_TOKENS = (
    "syllabus",
    "syllabi",
    "syllabe",
    "plan de cours",
    "plan du cours",
    "programme",
    "outline",
    "course outline",
)

GRADEABLE_ACTIVITY_MODULES = frozenset(
    {
        "assign",
        "assignment",
        "quiz",
        "forum",
        "workshop",
        "lesson",
        "scorm",
        "h5pactivity",
        "lti",
        "bigbluebuttonbn",
        "choice",
        "feedback",
        "glossary",
        "survey",
        "chat",
        "data",
        "wiki",
        "attendance",
    }
)

_NON_GRADEABLE_RESOURCE_TYPES = frozenset(
    {"file", "resource", "page", "folder", "label", "book", "url"}
)


def is_syllabus_like_name(name: str) -> bool:
    normalized = (name or "").strip().lower()
    if not normalized:
        return False
    return any(token in normalized for token in _SYLLABUS_NAME_TOKENS)


def activity_name_matches_lab(name: str) -> bool:
    return re.search(r"\b(?:labs?|laboratory)\b", name or "", re.IGNORECASE) is not None


def is_gradeable_activity_module(module: str) -> bool:
    return (module or "").strip().lower() in GRADEABLE_ACTIVITY_MODULES


def derive_course_activities_from_resources(resources: Iterable[MoodleResource]) -> List[CourseActivity]:
    """Build gradeable session activities from Moodle resources when none were supplied."""
    seen: set[str] = set()
    activities: List[CourseActivity] = []

    for resource in resources:
        name = (resource.name or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        if is_syllabus_like_name(name):
            continue

        module = (resource.type or "").strip().lower()
        if not module or module in _NON_GRADEABLE_RESOURCE_TYPES:
            continue
        if not is_gradeable_activity_module(module):
            continue

        seen.add(key)
        activities.append(
            CourseActivity(
                cmid=resource.cmid,
                module=resource.type,
                name=resource.name,
            )
        )

    return activities
