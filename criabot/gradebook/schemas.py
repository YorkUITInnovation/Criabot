from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class MoodleResource(BaseModel):
    cmid: Optional[int] = None
    type: Optional[str] = None
    name: str
    section: Optional[str] = None
    content_url: Optional[str] = None
    content_preview: Optional[str] = None


class CourseActivity(BaseModel):
    cmid: Optional[int] = None
    module: Optional[str] = None
    name: str
    grade_item_id: Optional[int] = None
    itemtype: Optional[str] = Field(default="mod", description="Grade item type: 'mod' for activities, 'manual' for manual entries, etc.")


# Moodle GRADE_DISPLAY_TYPE constants
GRADE_DISPLAY_TYPE_DEFAULT            = 0
GRADE_DISPLAY_TYPE_REAL               = 1
GRADE_DISPLAY_TYPE_PERCENTAGE         = 2
GRADE_DISPLAY_TYPE_LETTER             = 3
GRADE_DISPLAY_TYPE_REAL_PERCENTAGE    = 12
GRADE_DISPLAY_TYPE_REAL_LETTER        = 13
GRADE_DISPLAY_TYPE_LETTER_REAL        = 31
GRADE_DISPLAY_TYPE_LETTER_PERCENTAGE  = 32
GRADE_DISPLAY_TYPE_PERCENTAGE_LETTER  = 23
GRADE_DISPLAY_TYPE_PERCENTAGE_REAL    = 21

GRADE_DISPLAY_TYPE_NAMES = {
    GRADE_DISPLAY_TYPE_DEFAULT:           "Default",
    GRADE_DISPLAY_TYPE_REAL:              "Real (numeric)",
    GRADE_DISPLAY_TYPE_PERCENTAGE:        "Percentage",
    GRADE_DISPLAY_TYPE_LETTER:            "Letter",
    GRADE_DISPLAY_TYPE_REAL_PERCENTAGE:   "Real + Percentage",
    GRADE_DISPLAY_TYPE_REAL_LETTER:       "Real + Letter",
    GRADE_DISPLAY_TYPE_LETTER_REAL:       "Letter + Real",
    GRADE_DISPLAY_TYPE_LETTER_PERCENTAGE: "Letter + Percentage",
    GRADE_DISPLAY_TYPE_PERCENTAGE_LETTER: "Percentage + Letter",
    GRADE_DISPLAY_TYPE_PERCENTAGE_REAL:   "Percentage + Real",
}


class GradebookSubcategory(BaseModel):
    name: str
    weight: float
    aggregation_method: Optional[int] = Field(
        default=None,
        description="Optional Moodle aggregation method override for this subcategory.",
    )


class GradebookCategory(BaseModel):
    name: str
    weight: float
    items: List[str] = Field(default_factory=list)
    subcategories: List[GradebookSubcategory] = Field(default_factory=list)

    # Aggregation / drop-keep settings
    drop_lowest: int = Field(default=0, description="Drop N lowest grade items from this category")
    keep_highest: int = Field(default=0, description="Keep only the N highest grade items (0 = keep all)")
    aggregate_only_graded: bool = Field(default=True, description="Exclude empty/ungraded items from aggregation")
    aggregate_outcomes: bool = Field(default=False, description="Include outcome items in category aggregation")
    extra_credit: bool = Field(default=False, description="Treat this category as extra credit")

    # Category total grade item settings
    grade_min: Optional[float] = Field(default=None, description="Minimum points for the category total (default from Moodle)")
    grade_max: float = Field(default=100.0, description="Maximum points for the category total (default 100)")
    grade_pass: Optional[float] = Field(default=None, description="Passing grade threshold (e.g. 60 means 60/100 required to pass)")
    hidden: bool = Field(default=False, description="Hide this category from students")
    hidden_until: Optional[int] = Field(default=None, description="Hide until Unix timestamp (UTC)")
    locked: bool = Field(default=False, description="Lock grades in this category (prevent overrides)")
    lock_time: Optional[int] = Field(default=None, description="Auto-lock at Unix timestamp (UTC)")
    display_type: int = Field(default=GRADE_DISPLAY_TYPE_DEFAULT, description="How grades are displayed (0=default, 1=real, 2=percentage, 3=letter, ...)")
    decimals: int = Field(default=-1, description="Decimal places to display (-1 = course default, 0-5 = explicit)")

    # Optional formula-based computation metadata.
    calculation_formula: Optional[str] = Field(
        default=None,
        description="Excel-style formula used to compute the category total (e.g. =([midterm]*0.4)+([final]*0.6))",
    )
    formula_item_refs: List[str] = Field(
        default_factory=list,
        description="Referenced item names/IDs extracted from calculation_formula.",
    )
    formula_unresolved_refs: List[str] = Field(
        default_factory=list,
        description="Formula references that could not be resolved to grade item identifiers.",
    )


class GradebookProposal(BaseModel):
    categories: List[GradebookCategory] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    aggregation_method: int = Field(default=13, description="Moodle aggregation method: 0=Mean, 10=Weighted mean, 11=Simple weighted mean, 12=Mean with extra credits, 13=Natural (default)")


class GradebookSessionRecord(BaseModel):
    session_id: str
    course_id: str
    professor_id: str
    bot_name: str
    phase: str
    moodle_resources: List[MoodleResource] = Field(default_factory=list)
    course_activities: List[CourseActivity] = Field(default_factory=list)
    extraction: Optional[dict] = None
    proposal: Optional[GradebookProposal] = None
    content_mapping: Optional[dict] = None
    last_touched_at: Optional[int] = None
    uploaded_document_ids: List[str] = Field(default_factory=list, description="Document IDs uploaded in this session")
