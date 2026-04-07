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


class GradebookCategory(BaseModel):
    name: str
    weight: float
    items: List[str] = Field(default_factory=list)


class GradebookProposal(BaseModel):
    categories: List[GradebookCategory] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


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
