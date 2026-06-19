import pytest
import time
import json
from unittest.mock import AsyncMock, MagicMock

from criabot.gradebook.analyzer import SyllabusAnalyzer
from criabot.gradebook.content_mapper import ContentMapper
from criabot.gradebook.conversation import ConversationManager
from criabot.gradebook.formula_parser import FormulaParser
from criabot.gradebook.formula_resolver import FormulaResolver
from criabot.gradebook.proposal import (
    ProposalGenerator,
    validate_proposal_weights,
    sync_proposal_items_from_mapping,
    sync_mapping_from_proposal_manual_items,
    snapshot_subcategories,
    infer_subcategory_renames,
    sync_content_mapping_with_proposal_subcategory_changes,
    sync_confirmed_mapping_into_content_mapping,
    apply_mapping_subcategory_operations,
    sync_mapping_subcategories_from_proposal,
    sync_mapping_not_graded_items,
    sync_proposal_not_graded_from_confirmed_mapping,
    is_subcategory_only_proposal_change,
    should_invalidate_content_mapping,
    NOT_GRADED_CATEGORY,
)
from criabot.gradebook.schemas import CourseActivity, GradebookCategory, GradebookProposal, GradebookSessionRecord, MoodleResource, GradebookSubcategory
from criabot.gradebook.session import GradebookSessionEngine


def _mapper_proposal_with_categories(*names: str) -> GradebookProposal:
    weight = 100.0 / len(names) if names else 100.0
    return GradebookProposal(
        categories=[GradebookCategory(name=name, weight=weight) for name in names]
    )


@pytest.mark.asyncio
async def test_syllabus_analyzer_prefers_graph_search():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(return_value={
        "response": {"nodes": [], "assets": [], "search_units": 1},
        "graph_metadata": {"source": "ragflow"},
    })
    sdk.content.search = AsyncMock()

    analyzer = SyllabusAnalyzer(criadex=sdk)
    result = await analyzer.analyze_group(
        group_name="course-syllabus-index",
        prompt="What are grade category weights?",
    )

    assert result["graph_metadata"]["source"] == "ragflow"
    sdk.manage.graph_search.assert_called_once()
    sdk.content.search.assert_not_called()


@pytest.mark.asyncio
async def test_syllabus_analyzer_falls_back_to_standard_search():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(side_effect=RuntimeError("graph unavailable"))
    sdk.content.search = AsyncMock(return_value={"response": {"nodes": [], "assets": [], "search_units": 1}})

    analyzer = SyllabusAnalyzer(criadex=sdk)
    result = await analyzer.analyze_group(
        group_name="course-syllabus-index",
        prompt="How are exams weighted?",
    )

    assert result["graph_metadata"]["source"] == "fallback"
    sdk.content.search.assert_called_once()


@pytest.mark.asyncio
async def test_syllabus_analyzer_caches_repeated_queries():
    sdk = MagicMock()
    sdk.manage = MagicMock()
    sdk.content = MagicMock()
    sdk.manage.graph_search = AsyncMock(return_value={
        "response": {"nodes": [], "assets": [], "search_units": 1},
        "graph_metadata": {"source": "ragflow"},
    })

    analyzer = SyllabusAnalyzer(criadex=sdk)
    analyzer._analysis_cache.clear()

    await analyzer.analyze_group(
        group_name="course-syllabus-index",
        prompt="What are grade category weights?",
    )
    await analyzer.analyze_group(
        group_name="course-syllabus-index",
        prompt="What are grade category weights?",
    )

    sdk.manage.graph_search.assert_awaited_once()


@pytest.mark.asyncio
async def test_gradebook_session_starts_in_intake_without_syllabus():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Lecture 1 slides")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    assert session.phase == "INTAKE"
    assert session.proposal is None


@pytest.mark.asyncio
async def test_gradebook_session_persists_baseline_snapshot():
    engine = GradebookSessionEngine()
    baseline_snapshot = {
        "contract_name": "baseline_gradebook_v1",
        "schema_version": 1,
        "available": True,
        "courseid": "EECS-1000",
        "root_category": {"id": 1, "name": "Course total"},
        "tree": {"type": "category", "children": [{"type": "item"}]},
        "stats": {"category_count": 1, "item_count": 1, "max_depth": 1},
    }

    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Lecture 1 slides")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot=baseline_snapshot,
        import_mode="baseline",
    )

    assert session.extraction is not None
    assert session.phase == "BASELINE_READY"
    assert session.extraction.get("baseline_available") is True
    assert session.extraction.get("baseline_snapshot", {}).get("schema_version") == 1
    assert session.extraction.get("import_mode") == "baseline"
    assert session.extraction.get("context_source") == "baseline_import"
    assert session.extraction.get("baseline_policy") == "mirror_then_override"
    assert isinstance(session.extraction.get("baseline_import_snapshot_ref"), str)
    assert len(session.extraction.get("baseline_import_snapshot_ref")) == 64


@pytest.mark.asyncio
async def test_gradebook_session_forces_fresh_mode_when_baseline_unavailable():
    engine = GradebookSessionEngine()
    baseline_snapshot = {
        "contract_name": "baseline_gradebook_v1",
        "schema_version": 1,
        "available": False,
        "courseid": "EECS-1000",
        "root_category": None,
        "tree": None,
        "stats": None,
    }

    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Lecture 1 slides")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot=baseline_snapshot,
        import_mode="baseline",
    )

    assert session.extraction is not None
    assert session.phase == "INTAKE"
    assert session.extraction.get("baseline_available") is False
    assert session.extraction.get("import_mode") == "fresh"
    assert session.extraction.get("context_source") == "syllabus_generation"
    assert session.extraction.get("baseline_policy") == "generate_fresh"


@pytest.mark.asyncio
async def test_gradebook_session_with_generic_resource_type_stays_intake():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Week 1 slides", type="resource")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    assert session.phase == "INTAKE"


@pytest.mark.asyncio
async def test_gradebook_chat_affirmation_advances_analysis_to_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_b",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    assert session.phase == "ANALYSIS"

    updated = await engine.chat(session.session_id, "okay do it")
    assert updated.phase == "PROPOSAL"


@pytest.mark.asyncio
async def test_gradebook_baseline_ready_advances_to_proposal_without_analysis_regen():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2000",
        professor_id="prof_baseline",
        bot_name="eecs-baseline-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2000",
            "root_category": {"id": 1, "name": "Course total"},
            "tree": {"type": "category", "children": {"1": {"type": "item", "depth": 2}}},
            "stats": {"category_count": 1, "item_count": 1, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None

    updated = await engine.chat(session.session_id, "show proposal")
    assert updated.phase == "PROPOSAL"


@pytest.mark.asyncio
async def test_gradebook_baseline_show_proposal_does_not_mutate_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2000b",
        professor_id="prof_baseline_view",
        bot_name="eecs-baseline-view-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2000b",
            "root_category": {"id": 1, "name": "Course total"},
            "tree": {"type": "category", "children": {"1": {"type": "item", "depth": 2}}},
            "stats": {"category_count": 1, "item_count": 1, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    before = session.proposal.model_dump() if session.proposal else None

    updated = await engine.chat(session.session_id, "show proposal")

    assert updated.phase == "PROPOSAL"
    assert updated.proposal is not None
    assert updated.proposal.model_dump() == before
    assert bool((updated.extraction or {}).get("proposal_changed")) is False


def test_show_proposal_is_not_gradebook_refinement():
    assert ConversationManager._looks_like_gradebook_refinement("show proposal") is False
    assert ConversationManager._is_read_only_gradebook_prompt("show proposal") is True


def test_show_proposal_does_not_unhide_proposal_category():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Quizzes", weight=15, hidden=True),
            GradebookCategory(name="Proposal", weight=10, hidden=True),
        ]
    )
    before = proposal.model_dump()
    updated = generator.update_from_prompt(proposal, "show proposal")
    assert updated.model_dump() == before


@pytest.mark.asyncio
async def test_gradebook_baseline_ready_start_fresh_switches_to_fresh_proposal_mode():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2001",
        professor_id="prof_fresh_switch",
        bot_name="eecs-fresh-switch-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Midterm Quiz", module="quiz"),
        ],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2001",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Baseline Assignments",
                        "aggregationcoef2": 0.5,
                        "weightoverride": True,
                        "children": {},
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Baseline Exams",
                        "aggregationcoef2": 0.5,
                        "weightoverride": True,
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 2, "item_count": 0, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.extraction is not None
    assert session.extraction.get("import_mode") == "baseline"

    session = await engine.chat(session.session_id, "start fresh")
    assert session.phase == "ANALYSIS"
    assert session.extraction.get("import_mode") == "fresh"
    assert session.extraction.get("context_source") == "syllabus_generation"
    assert session.extraction.get("baseline_policy") == "generate_fresh"
    assert session.proposal is not None
    assert all("baseline" not in str(note).lower() for note in (session.proposal.notes or []))

    session = await engine.chat(session.session_id, "give me a proposal")
    assert session.phase == "PROPOSAL"
    assert session.proposal is not None
    assert all("baseline" not in str(note).lower() for note in (session.proposal.notes or []))


@pytest.mark.asyncio
async def test_gradebook_baseline_start_mirrors_existing_categories_in_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2010",
        professor_id="prof_baseline",
        bot_name="eecs-baseline-bot-3",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Final Quiz", module="quiz"),
        ],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2010",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Coursework",
                        "aggregationcoef2": 0.7,
                        "weightoverride": True,
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Homework 1",
                                "itemtype": "mod",
                                "itemmodule": "assign",
                                "children": {},
                            }
                        },
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Exams",
                        "aggregationcoef2": 0.3,
                        "weightoverride": True,
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Final Quiz",
                                "itemtype": "mod",
                                "itemmodule": "quiz",
                                "children": {},
                            }
                        },
                    },
                },
            },
            "stats": {"category_count": 2, "item_count": 2, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    names = [c.name for c in session.proposal.categories]
    assert names == ["Coursework", "Exams"]
    assert session.proposal.categories[0].items == ["Homework 1"]
    assert session.proposal.categories[1].items == ["Final Quiz"]
    assert session.proposal.categories[0].weight == pytest.approx(70.0)
    assert session.proposal.categories[1].weight == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_gradebook_reset_clears_persisted_chat_history():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2011",
        professor_id="prof_reset",
        bot_name="eecs-reset-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )

    await engine.persist_chat_turn(session.session_id, "show proposal", "Here is the proposal")
    existing = await engine.get(session.session_id)
    assert existing is not None
    assert len(engine.get_chat_history(existing)) == 2

    reset = await engine.reset(session.session_id, keep_extraction=True)
    assert engine.get_chat_history(reset) == []


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_excludes_category_totals_and_derives_weights_from_points():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2020",
        professor_id="prof_points",
        bot_name="eecs-points-bot",
        moodle_resources=[],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2020",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Coursework",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "grademax": 40,
                                "children": {},
                            },
                            "2": {
                                "type": "item",
                                "depth": 3,
                                "name": "Homework 1",
                                "itemtype": "mod",
                                "itemmodule": "assign",
                                "children": {},
                            },
                        },
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Exams",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "grademax": 60,
                                "children": {},
                            },
                            "2": {
                                "type": "item",
                                "depth": 3,
                                "name": "Final Quiz",
                                "itemtype": "mod",
                                "itemmodule": "quiz",
                                "children": {},
                            },
                        },
                    },
                },
            },
            "stats": {"category_count": 2, "item_count": 4, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    names = [c.name for c in session.proposal.categories]
    assert names == ["Coursework", "Exams"]
    assert session.proposal.categories[0].items == ["Homework 1"]
    assert session.proposal.categories[1].items == ["Final Quiz"]
    assert session.proposal.categories[0].weight == pytest.approx(40.0)
    assert session.proposal.categories[1].weight == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_imports_category_rules_and_settings():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2030",
        professor_id="prof_rules",
        bot_name="eecs-rules-bot",
        moodle_resources=[],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2030",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignments",
                        "droplow": 1,
                        "keephigh": 0,
                        "aggregateonlygraded": False,
                        "aggregateoutcomes": True,
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "grademax": 100,
                                "gradepass": 60,
                                "hidden": 1,
                                "hiddenuntil": 1767225600,
                                "locked": True,
                                "locktime": 1769904000,
                                "display": 2,
                                "decimals": 2,
                                "children": {},
                            },
                            "2": {
                                "type": "item",
                                "depth": 3,
                                "name": "Homework 1",
                                "itemtype": "mod",
                                "itemmodule": "assign",
                                "children": {},
                            },
                        },
                    }
                },
            },
            "stats": {"category_count": 1, "item_count": 2, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    cat = session.proposal.categories[0]
    assert cat.drop_lowest == 1
    assert cat.keep_highest == 0
    assert cat.aggregate_only_graded is False
    assert cat.aggregate_outcomes is True
    assert cat.hidden is True
    assert cat.hidden_until == 1767225600
    assert cat.locked is True
    assert cat.lock_time == 1769904000
    assert cat.display_type == 2
    assert cat.decimals == 2
    assert cat.grade_pass == pytest.approx(60.0)
    assert any(
        "Baseline category rules were imported" in note
        for note in (session.proposal.notes or [])
    )


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_preserves_numeric_category_order():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2031",
        professor_id="prof_sort",
        bot_name="eecs-sort-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2031",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "11": {
                        "type": "category",
                        "depth": 2,
                        "name": "Final Exam",
                        "aggregationcoef2": 0.25,
                        "weightoverride": True,
                        "children": {},
                    },
                    "4": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignments",
                        "aggregationcoef2": 0.25,
                        "weightoverride": True,
                        "children": {},
                    },
                    "7": {
                        "type": "category",
                        "depth": 2,
                        "name": "Quizzes",
                        "aggregationcoef2": 0.25,
                        "weightoverride": True,
                        "children": {},
                    },
                    "9": {
                        "type": "category",
                        "depth": 2,
                        "name": "Midterm",
                        "aggregationcoef2": 0.25,
                        "weightoverride": True,
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 4, "item_count": 0, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    assert [c.name for c in session.proposal.categories] == ["Assignments", "Quizzes", "Midterm", "Final Exam"]


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_imports_nested_and_root_uncategorized_nodes():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2031b",
        professor_id="prof_nested",
        bot_name="eecs-nested-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2031b",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignments",
                        "aggregationcoef2": 0.5,
                        "children": {
                            "1": {
                                "type": "category",
                                "depth": 3,
                                "name": "Homework",
                                "children": {
                                    "1": {
                                        "type": "item",
                                        "depth": 4,
                                        "name": "HW 1",
                                        "itemtype": "mod",
                                        "itemmodule": "assign",
                                        "children": {},
                                    },
                                },
                            },
                        },
                    },
                    "2": {
                        "type": "item",
                        "depth": 2,
                        "name": "Legacy Participation",
                        "itemtype": "manual",
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 2, "item_count": 2, "max_depth": 4},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None

    by_name = {c.name: c for c in session.proposal.categories}
    assert "Assignments" in by_name
    assert "Uncategorized" in by_name
    assert by_name["Assignments"].subcategories
    assert by_name["Assignments"].subcategories[0].name == "Homework"
    assert "Legacy Participation" in by_name["Uncategorized"].items


@pytest.mark.asyncio
async def test_gradebook_baseline_root_quiz_item_routes_to_quizzes_not_uncategorized():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2031c",
        professor_id="prof_baseline_quiz",
        bot_name="eecs-baseline-quiz-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2031c",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Quizzes",
                        "aggregationcoef2": 0.2,
                        "children": {},
                    },
                    "2": {
                        "type": "item",
                        "depth": 2,
                        "name": "Quiz 1",
                        "itemtype": "mod",
                        "itemmodule": "quiz",
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 1, "item_count": 1, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None

    by_name = {c.name: c for c in session.proposal.categories}
    assert "Quizzes" in by_name
    assert "Quiz 1" in by_name["Quizzes"].items
    assert "Uncategorized" not in by_name


@pytest.mark.asyncio
async def test_gradebook_baseline_root_course_total_not_added_to_uncategorized():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2031d",
        professor_id="prof_baseline_course_total",
        bot_name="eecs-baseline-course-total-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2031d",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "courseitem",
                        "depth": 2,
                        "name": "Course total",
                        "itemtype": "course",
                        "children": {},
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignments",
                        "aggregationcoef2": 1.0,
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 1, "item_count": 1, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None

    by_name = {c.name: c for c in session.proposal.categories}
    assert "Assignments" in by_name
    assert "Uncategorized" not in by_name


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_imports_item_weights_from_category_items():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2032",
        professor_id="prof_item_weights",
        bot_name="eecs-item-weights-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2032",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Quizzes",
                        "aggregation": 10,
                        "aggregationcoef2": 0.25,
                        "weightoverride": False,
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Midterm Quiz",
                                "itemtype": "mod",
                                "itemmodule": "quiz",
                                "aggregationcoef": 0.4,
                                "children": {},
                            },
                            "2": {
                                "type": "item",
                                "depth": 3,
                                "name": "Final Quiz",
                                "itemtype": "mod",
                                "itemmodule": "quiz",
                                "aggregationcoef": 0.6,
                                "children": {},
                            },
                        },
                    },
                },
            },
            "stats": {"category_count": 1, "item_count": 2, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    quizzes = session.proposal.categories[0]
    assert quizzes.name == "Quizzes"
    assert quizzes.item_weights.get("Midterm Quiz") == pytest.approx(40.0)
    assert quizzes.item_weights.get("Final Quiz") == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_uses_category_total_item_weights_for_top_level_categories():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2032b",
        professor_id="prof_top_weight_items",
        bot_name="eecs-top-weight-items-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2032b",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 10,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignment",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "aggregationcoef": 25.0,
                                "children": {},
                            }
                        },
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Quizzes",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "aggregationcoef": 10.0,
                                "children": {},
                            }
                        },
                    },
                    "3": {
                        "type": "category",
                        "depth": 2,
                        "name": "Midterm",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "aggregationcoef": 30.0,
                                "children": {},
                            }
                        },
                    },
                    "4": {
                        "type": "category",
                        "depth": 2,
                        "name": "Final",
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "aggregationcoef": 35.0,
                                "children": {},
                            }
                        },
                    },
                },
            },
            "stats": {"category_count": 4, "item_count": 4, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None

    by_name = {c.name: c for c in session.proposal.categories}
    assert by_name["Assignment"].weight == pytest.approx(25.0)
    assert by_name["Quizzes"].weight == pytest.approx(10.0)
    assert by_name["Midterm"].weight == pytest.approx(30.0)
    assert by_name["Final"].weight == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_prefers_uniform_top_level_aggregation_method():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2033",
        professor_id="prof_agg",
        bot_name="eecs-agg-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2033",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Assignments",
                        "aggregation": 10,
                        "aggregationcoef2": 0.25,
                        "children": {},
                    },
                    "2": {
                        "type": "category",
                        "depth": 2,
                        "name": "Quizzes",
                        "aggregation": 10,
                        "aggregationcoef2": 0.25,
                        "children": {},
                    },
                },
            },
            "stats": {"category_count": 2, "item_count": 0, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    assert session.proposal.aggregation_method == 10


@pytest.mark.asyncio
async def test_gradebook_baseline_snapshot_infers_item_level_hidden_and_suppresses_default_min_pass_decimals():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2034",
        professor_id="prof_visibility",
        bot_name="eecs-visibility-bot",
        moodle_resources=[],
        course_activities=[],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2034",
            "root_category": {
                "id": 1,
                "name": "Course total",
                "aggregation": 13,
                "keephigh": 0,
                "droplow": 0,
                "aggregateonlygraded": True,
                "aggregateoutcomes": False,
            },
            "tree": {
                "type": "category",
                "depth": 1,
                "children": {
                    "1": {
                        "type": "category",
                        "depth": 2,
                        "name": "Final Exam",
                        "aggregation": 10,
                        "children": {
                            "1": {
                                "type": "item",
                                "depth": 3,
                                "name": "Category total",
                                "itemtype": "category",
                                "grademin": 0,
                                "gradepass": 0,
                                "decimals": 0,
                                "children": {},
                            },
                            "2": {
                                "type": "item",
                                "depth": 3,
                                "name": "Final Manual",
                                "itemtype": "manual",
                                "hidden": 1,
                                "children": {},
                            },
                        },
                    },
                },
            },
            "stats": {"category_count": 1, "item_count": 2, "max_depth": 3},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    assert session.proposal is not None
    cat = session.proposal.categories[0]
    assert cat.hidden is True
    assert cat.grade_min is None
    assert cat.grade_pass is None
    assert cat.decimals == -1


@pytest.mark.asyncio
async def test_gradebook_baseline_ready_can_switch_to_fresh_analysis_on_request():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-2001",
        professor_id="prof_baseline",
        bot_name="eecs-baseline-bot-2",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 35%, Final 40%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
        baseline_snapshot={
            "contract_name": "baseline_gradebook_v1",
            "schema_version": 1,
            "available": True,
            "courseid": "EECS-2001",
            "root_category": {"id": 1, "name": "Course total"},
            "tree": {"type": "category", "children": {"1": {"type": "item", "depth": 2}}},
            "stats": {"category_count": 1, "item_count": 1, "max_depth": 2},
        },
        import_mode="baseline",
    )

    assert session.phase == "BASELINE_READY"
    updated = await engine.chat(session.session_id, "start fresh")
    assert updated.phase == "ANALYSIS"


@pytest.mark.asyncio
async def test_gradebook_chat_answers_syllabus_question_without_repeating_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_b",
        bot_name="eecs-bot",
        moodle_resources=[],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    assert session.phase == "INTAKE"

    chat = await engine.chat(session.session_id, "do you have syllabus?")
    from criabot.gradebook.conversation import ConversationManager
    reply = ConversationManager().make_reply(chat, chat.proposal, prompt="do you have syllabus?")
    assert "no" in reply.lower()
    assert "syllabus" in reply.lower()


@pytest.mark.asyncio
async def test_intake_with_uploaded_syllabus_flag_moves_to_proposal_on_request():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_c",
        bot_name="eecs-bot",
        moodle_resources=[],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    assert session.phase == "INTAKE"

    # Simulate syllabus upload metadata already stored in session extraction.
    session.extraction = {
        "has_syllabus": True,
        "syllabus_sources": ["syllabus_example.docx"],
    }
    await engine._save_session(session)

    updated = await engine.chat(session.session_id, "okay use what i give you and give me a proposal")
    assert updated.phase == "PROPOSAL"
    assert updated.proposal is not None


@pytest.mark.asyncio
async def test_gradebook_accept_adds_content_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )
    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"
    assert accepted.content_mapping is not None
    assert accepted.content_mapping["graded_activities"][0]["moodle_cmid"] == 10
    assert accepted.content_mapping["graded_activities"][0]["suggested_category"] == "Assignments"


@pytest.mark.asyncio
async def test_gradebook_accept_is_idempotent():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    first = await engine.accept(session.session_id)
    second = await engine.accept(session.session_id)

    assert first.content_mapping == second.content_mapping
    assert second.phase == "ACCEPTED"


@pytest.mark.asyncio
async def test_gradebook_finalize_marks_confirmed_mapping_and_completes():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[{"moodle_cmid": 10, "category": "Homework"}],
    )

    assert finalized.phase == "COMPLETED"
    assert finalized.content_mapping["graded_activities"][0]["confirmed_category"] == "Homework"
    assert finalized.content_mapping["graded_activities"][0]["finalized"] is True


@pytest.mark.asyncio
async def test_gradebook_finalize_prefers_grade_item_id_over_mismatched_cmid():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    engine._content_mapper.build_mapping = AsyncMock(return_value={
        "graded_activities": [
            {
                "grade_item_id": 501,
                "moodle_cmid": 999,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
            }
        ],
        "validation_errors": [],
    })

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[{"grade_item_id": "501", "moodle_cmid": 10, "category": "Homework"}],
    )

    row = finalized.content_mapping["graded_activities"][0]
    assert finalized.phase == "COMPLETED"
    assert row["confirmed_category"] == "Homework"
    assert row["finalized"] is True


@pytest.mark.asyncio
async def test_gradebook_finalize_stays_in_refinement_when_no_confirmed_rows_match():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    engine._content_mapper.build_mapping = AsyncMock(return_value={
        "graded_activities": [
            {
                "grade_item_id": 501,
                "moodle_cmid": 999,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
            }
        ],
        "validation_errors": [],
    })

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[{"grade_item_id": "777", "moodle_cmid": 10, "category": "Homework"}],
    )

    validation = ((finalized.content_mapping or {}).get("validation") or {})
    errors = [str(msg) for msg in (validation.get("errors") or [])]
    unresolved = validation.get("unresolved_confirmations") or []

    assert finalized.phase == "REFINEMENT"
    assert validation.get("can_proceed") is False
    assert any("Finalize mapping mismatch" in msg for msg in errors)
    assert len(unresolved) == 1


@pytest.mark.asyncio
async def test_post_finalize_edit_prompt_returns_to_refinement_with_updated_proposal_reply():
    engine = GradebookSessionEngine()
    conversation = ConversationManager()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 45%")],
        course_activities=[
            CourseActivity(name="Assignment 1", module="assign", cmid=901),
            CourseActivity(name="Midterm", module="quiz", cmid=902),
            CourseActivity(name="Final", module="quiz", cmid=903),
        ],
    )

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[
            {"moodle_cmid": 901, "category": "Assignments"},
            {"moodle_cmid": 902, "category": "Midterm"},
            {"moodle_cmid": 903, "category": "Final Exam"},
        ],
    )
    assert finalized.phase == "COMPLETED"

    prompt = "Actually, change midterm to 30%"
    edited = await engine.chat(session.session_id, prompt)
    reply = conversation.make_reply(session=edited, proposal=edited.proposal, prompt=prompt).lower()

    assert edited.phase == "REFINEMENT"
    assert "gradebook finalized successfully" not in reply
    assert "updated proposal" in reply


@pytest.mark.asyncio
async def test_post_finalize_first_edit_add_quizzes_is_applied_immediately():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 45%")],
        course_activities=[
            CourseActivity(name="Assignment 1", module="assign", cmid=901),
            CourseActivity(name="Midterm", module="quiz", cmid=902),
            CourseActivity(name="Final", module="quiz", cmid=903),
        ],
    )

    session = await engine.chat(session.session_id, "remove quizzes and assign it to final exam")
    assert all(cat.name != "Quizzes" for cat in (session.proposal.categories or []))

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[
            {"moodle_cmid": 901, "category": "Assignments"},
            {"moodle_cmid": 902, "category": "Midterm"},
            {"moodle_cmid": 903, "category": "Final Exam"},
        ],
    )
    assert finalized.phase == "COMPLETED"

    edited = await engine.chat(session.session_id, "added quizzes")
    categories = {cat.name: cat for cat in (edited.proposal.categories or [])}

    assert edited.phase == "REFINEMENT"
    assert bool((edited.extraction or {}).get("proposal_changed")) is True
    assert "Quizzes" in categories
    assert categories["Quizzes"].weight == 0.0


@pytest.mark.asyncio
async def test_post_finalize_first_edit_aggregation_phrase_is_applied_immediately():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 45%")],
        course_activities=[
            CourseActivity(name="Assignment 1", module="assign", cmid=901),
            CourseActivity(name="Midterm", module="quiz", cmid=902),
            CourseActivity(name="Final", module="quiz", cmid=903),
        ],
    )

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[
            {"moodle_cmid": 901, "category": "Assignments"},
            {"moodle_cmid": 902, "category": "Midterm"},
            {"moodle_cmid": 903, "category": "Final Exam"},
        ],
    )
    assert finalized.phase == "COMPLETED"
    assert finalized.proposal.aggregation_method == 13

    edited = await engine.chat(
        session.session_id,
        "Keep your default categories but set the aggregation method to Weighted mean of grades.",
    )

    assert edited.phase == "REFINEMENT"
    assert bool((edited.extraction or {}).get("proposal_changed")) is True
    assert edited.proposal.aggregation_method == 10
    assert any(
        "Aggregation method set to Weighted mean of grades" in note
        for note in (edited.proposal.notes or [])
    )


@pytest.mark.asyncio
async def test_refinement_weight_only_edit_preserves_content_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof-a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 45%")],
        course_activities=[
            CourseActivity(name="Assignment 1", module="assign", cmid=901),
            CourseActivity(name="Midterm", module="quiz", cmid=902),
            CourseActivity(name="Final", module="quiz", cmid=903),
        ],
    )

    accepted = await engine.accept(session.session_id)
    assert accepted.content_mapping is not None

    edited = await engine.chat(session.session_id, "make quizzes 10")

    assert edited.phase == "REFINEMENT"
    assert bool((edited.extraction or {}).get("proposal_changed")) is True
    assert edited.content_mapping is not None
    assert len((edited.content_mapping or {}).get("graded_activities") or []) > 0


@pytest.mark.asyncio
async def test_refinement_edit_invalidates_stale_content_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof-a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 45%")],
        course_activities=[
            CourseActivity(name="Assignment 1", module="assign", cmid=901),
            CourseActivity(name="Midterm", module="quiz", cmid=902),
            CourseActivity(name="Final", module="quiz", cmid=903),
        ],
    )

    accepted = await engine.accept(session.session_id)
    assert accepted.content_mapping is not None

    edited = await engine.chat(session.session_id, "remove Midterm")

    assert edited.phase == "REFINEMENT"
    assert bool((edited.extraction or {}).get("proposal_changed")) is True
    assert edited.content_mapping is None


@pytest.mark.asyncio
async def test_gradebook_reset_clears_mapping_and_restores_proposal_phase():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"
    assert accepted.content_mapping is not None

    reset = await engine.reset(session.session_id)
    assert reset.phase in {"PROPOSAL", "INTAKE"}
    assert reset.content_mapping is None
    assert reset.proposal is not None


@pytest.mark.asyncio
async def test_gradebook_delete_removes_session_from_engine():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    deleted = await engine.delete(session.session_id)
    assert isinstance(deleted, dict)
    assert deleted.get("success") is True
    assert deleted.get("existed") is True

    loaded = await engine.get(session.session_id)
    assert loaded is None


def _make_category(
    name,
    weight,
    *,
    extra_credit=False,
    subcategories=None,
):
    return GradebookCategory(
        name=name,
        weight=weight,
        items=[],
        subcategories=subcategories or [],
        extra_credit=extra_credit,
    )


def _make_subcategory(name, weight):
    return GradebookSubcategory(name=name, weight=weight)


def test_weight_validation_weighted_mean():
    proposal = GradebookProposal(
        categories=[
            _make_category("Assignments", 60.0),
            _make_category("Final Exam", 40.0),
        ],
        aggregation_method=10,
    )
    assert validate_proposal_weights(proposal) == []

    proposal.categories[0].weight = 70.0
    errors = validate_proposal_weights(proposal)
    assert errors
    assert errors[0]["aggregation_method"] == 10


def test_weight_validation_simple_weighted_mean_is_enforced():
    proposal = GradebookProposal(
        categories=[
            _make_category("Assignments", 99.0),
            _make_category("Final Exam", 1.0),
        ],
        aggregation_method=11,
    )
    assert validate_proposal_weights(proposal) == []

    proposal.categories[0].weight = 90.0
    errors = validate_proposal_weights(proposal)
    assert errors
    assert errors[0]["aggregation_method"] == 11


def test_post_update_checks_clears_stale_weight_check_when_total_is_100():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            _make_category("Assignments", 10.0),
            _make_category("Labs", 15.0),
            _make_category("Midterm", 30.0),
            _make_category("Final Exam", 30.0),
            _make_category("Quizzes", 15.0),
        ],
        aggregation_method=10,
        notes=["Weight check: total is 95.0% (expected 100%)."],
    )

    generator._post_update_checks(proposal, "show proposal")

    assert not any(str(note).lower().startswith("weight check:") for note in (proposal.notes or []))


def test_post_update_checks_adds_weight_check_when_total_is_not_100():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            _make_category("Assignments", 10.0),
            _make_category("Labs", 15.0),
            _make_category("Midterm", 30.0),
            _make_category("Final Exam", 30.0),
            _make_category("Quizzes", 10.0),
        ],
        aggregation_method=10,
        notes=[],
    )

    generator._post_update_checks(proposal, "show proposal")

    assert any("Weight check: total is 95.0%" in str(note) for note in (proposal.notes or []))


def test_weight_validation_mean_with_extra_credit_enforces_non_extra_total():
    proposal = GradebookProposal(
        categories=[
            _make_category("Assignments", 100.0),
            _make_category("Bonus", 20.0, extra_credit=True),
        ],
        aggregation_method=12,
    )
    assert validate_proposal_weights(proposal) == []

    proposal.categories[0].weight = 80.0
    errors = validate_proposal_weights(proposal)
    assert errors
    assert errors[0]["aggregation_method"] == 12


def test_weight_validation_mean_and_natural_not_enforced():
    for method in (0, 13):
        proposal = GradebookProposal(
            categories=[
                _make_category("Assignments", 99.0),
                _make_category("Final Exam", 1.0),
            ],
            aggregation_method=method,
        )
        assert validate_proposal_weights(proposal) == []


def test_weight_validation_ignores_subcategory_split_consistency_rules():
    proposal = GradebookProposal(
        categories=[
            _make_category(
                "Assignments",
                50.0,
                subcategories=[
                    _make_subcategory("Homework", 60.0),
                    _make_subcategory("Projects", 40.0),
                ],
            ),
            _make_category("Final Exam", 50.0),
        ],
        aggregation_method=10,
    )
    assert validate_proposal_weights(proposal) == []


@pytest.mark.asyncio
async def test_gradebook_proposal_updates_weights_and_splits():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(
        base_proposal,
        "Set Assignments to 40%, Labs to 10%, split assignments into Homework, Projects",
    )

    normalized = {cat.name: cat for cat in updated.categories}
    assert "Assignments" in normalized
    assert normalized["Labs"].weight == 10.0
    assert normalized["Assignments"].weight == 40.0
    assert "Homework" not in normalized
    assert "Projects" not in normalized
    assert any("internal allocation" in note.lower() for note in updated.notes)


@pytest.mark.asyncio
async def test_gradebook_proposal_does_not_create_free_text_categories():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(
        base_proposal,
        "Create a gradebook with Assignments 40%, Labs 10%, Midterm 20%, Final Exam 30%",
    )

    names = {cat.name for cat in updated.categories}
    assert names == {"Assignments", "Labs", "Midterm", "Final Exam"}
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_gradebook_proposal_typo_adjustments_match_existing_categories():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(
        base_proposal,
        "change lap to 15% and final to 25%",
    )

    normalized = {cat.name: cat for cat in updated.categories}
    assert normalized["Labs"].weight == 15.0
    assert normalized["Final Exam"].weight == 25.0
    assert "Lap" not in normalized


@pytest.mark.asyncio
async def test_gradebook_proposal_supports_no_percent_imperative_weight():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(base_proposal, "make labs 20")

    normalized = {cat.name: cat for cat in updated.categories}
    assert normalized["Labs"].weight == 20.0
    assert updated.aggregation_method == 13


@pytest.mark.asyncio
async def test_gradebook_proposal_supports_no_percent_multi_updates():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(base_proposal, "make midterm 24 and final 36")

    normalized = {cat.name: cat for cat in updated.categories}
    assert normalized["Midterm"].weight == 24.0
    assert normalized["Final Exam"].weight == 36.0


def test_remove_nonexistent_category_adds_note():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])
    updated = generator.update_from_prompt(base, "remove GhostCategory")
    assert any("not found" in n.lower() for n in (updated.notes or []))
    assert all(cat.name != "GhostCategory" for cat in updated.categories)


def test_remove_nonexistent_category_with_punctuation_adds_note():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])
    updated = generator.update_from_prompt(
        base,
        "Remove a category that does not exist: drop GhostCategory.",
    )
    notes = [str(n).lower() for n in (updated.notes or [])]
    assert any("not found" in n and "ghostcategory" in n for n in notes)
    assert all(cat.name != "GhostCategory" for cat in updated.categories)


def test_split_missing_category_adds_not_found_effect():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignment", weight=20.0, items=["Homework 1", "Homework 2"]),
            GradebookCategory(name="Quizzes", weight=15.0, items=["Midterm Quiz", "Final Quiz"]),
            GradebookCategory(name="Midterm", weight=30.0, items=["Midterm exam"]),
            GradebookCategory(name="Final Exam", weight=35.0, items=["Final exam"]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(
        proposal,
        "In Labs, keep total 15% and split into Lab Reports 10% and In-lab Work 5%.",
    )

    assert all(cat.name.lower() != "labs" for cat in updated.categories)
    effects = [str(n) for n in (updated.notes or []) if str(n).startswith("Effect:")]
    assert any("not found" in e.lower() and "labs" in e.lower() for e in effects)
    assert not any("split into" in e.lower() and "assignment" in e.lower() for e in effects)
    assert not any(cat.subcategories for cat in updated.categories)


def test_guard_ignores_syllabus_prose_in_proposal_request_prompt():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        (
            "In Teaching and Learning, the number of references needed, "
            "in particular, five references are required. okay use them and give me a proposal"
        ),
    )

    not_found_effects = [
        str(n)
        for n in (updated.notes or [])
        if str(n).startswith("Effect:") and "not found" in str(n).lower()
    ]
    assert not_found_effects == []


def test_stale_category_not_found_effects_cleared_on_weight_only_prompt():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    polluted = GradebookProposal.parse_obj(base.model_dump())
    polluted.notes = [
        (
            "Effect: Tried to modify 'Teaching and Learning', but category was not found. "
            "No changes made."
        ),
        (
            "Effect: Tried to modify 'the number of references needed', but category was not found. "
            "No changes made."
        ),
    ]

    updated = generator.update_from_prompt(polluted, "set midterm to 15 and assignment to 25")

    not_found_effects = [
        str(n)
        for n in (updated.notes or [])
        if str(n).startswith("Effect:") and "not found" in str(n).lower()
    ]
    assert not_found_effects == []
    assert any("midterm" in str(n).lower() for n in (updated.notes or []))


def test_split_overwrites_previous_subcategories_and_notes():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
    ])
    # First split
    updated1 = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")
    assert len(updated1.categories[0].subcategories) == 2
    # Overwrite split
    updated2 = generator.update_from_prompt(
        updated1,
        "Keep top-level weights unchanged. In Assignments (25%), split into Homework 5%, Projects 10%, Reflection 10%.",
    )
    subs = updated2.categories[0].subcategories
    names = [s.name for s in subs]
    weights = [s.weight for s in subs]
    assert names == ["Homework", "Projects", "Reflection"]
    assert weights == [5.0, 10.0, 10.0]
    # Only one effect/note for split
    split_notes = [n for n in (updated2.notes or []) if "split into" in n]
    assert len(split_notes) == 1


def test_effect_topic_override_keeps_latest_split_for_same_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    step1 = generator.update_from_prompt(base, "In Labs, split into Lab Reports 10% and In-lab Work 5%")
    step2 = generator.update_from_prompt(step1, "In Labs, divide it into: Lab Reports, In-Lab and Pre-Lab and assign 5% for each")

    effects = [n for n in (step2.notes or []) if str(n).startswith("Effect:")]
    split_effects = [e for e in effects if "split into" in e.lower() and "labs" in e.lower()]

    assert len(split_effects) == 1
    assert "Lab Reports 5.0%" in split_effects[0]
    assert "Pre-Lab 5.0%" in split_effects[0]


def test_split_supports_with_weight_of_fractional_values():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Quiz 1", module="quiz"),
        CourseActivity(name="Quiz 2", module="quiz"),
    ])
    base = generator.update_from_prompt(base, "Set Quizzes to 25%")

    updated = generator.update_from_prompt(
        base,
        "split quizzes into midterm quiz with weight of 0.4 and final quiz with weight of 0.6",
    )

    quizzes = next(cat for cat in updated.categories if cat.name == "Quizzes")
    assert len(quizzes.subcategories) == 2
    assert quizzes.subcategories[0].name == "Midterm Quiz"
    assert quizzes.subcategories[1].name == "Final Quiz"
    assert quizzes.subcategories[0].weight == pytest.approx(10.0)
    assert quizzes.subcategories[1].weight == pytest.approx(15.0)
    assert not any("internal split totals" in str(n).lower() for n in (updated.notes or []))


def test_drop_subcategory_removes_split_piece_and_redistributes_weight():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "drop Projects")

    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    sub_names = [sub.name for sub in (assignments.subcategories or [])]
    assert sub_names == ["Homework"]
    assert assignments.subcategories[0].weight == pytest.approx(25.0)
    assert any("removed subcategory 'projects'" in str(n).lower() for n in (updated.notes or []))


def test_rename_subcategory_updates_split_label():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "rename Projects to Project")

    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    sub_names = [sub.name for sub in (assignments.subcategories or [])]
    assert "Project" in sub_names
    assert "Projects" not in sub_names
    assert any("renamed subcategory 'projects' to 'project'" in str(n).lower() for n in (updated.notes or []))


def test_add_subcategory_appends_without_overwriting_existing_split():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "In Assignments, add subcategory Reflection 5%")

    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    sub_names = [sub.name for sub in (assignments.subcategories or [])]
    assert sub_names == ["Homework", "Projects", "Reflection"]
    assert any("added subcategory 'reflection'" in str(n).lower() for n in (updated.notes or []))


def test_drop_subcategory_does_not_emit_top_level_category_not_found():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "drop Projects")

    effects = [str(n) for n in (updated.notes or []) if str(n).startswith("Effect:")]
    assert not any(
        "category was not found" in effect.lower() and "projects" in effect.lower()
        for effect in effects
    )


def test_drop_missing_subcategory_reports_subcategory_not_found():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "drop GhostSubcategory")

    effects = [str(n) for n in (updated.notes or []) if str(n).startswith("Effect:")]
    assert any(
        "subcategory" in effect.lower()
        and "ghostsubcategory" in effect.lower()
        and "was not found" in effect.lower()
        for effect in effects
    )


def test_remove_subcategory_scoped_from_parent_phrase():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Labs, split into Lab Reports 10%, In-Lab Work 5%")

    updated = generator.update_from_prompt(split, "remove In-Lab Work from Labs")

    labs = next(cat for cat in updated.categories if cat.name == "Labs")
    sub_names = [sub.name for sub in (labs.subcategories or [])]
    assert sub_names == ["Lab Reports"]
    assert labs.subcategories[0].weight == pytest.approx(15.0)


def test_rename_subcategory_scoped_in_parent_phrase():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")

    updated = generator.update_from_prompt(split, "In Assignments, rename Projects to Project")

    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    sub_names = [sub.name for sub in (assignments.subcategories or [])]
    assert sub_names == ["Homework", "Project"]


def test_subcategory_weight_scoped_in_parent_does_not_change_parent_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Labs, split into Lab Reports 10%, In-lab Work 5%")
    labs = next(cat for cat in split.categories if cat.name == "Labs")
    assert labs.weight == pytest.approx(15.0)

    updated = generator.update_from_prompt(split, "set weight of Lab Reports in Labs to 5%")
    labs = next(cat for cat in updated.categories if cat.name == "Labs")
    lab_reports = next(sub for sub in labs.subcategories if sub.name == "Lab Reports")

    assert labs.weight == pytest.approx(15.0)
    assert lab_reports.weight == pytest.approx(5.0)
    effects = [str(n) for n in (updated.notes or []) if str(n).startswith("Effect:")]
    assert any("Lab Reports split weight in Labs to 5.0%" in effect for effect in effects)
    assert not any("Set Labs to 5.0%" in effect for effect in effects)


def test_subcategory_weight_explicit_keyword_without_parent():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Labs, split into Lab Reports 10%, In-lab Work 5%")

    updated = generator.update_from_prompt(split, "set Lab Reports subcategory weight to 5%")
    labs = next(cat for cat in updated.categories if cat.name == "Labs")
    lab_reports = next(sub for sub in labs.subcategories if sub.name == "Lab Reports")

    assert labs.weight == pytest.approx(15.0)
    assert lab_reports.weight == pytest.approx(5.0)


def test_rename_subcategory_does_not_rename_matching_grade_item():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign", cmid=10),
    ])
    split = generator.update_from_prompt(
        base,
        "In Assignments, split into Homework 10%, Projects 15%",
    )
    assignments = next(cat for cat in split.categories if cat.name == "Assignments")
    assert "Homework 1" in (assignments.items or [])

    updated = generator.update_from_prompt(split, "rename Homework to Homework Tasks")
    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    sub_names = [sub.name for sub in (assignments.subcategories or [])]

    assert "Homework Tasks" in sub_names
    assert "Homework 1" in (assignments.items or [])
    assert "Homework Tasks" not in (assignments.items or [])


def test_rename_subcategory_when_name_overlaps_parent_category_tokens():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Labs, split into Lab Reports 10%, In-Lab Work 5%")

    updated = generator.update_from_prompt(split, "rename Lab Reports to Lab Report")

    labs = next(cat for cat in updated.categories if cat.name == "Labs")
    sub_names = [sub.name.lower() for sub in (labs.subcategories or [])]
    assert "lab report" in sub_names
    assert "lab reports" not in sub_names


def test_set_subcategory_weight_matches_fuzzy_label():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    split = generator.update_from_prompt(base, "In Assignments, split into Homework 10%, Projects 15%")
    renamed = generator.update_from_prompt(split, "rename Projects to Project")

    updated = generator.update_from_prompt(renamed, "set Project weight to 12%")

    assignments = next(cat for cat in updated.categories if cat.name == "Assignments")
    weights = {sub.name: sub.weight for sub in (assignments.subcategories or [])}
    assert weights["Project"] == pytest.approx(12.0)


def test_split_follow_up_assign_updates_subcategory_weight_and_clears_stale_warning():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Quiz 1", module="quiz"),
    ])
    base = generator.update_from_prompt(base, "Set Quizzes to 25%")

    with_even_split = generator.update_from_prompt(
        base,
        "split quizzes into Midterm Quiz and Final Quiz",
    )
    # Default: even distribution — 2 subs each get 25%/2 = 12.5%; no warning.
    quizzes_after_split = next(cat for cat in with_even_split.categories if cat.name == "Quizzes")
    assert quizzes_after_split.subcategories[0].weight == pytest.approx(12.5)
    assert quizzes_after_split.subcategories[1].weight == pytest.approx(12.5)
    assert not any("internal split totals" in str(n).lower() for n in (with_even_split.notes or []))

    updated = generator.update_from_prompt(with_even_split, "assign 0.4 to Midterm Quiz")

    quizzes = next(cat for cat in updated.categories if cat.name == "Quizzes")
    weights = {sub.name: sub.weight for sub in quizzes.subcategories}
    assert weights["Midterm Quiz"] == pytest.approx(10.0)
    assert weights["Final Quiz"] == pytest.approx(12.5)  # unchanged from even split

    # Warning appears because 10.0 + 12.5 = 22.5 ≠ 25 (recomputed, not duplicated).
    split_warnings = [n for n in (updated.notes or []) if "internal split totals" in str(n).lower()]
    assert len(split_warnings) == 1
    assert "22.5%" in str(split_warnings[0])


def test_formula_effect_topic_override_handles_quoted_category_labels():
    generator = ProposalGenerator()
    proposal = generator.generate_initial([])

    # Simulate historical quoted stored effect text format.
    generator._append_effect_note(proposal, "Stored formula for 'Labs' (unresolved refs: [lab1])")
    generator._append_effect_note(proposal, "Cleared formula from Labs")

    effects = [n for n in (proposal.notes or []) if str(n).startswith("Effect:")]
    labs_effects = [e for e in effects if "formula" in e.lower() and "labs" in e.lower()]
    assert len(labs_effects) == 1
    assert "Cleared formula from Labs" in labs_effects[0]


@pytest.mark.asyncio
async def test_formula_override_flag_requires_explicit_prompt_intent():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Lab Report 1", module="assign"),
    ])

    updated = generator.update_from_prompt(
        base,
        "Set Final Exam as =([[midterm]]*0.4)+([[final]]*0.6)",
        course_activities=[CourseActivity(name="Lab Report 1", module="assign")],
    )
    final_exam = next(cat for cat in updated.categories if cat.name == "Final Exam")
    assert final_exam.calculation_formula is not None
    assert final_exam.formula_override is False

    overridden = generator.update_from_prompt(
        updated,
        "Override formula for Final Exam and set Final Exam as =([[midterm]]*0.3)+([[final]]*0.7)",
        course_activities=[CourseActivity(name="Lab Report 1", module="assign")],
    )
    final_exam_override = next(cat for cat in overridden.categories if cat.name == "Final Exam")
    assert final_exam_override.calculation_formula is not None
    assert final_exam_override.formula_override is True


@pytest.mark.asyncio
async def test_gradebook_proposal_regex_alias_phrases_map_to_existing_categories():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(
        base_proposal,
        "adjust the laboratory component to 18% and final exam weight to 22%",
    )

    normalized = {cat.name: cat for cat in updated.categories}
    assert normalized["Labs"].weight == 18.0
    assert normalized["Final Exam"].weight == 22.0


@pytest.mark.asyncio
async def test_gradebook_proposal_remove_category_frees_weight_by_default():
    """Mode 1: bare remove/drop frees weight; total decreases."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    # Assignments starts at 25%
    assignments_weight = next(c.weight for c in base.categories if c.name == "Assignments")

    updated = generator.update_from_prompt(base, "remove assignments")

    names = {cat.name for cat in updated.categories}
    assert "Assignments" not in names
    expected_total = sum(c.weight for c in base.categories) - assignments_weight
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(expected_total)
    assert any("freed" in n.lower() for n in (updated.notes or []))


@pytest.mark.asyncio
async def test_gradebook_proposal_remove_category_distributes_evenly():
    """Mode 2: 'remove X evenly' redistributes freed weight to remaining categories."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    assignments_weight = next(c.weight for c in base.categories if c.name == "Assignments")
    original_total = sum(c.weight for c in base.categories)

    updated = generator.update_from_prompt(base, "remove assignments evenly")

    names = {cat.name for cat in updated.categories}
    assert "Assignments" not in names
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(original_total)
    assert any("evenly" in n.lower() for n in (updated.notes or []))


@pytest.mark.asyncio
async def test_gradebook_proposal_remove_category_assigns_weight_to_target():
    """Mode 3: 'remove X and give to Y' transfers freed weight to Y."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    assignments_weight = next(c.weight for c in base.categories if c.name == "Assignments")
    labs_weight_before = next(c.weight for c in base.categories if c.name == "Labs")

    updated = generator.update_from_prompt(base, "remove assignments and give weight to labs")

    names = {cat.name: cat for cat in updated.categories}
    assert "Assignments" not in names
    assert names["Labs"].weight == pytest.approx(labs_weight_before + assignments_weight)
    assert any("labs" in n.lower() for n in (updated.notes or []))


@pytest.mark.asyncio
async def test_gradebook_proposal_remove_and_assign_to_multiple_targets():
    """Mode 3: weight split evenly across two specified targets."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    assignments_weight = next(c.weight for c in base.categories if c.name == "Assignments")
    labs_before = next(c.weight for c in base.categories if c.name == "Labs")
    midterm_before = next(c.weight for c in base.categories if c.name == "Midterm")

    updated = generator.update_from_prompt(
        base,
        "remove assignments and give weight to labs and midterm",
    )

    names = {cat.name: cat for cat in updated.categories}
    assert "Assignments" not in names
    share = assignments_weight / 2
    assert names["Labs"].weight == pytest.approx(labs_before + share)
    assert names["Midterm"].weight == pytest.approx(midterm_before + share)


@pytest.mark.asyncio
async def test_gradebook_proposal_remove_category_rebalances_weights():
    """Legacy test kept: bare remove still works (mode 1 — frees weight)."""
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])
    assignments_weight = next(c.weight for c in base_proposal.categories if c.name == "Assignments")
    original_total = sum(c.weight for c in base_proposal.categories)

    updated = generator.update_from_prompt(base_proposal, "remove assignments")

    names = {cat.name for cat in updated.categories}
    assert "Assignments" not in names
    # Mode 1: weight freed, total decreases
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(original_total - assignments_weight)


@pytest.mark.asyncio
async def test_gradebook_chat_updates_proposal_from_prompt():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_b",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Lab 1", module="lab"),
        ],
    )
    assert session.phase == "ANALYSIS"
    assert session.proposal is not None

    chat = await engine.chat(session.session_id, "Please make Assignments 40% and split assignments into Homework, Projects")
    assert chat.proposal is not None
    normalized = {cat.name: cat for cat in chat.proposal.categories}
    assert normalized["Assignments"].weight == 40.0
    assert "Homework" not in normalized
    assert "Projects" not in normalized
    assert any("internal allocation" in note.lower() for note in (chat.proposal.notes or []))


@pytest.mark.asyncio
async def test_gradebook_chat_help_prompt_does_not_mutate_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1001",
        professor_id="prof_help",
        bot_name="eecs-help-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    session.phase = "REFINEMENT"
    await engine._save_session(session)

    before = session.proposal.model_dump() if session.proposal else None
    updated = await engine.chat(session.session_id, "help")

    assert updated.proposal is not None
    assert updated.proposal.model_dump() == before
    assert bool((updated.extraction or {}).get("proposal_changed")) is False


@pytest.mark.asyncio
async def test_gradebook_chat_question_prompt_does_not_mutate_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1002",
        professor_id="prof_q",
        bot_name="eecs-q-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    session.phase = "REFINEMENT"
    await engine._save_session(session)

    before = session.proposal.model_dump() if session.proposal else None
    updated = await engine.chat(session.session_id, "what aggregation method we support?")

    assert updated.proposal is not None
    assert updated.proposal.model_dump() == before
    assert bool((updated.extraction or {}).get("proposal_changed")) is False


@pytest.mark.asyncio
async def test_gradebook_chat_unsupported_prompt_does_not_mutate_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1003",
        professor_id="prof_offtopic",
        bot_name="eecs-offtopic-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    session.phase = "REFINEMENT"
    await engine._save_session(session)

    before = session.proposal.model_dump() if session.proposal else None
    updated = await engine.chat(session.session_id, "fafavc")

    assert updated.proposal is not None
    assert updated.proposal.model_dump() == before
    assert bool((updated.extraction or {}).get("proposal_changed")) is False


@pytest.mark.asyncio
async def test_gradebook_chat_unknown_session_raises_key_error():
    engine = GradebookSessionEngine()
    with pytest.raises(KeyError):
        await engine.chat("missing-session", "hello")


@pytest.mark.asyncio
async def test_gradebook_chat_keeps_intake_for_non_informative_prompt():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[],
        course_activities=[],
    )
    assert session.phase == "INTAKE"

    updated = await engine.chat(session.session_id, "s")
    assert updated.phase == "INTAKE"
    assert updated.proposal is None


@pytest.mark.asyncio
async def test_gradebook_proposal_supports_env_alias_json(monkeypatch):
    monkeypatch.setenv(
        "GRADEBOOK_CATEGORY_ALIASES_JSON",
        json.dumps({"laboratory component": "Labs", "summative exam": "Final Exam"}),
    )
    monkeypatch.setenv(
        "GRADEBOOK_CATEGORY_ALIAS_REGEX_JSON",
        json.dumps({"Assignments": r"\b(coursework|cw)\b"}),
    )
    generator = ProposalGenerator()

    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
    ])
    updated = generator.update_from_prompt(
        base,
        "set laboratory component to 18% and summative exam to 22% and coursework to 60%",
    )
    normalized = {cat.name: cat for cat in updated.categories}
    assert normalized["Labs"].weight == 18.0
    assert normalized["Final Exam"].weight == 22.0
    assert normalized["Assignments"].weight == 60.0


def test_notes_do_not_accumulate_across_turns():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 10

    p1 = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes_p1 = [n for n in p1.notes if n.lower().startswith("weight check:")]
    assert len(weight_notes_p1) == 1, "First update should add exactly one weight-check note"

    p2 = generator.update_from_prompt(p1, "set Assignments to 35%")
    weight_notes_p2 = [n for n in p2.notes if n.lower().startswith("weight check:")]
    assert len(weight_notes_p2) == 1, "Second update must not accumulate old weight-check notes"


def test_weight_warning_for_weighted_mean_when_total_not_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 10

    updated = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert len(weight_notes) == 1


def test_no_weight_warning_for_weighted_mean_when_total_is_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 10

    updated = generator.update_from_prompt(
        base,
        "set Assignments to 25%, Labs to 15%, Midterm to 30%, Final Exam to 30%",
    )
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert not weight_notes


def test_no_weight_warning_for_natural_method_when_total_not_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 13

    updated = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert not weight_notes


def test_weight_warning_for_simple_weighted_mean_when_total_not_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 11

    updated = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert len(weight_notes) == 1


def test_no_weight_warning_for_mean_with_extra_credits_when_non_extra_total_is_100():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            _make_category("Assignments", 100.0),
            _make_category("Bonus", 20.0, extra_credit=True),
        ],
        aggregation_method=12,
    )

    updated = generator.update_from_prompt(base, "show proposal")
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert not weight_notes


def test_weight_warning_for_mean_with_extra_credits_when_non_extra_total_not_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base.aggregation_method = 12

    updated = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes = [n for n in (updated.notes or []) if str(n).lower().startswith("weight check:")]
    assert len(weight_notes) == 1


def test_conversation_zero_weight_category_is_not_marked_invalid():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=100.0),
            GradebookCategory(name="Quizzes", weight=0.0),
        ],
        aggregation_method=13,
    )
    issues = cm.validate_weights(proposal)
    assert not any("invalid weight" in issue.lower() for issue in issues)


def test_add_category_without_explicit_weight_defaults_to_zero():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    step = generator.update_from_prompt(base, "added quizzes")

    by_name = {cat.name: cat for cat in step.categories}
    assert "Quizzes" in by_name
    assert abs(by_name["Quizzes"].weight - 0.0) < 1e-6


def test_add_manual_grade_item_from_chat_prompt():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=100.0, items=[]),
        ]
    )

    updated = generator.update_from_prompt(proposal, "add grade item Final to Final Exam")

    assert updated.categories[0].items == ["Final"]
    assert any("manual grade item" in note.lower() for note in (updated.notes or []))


def _gradebook_chat_baseline_proposal() -> GradebookProposal:
    return GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["HW 2"],
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0, items=["HW 1"]),
                    GradebookSubcategory(name="Project", weight=15.0, items=["Term Project"]),
                ],
            ),
            GradebookCategory(
                name="Labs",
                weight=15.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(name="In-lab", weight=5.0, items=["Week 1 Lab"]),
                    GradebookSubcategory(name="Lab-report", weight=10.0, items=[]),
                ],
            ),
            GradebookCategory(name="Midterm", weight=30.0, items=["Midterm Exam"]),
            GradebookCategory(name="Final", weight=30.0, items=["Final Exam"], hidden=True),
        ]
    )


def test_add_test_creates_test_category_not_quizzes_alias():
    generator = ProposalGenerator()
    updated = generator.update_from_prompt(_gradebook_chat_baseline_proposal(), "add test")

    assert [category.name for category in updated.categories] == [
        "Assignments",
        "Labs",
        "Midterm",
        "Final",
        "Test",
    ]


def test_add_grade_item_without_category_creates_item_not_category():
    generator = ProposalGenerator()
    updated = generator.update_from_prompt(_gradebook_chat_baseline_proposal(), "add grade item kazem")

    assert [category.name for category in updated.categories] == [
        "Assignments",
        "Labs",
        "Midterm",
        "Final",
    ]
    assert "kazem" in (updated.categories[0].items or [])
    assert any("manual grade item 'kazem'" in note.lower() for note in (updated.notes or []))


def test_move_chat_created_grade_item_to_subcategory():
    generator = ProposalGenerator()
    proposal = generator.update_from_prompt(_gradebook_chat_baseline_proposal(), "add grade item kazem")
    updated = generator.update_from_prompt(proposal, "move grade item kazem to Lab-report in Labs")

    labs = next(category for category in updated.categories if category.name == "Labs")
    lab_report = next(sub for sub in labs.subcategories if sub.name == "Lab-report")
    assert "kazem" in (lab_report.items or [])
    assert "kazem" not in (labs.items or [])
    assert "kazem" not in (updated.categories[0].items or [])


# --- Aggregation Method Tests ---

def test_gradebook_proposal_default_aggregation_is_natural():
    proposal = GradebookProposal()
    assert proposal.aggregation_method == 13


def test_gradebook_proposal_stores_aggregation_method():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0),
            GradebookCategory(name="Final Exam", weight=75.0),
        ],
        aggregation_method=10,
    )
    assert proposal.aggregation_method == 10
    dumped = proposal.model_dump()
    assert dumped["aggregation_method"] == 10


def test_sync_proposal_items_from_mapping_adds_manual_items_to_categories():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Labs", weight=15.0, items=[]),
            GradebookCategory(name="Midterm", weight=30.0, items=[]),
            GradebookCategory(name="Final Exam", weight=30.0, items=[]),
        ]
    )
    mapping = [
        {"category": "Labs", "activity_name": "Labs Manual Item", "grade_item_id": 101, "itemtype": "manual"},
        {"category": "Midterm", "activity_name": "Midterm Manual Item", "grade_item_id": 102, "itemtype": "manual"},
        {"category": "Final Exam", "activity_name": "Final Exam Manual Item", "grade_item_id": 103, "itemtype": "manual"},
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is True
    assert proposal.categories[0].items == ["Labs Manual Item"]
    assert proposal.categories[1].items == ["Midterm Manual Item"]
    assert proposal.categories[2].items == ["Final Exam Manual Item"]


def test_sync_mapping_from_proposal_manual_items_skips_existing_mod_activities():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["The Role of Balance in Layout Design", "exam"],
            ),
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 63,
                "grade_item_id": 13,
                "activity_name": "The Role of Balance in Layout Design",
                "itemtype": "mod",
                "item_source": "activity",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
            }
        ]
    }
    course_activities = [
        CourseActivity(name="The Role of Balance in Layout Design", module="assign", cmid=63, grade_item_id=13),
    ]

    changed = sync_mapping_from_proposal_manual_items(proposal, content_mapping, course_activities)

    assert changed is True
    names = [row["activity_name"] for row in content_mapping["graded_activities"]]
    assert names.count("The Role of Balance in Layout Design") == 1
    assert "exam" in names
    assert sum(1 for row in content_mapping["graded_activities"] if row.get("item_source") == "proposal_manual") == 1


def test_sync_mapping_from_proposal_manual_items_prunes_stale_proposal_duplicates():
    proposal = GradebookProposal(
        categories=[GradebookCategory(name="Quizzes", weight=15.0, items=["quiz 1", "exam"])]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 1148,
                "grade_item_id": 336,
                "activity_name": "quiz 1",
                "itemtype": "mod",
                "item_source": "activity",
                "suggested_category": "Quizzes",
                "confirmed_category": "Quizzes",
            },
            {
                "activity_name": "quiz 1",
                "itemtype": "manual",
                "item_source": "proposal_manual",
                "suggested_category": "Quizzes",
                "confirmed_category": "Quizzes",
            },
        ]
    }

    changed = sync_mapping_from_proposal_manual_items(proposal, content_mapping, [])

    assert changed is True
    quiz_rows = [row for row in content_mapping["graded_activities"] if row.get("activity_name") == "quiz 1"]
    assert len(quiz_rows) == 1
    assert quiz_rows[0].get("itemtype") == "mod"
    assert any(row.get("activity_name") == "exam" for row in content_mapping["graded_activities"])


def test_sync_mapping_from_proposal_manual_items_adds_rows():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Midterm", weight=15.0, items=["exam"]),
            GradebookCategory(name="Final Exam", weight=30.0, items=["final"]),
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 10,
                "grade_item_id": 13,
                "activity_name": "quiz 1",
                "itemtype": "mod",
                "item_source": "activity",
                "suggested_category": "Quizzes",
                "confirmed_category": "Quizzes",
            }
        ]
    }

    changed = sync_mapping_from_proposal_manual_items(proposal, content_mapping, [])

    assert changed is True
    names = {row["activity_name"] for row in content_mapping["graded_activities"]}
    assert "exam" in names
    assert "final" in names
    manual_rows = [
        row for row in content_mapping["graded_activities"]
        if row.get("item_source") == "proposal_manual"
    ]
    assert len(manual_rows) == 2


def test_sync_proposal_items_from_mapping_is_idempotent():
    proposal = GradebookProposal(
        categories=[GradebookCategory(name="Labs", weight=15.0, items=["Labs Manual Item"])]
    )
    mapping = [
        {"category": "Labs", "activity_name": "Labs Manual Item", "grade_item_id": 101, "itemtype": "manual"},
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is False
    assert proposal.categories[0].items == ["Labs Manual Item"]


def test_sync_proposal_items_from_mapping_places_items_in_subcategories():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                items=["assignment1", "assignment2", "HW essay"],
                subcategories=[
                    GradebookSubcategory(name="Projects", weight=15.0),
                    GradebookSubcategory(name="Homework", weight=10.0),
                ],
            ),
        ]
    )
    mapping = [
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment1",
            "itemtype": "mod",
        },
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment2",
            "itemtype": "mod",
        },
        {
            "category": "Assignments",
            "confirmed_subcategory": "Homework",
            "activity_name": "HW essay",
            "itemtype": "mod",
        },
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is True
    projects, homework = proposal.categories[0].subcategories
    assert projects.items == ["assignment1", "assignment2"]
    assert homework.items == ["HW essay"]
    assert proposal.categories[0].items == []


def test_sync_proposal_items_from_mapping_moves_parent_item_into_subcategory():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Labs",
                weight=15.0,
                items=["Labs Manual Item"],
                subcategories=[GradebookSubcategory(name="Lab Reports", weight=10.0)],
            ),
        ]
    )
    mapping = [
        {
            "category": "Labs",
            "confirmed_subcategory": "Lab Reports",
            "activity_name": "Labs Manual Item",
            "grade_item_id": 101,
            "itemtype": "manual",
        },
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is True
    assert proposal.categories[0].items == []
    assert proposal.categories[0].subcategories[0].items == ["Labs Manual Item"]


def test_format_categories_nests_mapping_subcategory_items_without_arrow():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                items=["assignment1", "The Role of Balance in Layout Design"],
                subcategories=[
                    GradebookSubcategory(name="Projects", weight=15.0),
                    GradebookSubcategory(name="Homework", weight=10.0),
                ],
            ),
        ]
    )
    session = GradebookSessionRecord(
        session_id="mapping-sub-tree",
        course_id="EECS-1000",
        professor_id="prof",
        bot_name="eecs-bot",
        phase="ACCEPTED",
        proposal=proposal,
        content_mapping={
            "graded_activities": [
                {
                    "activity_name": "assignment1",
                    "confirmed_category": "Assignments",
                    "confirmed_subcategory": "Projects",
                    "itemtype": "mod",
                },
                {
                    "activity_name": "The Role of Balance in Layout Design",
                    "confirmed_category": "Assignments",
                    "confirmed_subcategory": "Homework",
                    "itemtype": "mod",
                },
            ]
        },
        course_activities=[
            CourseActivity(name="assignment1", module="assign", cmid=1, grade_item_id=1),
            CourseActivity(
                name="The Role of Balance in Layout Design",
                module="assign",
                cmid=2,
                grade_item_id=2,
            ),
        ],
    )

    text = cm._format_categories(proposal, session)

    assert "→ Projects" not in text
    assert "→ Homework" not in text
    assert "[Assignments > Projects] assignment1" in text
    assert "[Assignments > Homework] The Role of Balance in Layout Design" in text
    assert text.count("assignment1") == 1
    assert text.count("The Role of Balance in Layout Design") == 1


def test_chat_move_then_mapping_subcategory_does_not_duplicate_proposal_tree():
    """Items moved into a subcategory via chat should stay put when mapping confirms the same sub."""
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(
                        name="Projects",
                        weight=15.0,
                        items=["assignment1", "assignment2"],
                    ),
                    GradebookSubcategory(name="Homework", weight=10.0),
                ],
            ),
        ]
    )
    mapping = [
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment1",
            "itemtype": "mod",
        },
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment2",
            "itemtype": "mod",
        },
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is False
    projects = proposal.categories[0].subcategories[0]
    assert projects.items == ["assignment1", "assignment2"]
    assert proposal.categories[0].items == []

    session = GradebookSessionRecord(
        session_id="chat-then-mapping",
        course_id="EECS-1000",
        professor_id="prof",
        bot_name="eecs-bot",
        phase="ACCEPTED",
        proposal=proposal,
        content_mapping={"graded_activities": mapping},
        course_activities=[
            CourseActivity(name="assignment1", module="assign", cmid=1, grade_item_id=1),
            CourseActivity(name="assignment2", module="assign", cmid=2, grade_item_id=2),
        ],
    )
    text = cm._format_categories(proposal, session)

    assert text.count("assignment1") == 1
    assert text.count("assignment2") == 1
    assert "→ Projects" not in text
    assert "[Assignments > Projects] assignment1" in text
    assert "[Assignments > Projects] assignment2" in text


def test_chat_move_then_mapping_category_only_preserves_subcategory_placement():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(
                        name="Projects",
                        weight=15.0,
                        items=["assignment1"],
                    ),
                ],
            ),
        ]
    )
    mapping = [
        {
            "category": "Assignments",
            "activity_name": "assignment1",
            "itemtype": "mod",
        },
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is False
    assert proposal.categories[0].subcategories[0].items == ["assignment1"]
    assert proposal.categories[0].items == []


def test_chat_move_then_mapping_clears_stale_parent_duplicate():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                items=["assignment1", "assignment2"],
                subcategories=[
                    GradebookSubcategory(
                        name="Projects",
                        weight=15.0,
                        items=["assignment1", "assignment2"],
                    ),
                ],
            ),
        ]
    )
    mapping = [
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment1",
            "itemtype": "mod",
        },
        {
            "category": "Assignments",
            "confirmed_subcategory": "Projects",
            "activity_name": "assignment2",
            "itemtype": "mod",
        },
    ]

    changed = sync_proposal_items_from_mapping(proposal, mapping)

    assert changed is True
    assert proposal.categories[0].items == []
    session = GradebookSessionRecord(
        session_id="stale-parent-dup",
        course_id="EECS-1000",
        professor_id="prof",
        bot_name="eecs-bot",
        phase="ACCEPTED",
        proposal=proposal,
        content_mapping={"graded_activities": mapping},
        course_activities=[
            CourseActivity(name="assignment1", module="assign", cmid=1, grade_item_id=1),
            CourseActivity(name="assignment2", module="assign", cmid=2, grade_item_id=2),
        ],
    )
    text = cm._format_categories(proposal, session)
    assert text.count("assignment1") == 1
    assert text.count("assignment2") == 1


@pytest.mark.parametrize("text,expected", [
    ("I prefer weighted mean", 10),
    ("use weighted average", 10),
    ("Use simple weighted mean", 11),
    ("I want natural aggregation", 13),
    ("Mean of grades with extra credits", 12),
    ("use extra credits method", 12),
    ("simple mean", 0),
    ("just a random sentence", None),
])
def test_detect_aggregation_method(text, expected):
    cm = ConversationManager()
    assert cm._detect_aggregation_method(text) == expected


@pytest.mark.parametrize("code,expected_name", [
    (0, "Mean of grades"),
    (10, "Weighted mean of grades"),
    (11, "Simple weighted mean of grades"),
    (12, "Mean of grades (with extra credits)"),
    (13, "Natural"),
    (99, "Unknown method (99)"),
])
def test_get_aggregation_method_name(code, expected_name):
    cm = ConversationManager()
    assert cm._get_aggregation_method_name(code) == expected_name


@pytest.mark.asyncio
async def test_proposal_reply_includes_aggregation_method():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_d",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    # Advance to PROPOSAL phase
    session = await engine.chat(session.session_id, "give me a proposal")
    assert session.phase == "PROPOSAL"
    assert session.proposal is not None

    cm = ConversationManager()
    reply = cm.make_reply(session, session.proposal, prompt="give me a proposal")
    assert "Aggregation" in reply or "aggregation" in reply


@pytest.mark.asyncio
async def test_chat_with_aggregation_preference_updates_proposal():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_e",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    # Move to PROPOSAL phase
    session = await engine.chat(session.session_id, "give me a proposal")
    assert session.phase == "PROPOSAL"

    # User expresses aggregation preference in refinement
    cm = ConversationManager()
    detected = cm._detect_aggregation_method("I want weighted mean of grades")
    assert detected == 10

    if session.proposal:
        session.proposal.aggregation_method = detected
    assert session.proposal.aggregation_method == 10


# --- Per-category settings tests ---

def test_proposal_drop_lowest_parsed_from_prompt():
    generator = ProposalGenerator()
    base = generator.generate_initial([CourseActivity(name="Quiz 1", module="quiz")])

    updated = generator.update_from_prompt(base, "drop the lowest 2 from Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].drop_lowest == 2


def test_activity_name_matches_lab_ignores_syllabus_substring():
    from criabot.gradebook.naming_utils import activity_name_matches_lab, is_syllabus_like_name

    assert is_syllabus_like_name("syllabus.docx")
    assert not activity_name_matches_lab("syllabus.docx")
    assert activity_name_matches_lab("Lab 3 report")


def test_generate_initial_syllabus_docx_not_placed_in_labs():
    generator = ProposalGenerator()
    proposal = generator.generate_initial([
        CourseActivity(name="syllabus.docx", module="file"),
    ])
    by_name = {c.name: c for c in proposal.categories}
    assert "syllabus.docx" not in by_name["Labs"].items
    assert "syllabus.docx" not in by_name["Assignments"].items


@pytest.mark.asyncio
async def test_gradebook_start_does_not_treat_duplicate_syllabus_files_as_activities():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[
            MoodleResource(name="syllabus.docx", type="file", section="0"),
            MoodleResource(name="syllabus.docx", type="file", section="0"),
        ],
        course_activities=[],
    )

    assert session.extraction.get("has_syllabus") is True
    assert session.course_activities == []


def test_generate_initial_adds_quizzes_category_for_quiz_activities():
    generator = ProposalGenerator()

    proposal = generator.generate_initial([
        CourseActivity(name="Knowledge Check 1", module="quiz"),
        CourseActivity(name="Quiz 2", module="quiz"),
    ])

    by_name = {c.name: c for c in proposal.categories}

    assert "Quizzes" in by_name
    assert by_name["Quizzes"].weight == pytest.approx(15.0)
    assert by_name["Quizzes"].items == ["Knowledge Check 1", "Quiz 2"]


def test_resolve_final_exam_alias_prefers_existing_final_category():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=70.0, items=[]),
            GradebookCategory(name="Final", weight=30.0, items=["Final Exam"]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "set final exam to 35%")
    by_name = {c.name: c for c in updated.categories}

    assert "Final" in by_name
    assert "Final Exam" not in by_name
    assert by_name["Final"].weight == pytest.approx(35.0)


def test_proposal_drop_lowest_per_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(base, "drop the lowest 1 from Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].drop_lowest == 1
    assert by_name["Midterm"].drop_lowest == 0


def test_proposal_drop_lowest_matches_baseline_singular_category_name():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignment", weight=25.0, items=["Homework 1", "Homework 2"]),
            GradebookCategory(name="Quizzes", weight=25.0, items=["Midterm Quiz", "Final Quiz"]),
            GradebookCategory(name="Midterm", weight=25.0, items=["Midterm exam"], hidden=True),
            GradebookCategory(name="Final", weight=25.0, items=["Final exam"], hidden=True),
        ],
        notes=["Effect: Midterm hidden from students"],
        aggregation_method=10,
    )

    updated = generator.update_from_prompt(base, "For Assignments, set drop lowest to 1")
    by_name = {c.name: c for c in updated.categories}

    assert by_name["Assignment"].drop_lowest == 1
    assert any(
        str(note).strip().lower() == "effect: drop lowest 1 from assignment"
        for note in (updated.notes or [])
    )


def test_remove_grade_item_phrase_does_not_remove_category():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=30.0, items=["fina"]),
            GradebookCategory(name="Assignments", weight=70.0, items=[]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "remove fina grade item")
    by_name = {c.name: c for c in updated.categories}

    assert "Final Exam" in by_name
    assert by_name["Final Exam"].items == []
    assert any("removed 'fina' from final exam" in str(n).lower() for n in (updated.notes or []))
    assert not any("removed 'final exam'" in str(n).lower() for n in (updated.notes or []))


def test_rename_grade_item_phrase_updates_item_name():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=100.0, items=["fina"]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "rename grade item fina to final")

    assert updated.categories[0].items == ["final"]
    assert any("renamed 'fina' to 'final' in final exam" in str(n).lower() for n in (updated.notes or []))


def test_rename_grade_item_in_subcategory_updates_subcategory_item_name():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                subcategories=[
                    GradebookSubcategory(
                        name="Homework",
                        weight=10.0,
                        items=["assignment1", "assignment 2"],
                    ),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            ),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "rename assignment 2 to assignment2")

    homework = updated.categories[0].subcategories[0]
    assert homework.items == ["assignment1", "assignment2"]
    assert "assignment 2" not in (homework.items or [])
    assert any(
        "renamed 'assignment 2' to 'assignment2' in assignments" in str(n).lower()
        for n in (updated.notes or [])
    )


def test_remove_grade_item_from_subcategory_updates_subcategory_items():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                subcategories=[
                    GradebookSubcategory(
                        name="Homework",
                        weight=10.0,
                        items=["assignment1", "report_manual"],
                    ),
                ],
            ),
        ],
        notes=[],
    )

    for prompt in (
        "remove report_manual",
        "remove report_manual from Homework",
        "remove report_manual from Assignments",
    ):
        updated = generator.update_from_prompt(base.model_copy(deep=True), prompt)
        homework = updated.categories[0].subcategories[0]
        assert homework.items == ["assignment1"], prompt
        assert "report_manual" not in (homework.items or []), prompt


def test_remove_moodle_activity_is_blocked_when_course_activities_are_known():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=10.0,
                subcategories=[
                    GradebookSubcategory(
                        name="Homework",
                        weight=10.0,
                        items=["assignment1", "assignment2"],
                    ),
                ],
            ),
        ],
        notes=[],
    )
    activities = [
        CourseActivity(name="assignment1", module="assign", cmid=101),
        CourseActivity(name="assignment 2", module="assign", cmid=102),
    ]

    updated = generator.update_from_prompt(
        base,
        "remove assignment2",
        course_activities=activities,
    )

    homework = updated.categories[0].subcategories[0]
    assert homework.items == ["assignment1", "assignment2"]
    assert any("cannot remove moodle activity" in str(n).lower() for n in (updated.notes or []))


def test_set_activity_not_graded_removes_from_subcategory_tree():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(
                        name="Homework",
                        weight=10.0,
                        items=["Homework 1", "Homework 2"],
                    ),
                ],
            ),
        ],
        notes=[],
    )
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=101),
        CourseActivity(name="Homework 2", module="assign", cmid=102),
    ]

    updated = generator.update_from_prompt(
        base,
        "set Homework 1 to not graded",
        course_activities=activities,
    )

    homework = updated.categories[0].subcategories[0]
    assert homework.items == ["Homework 2"]
    assert "Homework 1" in (updated.not_graded_items or [])
    assert any("set moodle activity 'homework 1' to not graded" in str(n).lower() for n in (updated.notes or []))


def test_set_activity_not_graded_syncs_mapping_rows():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["Homework 2"],
            ),
        ],
        not_graded_items=["Homework 1"],
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
                "confirmed_subcategory": "Homework",
            },
            {
                "moodle_cmid": 102,
                "activity_name": "Homework 2",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
            },
        ]
    }

    assert sync_mapping_not_graded_items(content_mapping, proposal) is True
    row = content_mapping["graded_activities"][0]
    assert row["confirmed_category"] == NOT_GRADED_CATEGORY
    assert row.get("confirmed_subcategory", "x") == ""
    assert row.get("not_graded") is True
    assert content_mapping["graded_activities"][1]["confirmed_category"] == "Assignments"


def test_move_from_not_graded_restores_graded_category():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Labs", weight=15.0, items=[]),
        ],
        not_graded_items=["Homework 1"],
        notes=[],
    )
    activities = [CourseActivity(name="Homework 1", module="assign", cmid=101)]

    updated = generator.update_from_prompt(
        base,
        "move Homework 1 to Assignments",
        course_activities=activities,
    )

    assert updated.not_graded_items == []
    assert "Homework 1" in (updated.categories[0].items or [])
    assert any("moved 'homework 1'" in str(n).lower() for n in (updated.notes or []))


def test_sync_proposal_not_graded_from_confirmed_mapping_adds_tracked_item():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["Balance Activity"],
            ),
        ],
        not_graded_items=[],
    )
    activities = [CourseActivity(name="Balance Activity", module="assign", cmid=201)]
    confirmed_mapping = [
        {
            "activity_name": "Balance Activity",
            "category": NOT_GRADED_CATEGORY,
            "moodle_cmid": 201,
        }
    ]

    assert sync_proposal_not_graded_from_confirmed_mapping(
        proposal,
        confirmed_mapping,
        activities,
    ) is True
    assert "Balance Activity" in (proposal.not_graded_items or [])
    assert "Balance Activity" not in (proposal.categories[0].items or [])


def test_sync_confirmed_mapping_into_content_mapping_marks_not_graded():
    proposal = GradebookProposal(
        categories=[GradebookCategory(name="Assignments", weight=100.0, items=[])],
        not_graded_items=["Balance Activity"],
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 201,
                "activity_name": "Balance Activity",
                "confirmed_category": "Assignments",
                "confirmed_subcategory": "Homework",
            }
        ]
    }
    confirmed_mapping = [
        {
            "activity_name": "Balance Activity",
            "category": NOT_GRADED_CATEGORY,
            "moodle_cmid": 201,
        }
    ]

    assert sync_confirmed_mapping_into_content_mapping(
        content_mapping,
        confirmed_mapping,
        proposal,
    ) is True
    row = content_mapping["graded_activities"][0]
    assert row["confirmed_category"] == NOT_GRADED_CATEGORY
    assert row.get("confirmed_subcategory", "x") == ""
    assert row.get("not_graded") is True


def test_should_not_invalidate_content_mapping_when_marking_activity_not_graded():
    before = GradebookProposal(
        categories=[GradebookCategory(name="Assignments", weight=100.0, items=["Homework 1"])],
        not_graded_items=[],
    )
    after = GradebookProposal(
        categories=[GradebookCategory(name="Assignments", weight=100.0, items=[])],
        not_graded_items=["Homework 1"],
    )

    assert should_invalidate_content_mapping(before, after) is False


@pytest.mark.asyncio
async def test_content_mapper_build_mapping_respects_not_graded_items():
    mapper = ContentMapper(criadex=None)
    proposal = GradebookProposal(
        categories=[GradebookCategory(name="Assignments", weight=100.0, items=["Homework 2"])],
        not_graded_items=["Homework 1"],
    )
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=101),
        CourseActivity(name="Homework 2", module="assign", cmid=102),
    ]

    mapping = await mapper.build_mapping(activities, proposal)
    rows = {row["activity_name"]: row for row in mapping["graded_activities"]}
    assert rows["Homework 1"]["confirmed_category"] == ContentMapper.NOT_GRADED
    assert rows["Homework 2"]["confirmed_category"] == "Assignments"


def test_remove_grade_item_prefix_phrase_removes_item():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=100.0, items=["XYZ"]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "remove grade item XYZ")

    assert updated.categories[0].items == []
    assert any("removed 'xyz' from final exam" in str(n).lower() for n in (updated.notes or []))


def test_remove_item_from_category_phrase_does_not_remove_category():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=30.0, items=["XYZ"]),
            GradebookCategory(name="Assignments", weight=70.0, items=[]),
        ],
        notes=[],
    )

    updated = generator.update_from_prompt(base, "remove XYZ from Final Exam")
    by_name = {c.name: c for c in updated.categories}

    assert "Final Exam" in by_name
    assert by_name["Final Exam"].items == []
    assert not any("removed 'final exam'" in str(n).lower() for n in (updated.notes or []))


def test_proposal_keep_highest_per_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(base, "keep the best 3 from Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].keep_highest == 3
    assert by_name["Assignments"].drop_lowest == 0  # mutually exclusive


def test_proposal_extra_credit_per_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(base, "Assignments count as extra credit")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].extra_credit is True
    assert by_name["Midterm"].extra_credit is False


def test_proposal_exclude_empty_grades():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    # Default is True (exclude empty)
    assert all(c.aggregate_only_graded for c in base.categories)

    updated = generator.update_from_prompt(base, "include empty grades for Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].aggregate_only_graded is False
    assert by_name["Labs"].aggregate_only_graded is True


def test_proposal_exclude_empty_grades_category_first_with_typo_hint():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    step1 = generator.update_from_prompt(base, "include empty grades for Assignments")
    step2 = generator.update_from_prompt(step1, "for assignmnet Exclude empty grades")

    by_name = {c.name: c for c in step2.categories}
    assert by_name["Assignments"].aggregate_only_graded is True


def test_proposal_include_outcomes_per_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "include outcomes for Labs")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].aggregate_outcomes is True
    assert by_name["Assignments"].aggregate_outcomes is False


def test_category_settings_persist_in_schema_dump():
    cat = GradebookCategory(
        name="Quizzes",
        weight=20.0,
        drop_lowest=1,
        keep_highest=0,
        aggregate_only_graded=True,
        extra_credit=False,
    )
    dumped = cat.model_dump()
    assert dumped["drop_lowest"] == 1
    assert dumped["aggregate_only_graded"] is True
    assert dumped["extra_credit"] is False


def test_format_categories_shows_per_category_settings():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[
        GradebookCategory(name="Assignments", weight=40.0, drop_lowest=2),
        GradebookCategory(name="Final Exam", weight=60.0, extra_credit=True),
    ])
    text = cm._format_categories(proposal)
    assert "drop lowest 2" in text
    assert "extra credit" in text.lower()


def test_format_categories_shows_subcategories():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[
        GradebookCategory(
            name="Assignments",
            weight=40.0,
            items=["HW 1", "Project"],
            subcategories=[
                GradebookSubcategory(name="Homework", weight=20.0),
                GradebookSubcategory(name="Project", weight=20.0),
            ],
        ),
    ])

    text = cm._format_categories(proposal)
    assert text.startswith("  ├── **Assignments**") or text.startswith("  └── **Assignments**")
    assert "**Homework** (20.0%)" in text
    assert "**Project** (20.0%)" in text
    assert "HW 1" in text
    assert "├──" in text or "└──" in text


def test_add_to_existing_category_creates_manual_item_not_subcategory():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Final Exam", weight=100.0, items=[]),
        ]
    )

    updated = generator.update_from_prompt(proposal, "add test to Final exam")

    assert [cat.name for cat in updated.categories] == ["Final Exam"]
    assert updated.categories[0].items == ["test"]
    assert updated.categories[0].subcategories == []
    assert any("manual grade item 'test'" in str(note).lower() for note in (updated.notes or []))


def test_proposal_formula_is_applied_to_target_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "Set Final Exam as =([[midterm]]*0.4)+([[final]]*0.6)",
    )
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Final Exam"].calculation_formula == "=([[midterm]]*0.4)+([[final]]*0.6)"
    assert by_name["Final Exam"].formula_item_refs == ["midterm", "final"]
    assert any("Applied formula to Final Exam" in note for note in (updated.notes or []))


def test_invalid_formula_adds_validation_note():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "Use this formula: =[[midterm]]++[[final]]",
    )

    assert any("Formula ignored:" in note for note in (updated.notes or []))


def test_formula_resolver_direct_match_uses_grade_item_id():
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334),
        CourseActivity(name="Homework 2", module="assign", cmid=11, grade_item_id=335),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=average([[hw1]],[[hw2]])",
        activities=activities,
    )

    assert unresolved == []
    assert suggestions == {}
    assert resolved == "=average([[Homework 1]],[[Homework 2]])"


def test_formula_resolver_returns_unresolved_with_suggestions():
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334),
        CourseActivity(name="Final Exam", module="quiz", cmid=12, grade_item_id=501),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=average([[ghost_hw]],[[final]])",
        activities=activities,
    )

    assert "ghost_hw" in unresolved
    assert "final" not in unresolved
    assert "[[Final Exam]]" in resolved
    assert suggestions.get("ghost_hw") is not None


def test_formula_resolver_maps_hw_alias_to_assignment_numbered_item():
    activities = [
        CourseActivity(name="Assignment1", module="assign", cmid=20, grade_item_id=420),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=round([[hw1]],2)",
        activities=activities,
    )

    assert unresolved == []
    assert suggestions == {}
    assert "[[Assignment1]]" in resolved


def test_formula_resolver_supports_activity_and_manual_grade_items():
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334, itemtype="mod"),
        CourseActivity(name="Participation Bonus", module=None, cmid=None, grade_item_id=777, itemtype="manual"),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=round(([[hw1]]*0.9)+([[participationbonus]]*0.1),2)",
        activities=activities,
    )

    assert unresolved == []
    assert suggestions == {}
    assert "[[Homework 1]]" in resolved
    assert "[[Participation Bonus]]" in resolved


def test_formula_resolver_accepts_category_name_refs_when_activity_ids_missing():
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=if([[midterm]]>[[final]],[[midterm]],[[final]])",
        activities=activities,
        category_names=["Assignments", "Labs", "Midterm", "Final Exam"],
    )

    assert unresolved == []
    assert suggestions == {}
    assert "[[midterm]]" in resolved
    assert "[[final]]" in resolved


def test_formula_resolver_accepts_singular_ref_for_plural_category_name():
    activities = [
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334),
    ]

    resolved, unresolved, suggestions = FormulaResolver.resolve_formula(
        formula="=round([[project]],2)",
        activities=activities,
        category_names=["Projects"],
    )

    assert unresolved == []
    assert suggestions == {}
    assert "[[project]]" in resolved


def test_proposal_formula_accepts_subcategory_reference_aliases():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334),
    ])

    split = generator.update_from_prompt(base, "In Assignments, add subcategories: Homework 10%, Projects 15%")
    updated = generator.update_from_prompt(
        split,
        "Set the Assignments category formula to: =round(([[hw1]]*0.6)+([[project]]*0.4),2)",
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10, grade_item_id=334)],
    )

    assignments = next(c for c in (updated.categories or []) if c.name == "Assignments")
    assert assignments.calculation_formula is not None
    assert assignments.formula_unresolved_refs == []


@pytest.mark.asyncio
async def test_proposal_formula_midterm_final_refs_are_not_marked_unresolved():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_formula_category_refs",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Midterm 30%, Final 30%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign", cmid=101, grade_item_id=334),
        ],
    )

    updated = await engine.chat(
        session.session_id,
        "=if([[midterm]]>[[final]],[[midterm]],[[final]])",
    )
    by_name = {cat.name: cat for cat in (updated.proposal.categories if updated.proposal else [])}

    assert "Midterm" in by_name
    assert by_name["Midterm"].formula_unresolved_refs == []
    assert not any("unresolved formula refs [midterm], [final]" in str(n).lower() for n in (updated.proposal.notes or []))


def test_formula_error_reply_ignores_stale_unresolved_refs_when_parse_fails():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Labs", weight=25.0, items=[], formula_unresolved_refs=["lab1", "lab2"]),
        ],
        notes=[
            "Effect: Stored formula for 'Labs' (unresolved refs: [lab1], [lab2]).",
            "Formula ignored: Unsupported function 'vlookup'. Supported functions include: sum, average, max, min, if, round, mod, pi, power.",
        ],
    )

    reply = cm._formula_error_reply(proposal)
    assert "unsupported function 'vlookup'" in reply.lower()
    assert "labs: unresolved formula refs" not in reply.lower()


@pytest.mark.asyncio
async def test_proposal_formula_tracks_unresolved_references():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_formula",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign", cmid=101, grade_item_id=334),
        ],
    )

    updated = await engine.chat(session.session_id, "Set Assignments formula to =average([[hw1]],[[ghost]])")
    by_name = {cat.name: cat for cat in (updated.proposal.categories if updated.proposal else [])}

    assert "Assignments" in by_name
    assert by_name["Assignments"].formula_unresolved_refs == ["ghost"]
    assert any("unresolved references" in str(n).lower() for n in (updated.proposal.notes or []))
    assert any("Stored formula for 'Assignments'" in str(n) for n in (updated.proposal.notes or []))


@pytest.mark.asyncio
async def test_accept_blocks_when_formula_has_unresolved_refs():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_accept",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign", cmid=101, grade_item_id=334),
        ],
    )

    await engine.chat(session.session_id, "Set Assignments formula to =average([[unknown_ref]],[[hw1]])")
    accepted = await engine.accept(session.session_id)

    assert accepted.phase == "REFINEMENT"
    validation = (accepted.content_mapping or {}).get("validation") or {}
    assert validation.get("can_proceed") is False
    assert any("unresolved" in err.lower() for err in (validation.get("errors") or []))


@pytest.mark.asyncio
async def test_finalize_stays_in_refinement_when_formula_refs_are_unresolved():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_finalize_guard",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign", cmid=101, grade_item_id=334),
        ],
    )

    await engine.chat(session.session_id, "Set Assignments formula to =average([[unknown_ref]],[[hw1]])")
    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[{"moodle_cmid": 101, "category": "Assignments"}],
    )

    assert finalized.phase == "REFINEMENT"
    validation = (finalized.content_mapping or {}).get("validation") or {}
    assert validation.get("can_proceed") is False
    assert any("unresolved" in err.lower() for err in (validation.get("errors") or []))


@pytest.mark.asyncio
async def test_accept_warns_for_empty_categories_without_blocking():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_empty",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    accepted = await engine.accept(session.session_id)
    validation = (accepted.content_mapping or {}).get("validation") or {}

    assert accepted.phase == "ACCEPTED"
    assert validation.get("can_proceed") is True
    assert any("no activities assigned" in w.lower() for w in (validation.get("warnings") or []))
    assert any("manual grade item" in w.lower() for w in (validation.get("warnings") or []))


@pytest.mark.asyncio
async def test_finalize_accepts_manual_grade_item_created_after_accept():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_manual_finalize",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Midterm 40%, Final 60%")],
        course_activities=[],
    )

    engine._content_mapper.build_mapping = AsyncMock(return_value={
        "graded_activities": [],
        "validation_errors": [],
    })

    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[
            {
                "grade_item_id": 777,
                "moodle_cmid": None,
                "activity_name": "Midterm Manual Item",
                "itemtype": "manual",
                "item_source": "manual",
                "category": "Midterm",
            }
        ],
    )

    assert finalized.phase == "COMPLETED"
    graded = (finalized.content_mapping or {}).get("graded_activities") or []
    assert any(item.get("grade_item_id") == 777 for item in graded)


@pytest.mark.asyncio
async def test_accept_blocks_when_weighted_mean_total_is_not_100():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_weight_guard_accept",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    session.proposal.aggregation_method = 10
    session.proposal.categories = [
        GradebookCategory(name="Assignments", weight=70.0),
        GradebookCategory(name="Exams", weight=40.0),
    ]

    accepted = await engine.accept(session.session_id)
    validation = (accepted.content_mapping or {}).get("validation") or {}

    assert accepted.phase == "REFINEMENT"
    assert validation.get("can_proceed") is False
    assert any("expected 100.0" in err.lower() for err in (validation.get("errors") or []))


@pytest.mark.asyncio
async def test_finalize_stays_in_refinement_when_weighted_mean_total_is_not_100():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_weight_guard_finalize",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )

    session.proposal.aggregation_method = 10
    session.proposal.categories = [
        GradebookCategory(name="Assignments", weight=70.0),
        GradebookCategory(name="Exams", weight=40.0),
    ]

    finalized = await engine.finalize(
        session.session_id,
        confirmed_mapping=[{"moodle_cmid": 10, "category": "Assignments"}],
    )
    validation = (finalized.content_mapping or {}).get("validation") or {}

    assert finalized.phase == "REFINEMENT"
    assert validation.get("can_proceed") is False
    assert any("expected 100.0" in err.lower() for err in (validation.get("errors") or []))


def test_content_mapper_validate_mapping_detects_stale_categories():
    mapper = ContentMapper()
    proposal = GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)])
    mapping_rows = [
        {
            "moodle_cmid": 10,
            "activity_name": "Quiz 1",
            "suggested_category": "Quizzes",
            "confirmed_category": "Quizzes",
        }
    ]

    issues = mapper.validate_mapping(mapping_rows, proposal)

    assert len(issues) == 1
    assert issues[0]["missing_category"] == "Quizzes"


def test_infer_subcategory_rename_project_to_projects():
    before = {"assignments": ["Homework", "Project"]}
    after = {"assignments": ["Homework", "Projects"]}

    renames = infer_subcategory_renames(before, after)

    assert renames == [("assignments", "Project", "Projects")]


def test_sync_mapping_preserves_item_on_subcategory_rename():
    proposal_before = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Project", weight=15.0),
                ],
            )
        ]
    )
    proposal_after = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 201,
                "activity_name": "Course Project",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
                "suggested_subcategory": "Project",
                "confirmed_subcategory": "Project",
                "subcategory": "Project",
            }
        ]
    }
    renames = infer_subcategory_renames(
        snapshot_subcategories(proposal_before),
        snapshot_subcategories(proposal_after),
    )

    assert is_subcategory_only_proposal_change(proposal_before, proposal_after) is True
    assert sync_content_mapping_with_proposal_subcategory_changes(
        content_mapping,
        proposal_after,
        renames,
        [],
    ) is True

    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == "Projects"
    assert row["subcategory"] == "Projects"


def test_sync_content_mapping_clears_removed_subcategory():
    proposal_after = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Midterm",
                weight=20.0,
                subcategories=[],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 55,
                "activity_name": "Midterm Exam",
                "suggested_category": "Midterm",
                "confirmed_category": "Midterm",
                "suggested_subcategory": "Written",
                "confirmed_subcategory": "Written",
                "subcategory": "Written",
            }
        ]
    }

    assert sync_content_mapping_with_proposal_subcategory_changes(
        content_mapping,
        proposal_after,
        [],
        [("midterm", "Written")],
    ) is True

    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == ""
    assert row["subcategory"] == ""


def test_sync_confirmed_mapping_updates_subcategory_on_rows():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
                "suggested_subcategory": "",
                "confirmed_subcategory": "",
                "subcategory": "",
            }
        ]
    }
    confirmed_mapping = [
        {
            "moodle_cmid": 101,
            "activity_name": "Homework 1",
            "category": "Assignments",
            "subcategory": "Homework",
        }
    ]

    assert sync_confirmed_mapping_into_content_mapping(
        content_mapping,
        confirmed_mapping,
        proposal,
    ) is True

    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == "Homework"
    assert row["subcategory"] == "Homework"


def test_sync_confirmed_mapping_clears_invalid_subcategory():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
                "confirmed_subcategory": "Projects",
                "subcategory": "Projects",
            }
        ]
    }
    confirmed_mapping = [
        {
            "moodle_cmid": 101,
            "category": "Assignments",
            "subcategory": "Projects",
        }
    ]

    assert sync_confirmed_mapping_into_content_mapping(
        content_mapping,
        confirmed_mapping,
        proposal,
    ) is True

    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == ""
    assert row["subcategory"] == ""


def test_proposal_move_item_to_subcategory_updates_tree():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["HW 1", "Term Project"],
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Project", weight=15.0),
                ],
            )
        ]
    )

    moved_hw = generator.update_from_prompt(proposal, "move HW 1 to Homework")
    assignments = moved_hw.categories[0]
    homework_sub = next(sub for sub in assignments.subcategories if sub.name == "Homework")
    assert "HW 1" in homework_sub.items
    assert "HW 1" not in (assignments.items or [])

    moved_project = generator.update_from_prompt(
        moved_hw,
        "move Term Project to Project in Assignments",
    )
    assignments = moved_project.categories[0]
    project_sub = next(sub for sub in assignments.subcategories if sub.name == "Project")
    assert "Term Project" in project_sub.items


def test_proposal_move_multiple_items_to_subcategory_in_single_prompt():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["assignment1", "assignment 2", "Essay"],
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )

    updated = generator.update_from_prompt(
        proposal,
        "move assignment1 and assignment 2 to Homework",
    )
    assignments = updated.categories[0]
    homework_sub = next(sub for sub in assignments.subcategories if sub.name == "Homework")
    assert "assignment1" in (homework_sub.items or [])
    assert "assignment 2" in (homework_sub.items or [])
    assert "assignment1" not in (assignments.items or [])
    assert "assignment 2" not in (assignments.items or [])


def test_proposal_move_multiple_items_to_parent_category_in_single_prompt():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["Essay"],
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0, items=["assignment1", "assignment 2"]),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )

    updated = generator.update_from_prompt(
        proposal,
        "move assignment1 and assignment 2 to Assignments",
    )
    assignments = updated.categories[0]
    homework_sub = next(sub for sub in assignments.subcategories if sub.name == "Homework")
    assert "assignment1" in (assignments.items or [])
    assert "assignment 2" in (assignments.items or [])
    assert "assignment1" not in (homework_sub.items or [])
    assert "assignment 2" not in (homework_sub.items or [])


def test_add_manual_grade_item_to_subcategory_explicit():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Labs",
                weight=15.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(name="Lab Reports", weight=10.0),
                    GradebookSubcategory(name="In-Lab Work", weight=5.0),
                ],
            )
        ]
    )

    updated = generator.update_from_prompt(proposal, "add grade item lab1 to subcategory In-Lab Work")
    labs = updated.categories[0]
    inlab = next(sub for sub in labs.subcategories if sub.name == "In-Lab Work")
    assert "lab1" in (inlab.items or [])
    assert "lab1" not in (labs.items or [])


def test_add_manual_grade_item_to_subcategory_bare():
    generator = ProposalGenerator()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Labs",
                weight=15.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(name="Lab Reports", weight=10.0),
                    GradebookSubcategory(name="In-Lab Work", weight=5.0),
                ],
            ),
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=[],
                subcategories=[
                    GradebookSubcategory(name="Projects", weight=25.0),
                ],
            ),
        ]
    )

    updated = generator.update_from_prompt(proposal, "add grade item test to Projects")
    assignments = updated.categories[1]
    projects_sub = next(sub for sub in assignments.subcategories if sub.name == "Projects")
    assert "test" in (projects_sub.items or [])
    assert "test" not in (assignments.items or [])


def test_sync_mapping_subcategories_from_proposal():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0, items=["Quiz 1"]),
                ],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Quiz 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
            }
        ]
    }

    assert sync_mapping_subcategories_from_proposal(content_mapping, proposal) is True
    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == "Homework"


def test_apply_mapping_subcategory_operations_syncs_from_proposal():
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Quizzes",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=25.0, items=["Quiz 1"]),
                ],
            )
        ]
    )
    content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Quiz 1",
                "suggested_category": "Quizzes",
                "confirmed_category": "Quizzes",
            }
        ]
    }

    assert apply_mapping_subcategory_operations(content_mapping, proposal, "") is True
    row = content_mapping["graded_activities"][0]
    assert row["confirmed_subcategory"] == "Homework"


def test_format_categories_shows_items_under_subcategories_without_unassigned():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[
        GradebookCategory(
            name="Assignments",
            weight=25.0,
            items=["HW 2"],
            subcategories=[
                GradebookSubcategory(name="Homework", weight=10.0, items=["HW 1"]),
                GradebookSubcategory(name="Project", weight=15.0, items=["Term Project"]),
            ],
        ),
    ])

    text = cm._format_categories(proposal)
    assert "unassigned" not in text.lower()
    assert "HW 1" in text
    assert "Term Project" in text


def test_format_mapping_sync_status_includes_subcategory_counts():
    manager = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            )
        ]
    )
    session = GradebookSessionRecord(
        session_id="sub-sync-status",
        course_id="EECS-1000",
        professor_id="prof",
        bot_name="eecs-bot",
        phase="ACCEPTED",
        proposal=proposal,
        content_mapping={
            "graded_activities": [
                {
                    "moodle_cmid": 101,
                    "activity_name": "Homework 1",
                    "suggested_category": "Assignments",
                    "confirmed_category": "Assignments",
                    "confirmed_subcategory": "Homework",
                },
                {
                    "moodle_cmid": 102,
                    "activity_name": "Homework 2",
                    "suggested_category": "Assignments",
                    "confirmed_category": "Assignments",
                    "confirmed_subcategory": "Homework",
                },
            ],
            "validation_errors": [
                {
                    "activity_name": "Stale Item",
                    "missing_subcategory": "Old Sub",
                    "parent_category": "Assignments",
                }
            ],
        },
    )

    text = manager._format_mapping_sync_status(session, proposal)
    assert "Rows by subcategory" in text
    assert "Assignments/Homework: 2" in text
    assert "subcategories not present" in text


def test_should_invalidate_content_mapping_allows_weight_and_subcategory_changes():
    proposal_before = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Midterm",
                weight=30.0,
                subcategories=[GradebookSubcategory(name="Written", weight=20.0)],
            ),
            GradebookCategory(name="Final", weight=30.0),
        ]
    )
    proposal_after = GradebookProposal(
        categories=[
            GradebookCategory(name="Midterm", weight=20.0, subcategories=[]),
            GradebookCategory(name="Final", weight=40.0),
        ]
    )

    assert should_invalidate_content_mapping(proposal_before, proposal_after) is False
    assert is_subcategory_only_proposal_change(proposal_before, proposal_after) is True


@pytest.mark.asyncio
async def test_chat_subcategory_removal_with_weight_change_preserves_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_sub_remove",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Midterm 30%")],
        course_activities=[CourseActivity(name="Midterm Exam", module="quiz", cmid=55)],
    )
    session.proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Midterm",
                weight=30.0,
                subcategories=[GradebookSubcategory(name="Written", weight=20.0)],
            ),
            GradebookCategory(name="Final", weight=30.0),
        ]
    )
    session.content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 55,
                "activity_name": "Midterm Exam",
                "suggested_category": "Midterm",
                "confirmed_category": "Midterm",
                "suggested_subcategory": "Written",
                "confirmed_subcategory": "Written",
                "subcategory": "Written",
            }
        ],
        "validation_errors": [],
    }
    session.phase = "ACCEPTED"
    engine._active_sessions[session.session_id] = session

    updated = await engine.chat(
        session.session_id,
        "remove Written from Midterm, set Midterm to 20%, set Final to 40%",
    )

    assert updated.content_mapping is not None
    row = (updated.content_mapping or {}).get("graded_activities", [])[0]
    assert row["confirmed_subcategory"] == ""
    assert row["subcategory"] == ""


@pytest.mark.asyncio
async def test_chat_move_item_to_subcategory_updates_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_sub_move",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Quiz 1", module="quiz", cmid=101)],
    )
    session.proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["Quiz 1"],
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    session.content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Quiz 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
            }
        ],
        "validation_errors": [],
    }
    session.phase = "ACCEPTED"
    engine._active_sessions[session.session_id] = session

    updated = await engine.chat(session.session_id, "move Quiz 1 to Projects")

    assert updated.content_mapping is not None
    row = (updated.content_mapping or {}).get("graded_activities", [])[0]
    assert row["confirmed_subcategory"] == "Projects"
    assert row["subcategory"] == "Projects"


@pytest.mark.asyncio
async def test_sync_moodle_context_merges_confirmed_subcategory():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_sync_sub",
        bot_name="eecs-bot",
        moodle_resources=[],
        course_activities=[CourseActivity(name="Quiz 1", module="quiz", cmid=101)],
    )
    session.proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            )
        ]
    )
    session.content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 101,
                "activity_name": "Quiz 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
            }
        ],
        "validation_errors": [],
    }
    session.phase = "ACCEPTED"
    engine._active_sessions[session.session_id] = session

    updated = await engine.sync_moodle_context(
        session.session_id,
        confirmed_mapping=[
            {
                "moodle_cmid": 101,
                "activity_name": "Quiz 1",
                "category": "Assignments",
                "subcategory": "Homework",
            }
        ],
    )

    row = (updated.content_mapping or {}).get("graded_activities", [])[0]
    assert row["confirmed_subcategory"] == "Homework"
    assert row["subcategory"] == "Homework"


@pytest.mark.asyncio
async def test_chat_subcategory_rename_preserves_existing_content_mapping():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_sub_rename",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
    )
    session.proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Project", weight=15.0),
                ],
            )
        ]
    )
    session.content_mapping = {
        "graded_activities": [
            {
                "moodle_cmid": 10,
                "activity_name": "Homework 1",
                "suggested_category": "Assignments",
                "confirmed_category": "Assignments",
                "suggested_subcategory": "Homework",
                "confirmed_subcategory": "Homework",
                "subcategory": "Homework",
            }
        ],
        "validation_errors": [],
    }
    session.phase = "ACCEPTED"
    engine._active_sessions[session.session_id] = session

    updated = await engine.chat(session.session_id, "rename Homework to Homework Tasks")

    row = (updated.content_mapping or {}).get("graded_activities", [])[0]
    assert row["confirmed_subcategory"] == "Homework Tasks"
    assert updated.content_mapping is not None


@pytest.mark.asyncio
async def test_content_mapper_respects_proposal_item_placement():
    """Items already placed in the proposal tree should map with proposal placement, not LLM/deterministic."""
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0, items=["assignment1", "assignment 2"]),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            ),
            GradebookCategory(
                name="Quizzes",
                weight=15.0,
                items=["quiz 1", "quiz 2"],
            ),
        ]
    )
    activities = [
        CourseActivity(name="assignment1", module="assign", cmid=101),
        CourseActivity(name="assignment 2", module="assign", cmid=102),
        CourseActivity(name="quiz 1", module="quiz", cmid=103),
    ]
    result = await mapper.build_mapping(course_activities=activities, proposal=proposal)
    rows = {r["activity_name"]: r for r in result["graded_activities"]}

    assert rows["assignment1"]["confirmed_category"] == "Assignments"
    assert rows["assignment1"]["confirmed_subcategory"] == "Homework"
    assert rows["assignment 2"]["confirmed_category"] == "Assignments"
    assert rows["assignment 2"]["confirmed_subcategory"] == "Homework"
    assert rows["quiz 1"]["confirmed_category"] == "Quizzes"
    assert rows["quiz 1"]["confirmed_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_includes_manual_grade_items_from_proposal():
    """Manual grade items added to proposal should appear in mapping with item_source: manual."""
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Labs",
                weight=15.0,
                items=["lab_manual_parent"],
                subcategories=[
                    GradebookSubcategory(name="Lab Reports", weight=10.0, items=["report_manual"]),
                    GradebookSubcategory(name="In-Lab Work", weight=5.0, items=["lab1_manual"]),
                ],
            ),
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                items=["hw_ungraded"],
            ),
        ]
    )
    activities = [
        CourseActivity(name="assignment1", module="assign", cmid=101),
    ]
    result = await mapper.build_mapping(course_activities=activities, proposal=proposal)
    rows = {r["activity_name"]: r for r in result["graded_activities"]}

    # Manual item at parent level
    assert "lab_manual_parent" in rows
    assert rows["lab_manual_parent"]["item_source"] == "manual"
    assert rows["lab_manual_parent"]["confirmed_category"] == "Labs"
    assert rows["lab_manual_parent"]["confirmed_subcategory"] == ""
    assert rows["lab_manual_parent"]["itemtype"] == "manual"

    # Manual item in subcategory
    assert "report_manual" in rows
    assert rows["report_manual"]["item_source"] == "manual"
    assert rows["report_manual"]["confirmed_category"] == "Labs"
    assert rows["report_manual"]["confirmed_subcategory"] == "Lab Reports"
    assert rows["report_manual"]["itemtype"] == "manual"

    # Another manual item in different subcategory
    assert "lab1_manual" in rows
    assert rows["lab1_manual"]["item_source"] == "manual"
    assert rows["lab1_manual"]["confirmed_category"] == "Labs"
    assert rows["lab1_manual"]["confirmed_subcategory"] == "In-Lab Work"
    assert rows["lab1_manual"]["itemtype"] == "manual"

    # Manual item in Assignments parent
    assert "hw_ungraded" in rows
    assert rows["hw_ungraded"]["item_source"] == "manual"
    assert rows["hw_ungraded"]["confirmed_category"] == "Assignments"
    assert rows["hw_ungraded"]["confirmed_subcategory"] == ""
    assert rows["hw_ungraded"]["itemtype"] == "manual"


@pytest.mark.asyncio
async def test_content_mapper_suggests_subcategory_by_token_match():
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == "Assignments"
    assert row["suggested_subcategory"] == "Homework"


@pytest.mark.asyncio
async def test_content_mapper_subcategory_empty_when_llm_unsure():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": (
                        '[{"activity_key": "gradeitem:10:0", "category": "Assignments", "subcategory": "Projects", '
                        '"confidence": 0.55, "reasoning": "loose assignment fit", "subcategory_reasoning": "guess"}]'
                    )
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Weekly Journal", grade_item_id=10, itemtype="manual")],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == "Assignments"
    assert row["suggested_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_skips_subcategory_when_parent_has_no_subcategories():
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Midterm", weight=30.0),
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            ),
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Midterm Exam", module="quiz", cmid=102)],
        proposal=proposal,
    )

    row = next(item for item in result["graded_activities"] if item.get("moodle_cmid") == 102)
    assert row["suggested_category"] == "Midterm"
    assert row["suggested_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_skips_subcategory_when_category_uncertain():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": (
                        '[{"activity_key": "gradeitem:10:0", "category": "__uncategorized__", "subcategory": "Homework", '
                        '"confidence": 0.55, "subcategory_confidence": 0.95, "reasoning": "unclear fit", '
                        '"subcategory_reasoning": "should be ignored"}]'
                    )
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            )
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Weekly Journal", grade_item_id=10, itemtype="manual")],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == ContentMapper.UNCATEGORIZED
    assert row["suggested_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_rejects_llm_subcategory_from_wrong_parent():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": (
                        '[{"activity_key": "gradeitem:10:0", "category": "Assignments", "subcategory": "Lab Reports", '
                        '"confidence": 0.92, "subcategory_confidence": 0.9, "reasoning": "assignment fit", '
                        '"subcategory_reasoning": "wrong parent sub"}]'
                    )
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            ),
            GradebookCategory(
                name="Labs",
                weight=15.0,
                subcategories=[GradebookSubcategory(name="Lab Reports", weight=15.0)],
            ),
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Weekly Journal", grade_item_id=10, itemtype="manual")],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == "Assignments"
    assert row["suggested_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_skips_subcategory_when_no_token_match_under_confident_parent():
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Weekly Reflection", module="assign", cmid=10)],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == "Assignments"
    assert row["suggested_subcategory"] == ""


@pytest.mark.asyncio
async def test_content_mapper_llm_assigns_subcategory_when_confident():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": (
                        '[{"activity_key": "gradeitem:11:0", "category": "Assignments", "subcategory": "Projects", '
                        '"confidence": 0.92, "subcategory_confidence": 0.9, "reasoning": "project keyword", '
                        '"subcategory_reasoning": "name match"}]'
                    )
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=10.0),
                    GradebookSubcategory(name="Projects", weight=15.0),
                ],
            )
        ]
    )
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Capstone Project Draft", grade_item_id=11, itemtype="manual")],
        proposal=proposal,
    )

    row = result["graded_activities"][0]
    assert row["suggested_category"] == "Assignments"
    assert row["suggested_subcategory"] == "Projects"


def test_content_mapper_validate_mapping_detects_stale_subcategory():
    mapper = ContentMapper()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=25.0,
                subcategories=[GradebookSubcategory(name="Homework", weight=25.0)],
            )
        ]
    )
    mapping_rows = [
        {
            "moodle_cmid": 10,
            "activity_name": "Homework 1",
            "suggested_category": "Assignments",
            "confirmed_category": "Assignments",
            "suggested_subcategory": "Projects",
            "confirmed_subcategory": "Projects",
        }
    ]

    issues = mapper.validate_mapping(mapping_rows, proposal)

    assert len(issues) == 1
    assert issues[0]["missing_subcategory"] == "Projects"


def test_formula_error_reply_deduplicates_unresolved_warning_lines():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=100.0,
                calculation_formula="=average([[ghost]],[[hw1]])",
                formula_item_refs=["ghost", "hw1"],
                formula_unresolved_refs=["ghost"],
            )
        ],
        notes=[
            "Formula warning: unresolved references [ghost].",
            "Formula warning: unresolved references [ghost].",
        ],
    )

    text = cm._formula_error_reply(proposal)
    assert text.lower().count("unresolved formula refs") == 1


def test_format_categories_includes_formula_setting():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[
        GradebookCategory(
            name="Final Exam",
            weight=100.0,
            calculation_formula="=([[midterm]]*0.4)+([[final]]*0.6)",
            formula_item_refs=["midterm", "final"],
        ),
    ])

    text = cm._format_categories(proposal)
    assert "📐" in text
    assert "formula:" not in text.lower()
    assert "([[midterm]]*0.4)+([[final]]*0.6)" not in text


def test_conversation_help_request_returns_supported_instruction_list():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="what instruction do you support")

    assert "supported instructions" in reply.lower()
    assert "excel-style formulas" in reply.lower()
    assert "manual grade item" in reply.lower()
    assert "show grade items" in reply.lower()
    assert "show activities" in reply.lower()
    assert "show activities and grade items" in reply.lower()
    assert "show all activities" in reply.lower()
    assert "show all grade items" in reply.lower()
    assert "subcategory edits" in reply.lower()
    assert "drop projects" in reply.lower()
    assert "rename projects to project" in reply.lower()
    assert "add subcategory reflection" in reply.lower()
    assert "grouping bucket" in reply.lower()
    assert "concrete scored row" in reply.lower()
    assert "moodle activities cannot be removed" in reply.lower()
    assert "show proposal as markdown" in reply.lower()
    assert "show mapping sync status" in reply.lower()
    assert "show mapping rows before finalize" in reply.lower()
    assert "manual grade items" in reply.lower()
    assert "moodle activities" in reply.lower()
    assert "set homework 1 to not graded" in reply.lower()
    assert "don't grade" not in reply.lower()


def test_conversation_proposal_view_commands_return_distinct_outputs():
    cm = ConversationManager()
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=60.0,
                items=["HW 1"],
                subcategories=[GradebookSubcategory(name="Homework", weight=60.0, items=["HW 2"])],
            ),
            GradebookCategory(name="Final Exam", weight=40.0, items=["Final"]),
        ],
        notes=["Effect: Added manual grade item 'Final' to Final Exam"],
    )
    session = GradebookSessionRecord(
        session_id="s_views",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=proposal,
        course_activities=[CourseActivity(name="HW 1", module="assign")],
        content_mapping={
            "graded_activities": [
                {
                    "moodle_cmid": 101,
                    "activity_name": "HW 1",
                    "confirmed_category": "Assignments",
                    "confirmed_subcategory": "Homework",
                    "itemtype": "mod",
                },
                {
                    "moodle_cmid": None,
                    "activity_name": "Final",
                    "confirmed_category": "Final Exam",
                    "itemtype": "manual",
                    "grade_item_id": 501,
                },
            ],
            "validation_errors": [],
        },
    )

    full_reply = cm.make_reply(session, proposal, prompt="show proposal")
    markdown_reply = cm.make_reply(session, proposal, prompt="show proposal as markdown")
    listing_reply = cm.make_reply(session, proposal, prompt="show activities and grade items")
    sync_reply = cm.make_reply(session, proposal, prompt="show mapping sync status")
    rows_reply = cm.make_reply(session, proposal, prompt="show mapping rows before finalize")

    assert "Updated proposal:" in full_reply or "Current proposal:" in full_reply
    assert "**Grade Aggregation Method**" in full_reply
    assert "**Effects:**" in full_reply

    assert "**Proposal hierarchy:**" in markdown_reply
    assert "Grade Aggregation Method" not in markdown_reply
    assert "activity and grade-item summary" in listing_reply.lower()

    assert "mapping sync status" in sync_reply.lower()
    assert "Mapped rows:" in sync_reply
    assert "cmid 101" not in sync_reply

    assert "**Mapping rows**" in rows_reply
    assert "cmid 101" in rows_reply
    assert "Assignments > Homework" in rows_reply
    assert "Grade Aggregation Method" not in rows_reply

    assert len({full_reply, markdown_reply, listing_reply, sync_reply, rows_reply}) == 5


def test_conversation_mapping_prompt_shows_mapping_rows_in_accepted_phase():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s2b",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="ACCEPTED",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
        content_mapping={
            "graded_activities": [
                {
                    "moodle_cmid": 55,
                    "activity_name": "Quiz 1",
                    "confirmed_category": "Assignments",
                }
            ]
        },
    )

    reply = cm.make_reply(session, session.proposal, prompt="show mapping rows before finalize")

    assert "**Mapping rows**" in reply
    assert "Quiz 1" in reply
    assert "cmid 55" in reply
    assert "proposal accepted" not in reply.lower()


def test_conversation_help_and_formula_text_avoids_markdown_backticks():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s_help_style",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    help_reply = cm.make_reply(session, session.proposal, prompt="help")
    formula_reply = cm.make_reply(session, session.proposal, prompt="what excel formula we support")
    unsupported_reply = cm.make_reply(session, session.proposal, prompt="dada")

    assert "`" not in unsupported_reply

    assert "`[[item_id]]`" in help_reply
    assert "`=average([[hw1]],[[hw2]],[[project]])`" in help_reply
    assert "- Show proposal as markdown" in help_reply
    assert "- Show proposal —" in help_reply
    assert "- 'Show proposal as markdown'" not in help_reply
    assert "`=average([[hw1]],[[hw2]],[[hw3]])`" in formula_reply
    assert "`+` (addition)" in formula_reply


def test_conversation_lists_grade_items_and_activities_together():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1a",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Quiz 1", module="quiz"),
        ],
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(name="Assignments", weight=50.0, items=["Homework 1", "Final"]),
                GradebookCategory(name="Quizzes", weight=50.0, items=["Quiz 1"]),
            ],
        ),
    )

    reply = cm.make_reply(session, session.proposal, prompt="show activities and grade items")

    assert "moodle activities" in reply.lower()
    assert "homework 1" in reply.lower()
    assert "proposal grade items" in reply.lower()
    assert "final [manual]" in reply.lower()
    assert "quiz 1 [activity]" in reply.lower()


def test_conversation_show_grade_items_lists_all_items_by_default():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1b",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        course_activities=[
            CourseActivity(name="A1", module="assign"),
            CourseActivity(name="A2", module="assign"),
            CourseActivity(name="A3", module="assign"),
            CourseActivity(name="A4", module="assign"),
            CourseActivity(name="A5", module="assign"),
            CourseActivity(name="A6", module="assign"),
        ],
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(name="Assignments", weight=100.0, items=["A1", "A2", "A3", "A4", "A5", "A6"]),
            ],
        ),
    )

    reply = cm.make_reply(session, session.proposal, prompt="show grade items")

    assert "proposal grade items" in reply.lower()
    assert "moodle activities" not in reply.lower()
    assert "a6 [activity]" in reply.lower()
    assert "... and" not in reply.lower()


def test_format_categories_treats_post_finalize_manual_items_as_grade_items():
    """Manual items synced into course_activities after finalize must not be labeled activity."""
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1-post-finalize",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="COMPLETED",
        course_activities=[
            CourseActivity(name="quiz 1", module="quiz", cmid=101, itemtype="mod"),
            CourseActivity(name="final", itemtype="manual", grade_item_id=501),
            CourseActivity(name="Labs Manual Item", itemtype="manual", grade_item_id=502),
            CourseActivity(name="Midterm Manual Item", itemtype="manual", grade_item_id=503),
        ],
        content_mapping={
            "graded_activities": [
                {
                    "activity_name": "final",
                    "confirmed_category": "Final Exam",
                    "itemtype": "manual",
                    "item_source": "proposal_manual",
                },
                {
                    "activity_name": "Labs Manual Item",
                    "confirmed_category": "Labs",
                    "itemtype": "manual",
                    "item_source": "manual",
                },
                {
                    "activity_name": "Midterm Manual Item",
                    "confirmed_category": "Midterm",
                    "itemtype": "manual",
                    "item_source": "manual",
                },
            ]
        },
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(name="Labs", weight=15.0, items=["Labs Manual Item"]),
                GradebookCategory(name="Midterm", weight=30.0, items=["Midterm Manual Item"]),
                GradebookCategory(name="Final Exam", weight=30.0, items=["final"]),
                GradebookCategory(name="Quizzes", weight=15.0, items=["quiz 1"]),
            ],
        ),
    )

    tree = cm._format_categories(session.proposal, session)
    reply = cm.make_reply(session, session.proposal, prompt="show all activities")

    assert "[grade item] [Final Exam] final" in tree
    assert "[grade item] [Labs] Labs Manual Item" in tree
    assert "[grade item] [Midterm] Midterm Manual Item" in tree
    assert "[activity] [Quizzes] quiz 1" in tree
    assert "final" not in reply.lower().split("moodle activities:")[-1].split("proposal grade items")[0]
    assert "labs manual item" not in reply.lower().split("moodle activities:")[-1].split("proposal grade items")[0]


def test_conversation_listing_shows_show_all_hints_when_truncated():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1c",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        course_activities=[
            CourseActivity(name=f"Activity {i}", module="assign")
            for i in range(1, 11)
        ],
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(
                    name="Assignments",
                    weight=100.0,
                    items=[f"Activity {i}" for i in range(1, 8)],
                ),
            ],
        ),
    )

    reply = cm.make_reply(session, session.proposal, prompt="show activities and grade items")

    assert "... and" in reply.lower()
    assert "show all activities" in reply.lower()
    assert "show all grade items" in reply.lower()


def test_conversation_show_all_activities_returns_full_list():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s1d",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        course_activities=[
            CourseActivity(name=f"Activity {i}", module="assign")
            for i in range(1, 12)
        ],
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(name="Assignments", weight=100.0, items=["Activity 1"]),
            ],
        ),
    )

    reply = cm.make_reply(session, session.proposal, prompt="show all activities")

    assert "moodle activities" in reply.lower()
    assert "activity 11" in reply.lower()
    assert "... and" not in reply.lower()


def test_conversation_offtopic_prompt_returns_unsupported_warning_in_accepted_phase():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s2a",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="ACCEPTED",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="ffasfasfaf")

    assert "couldn't understand" in reply.lower()
    assert "manual grade item" in reply.lower()


def test_conversation_offtopic_prompt_returns_unsupported_warning_in_refinement():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s2",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="make me a pizza")

    assert "couldn't understand" in reply.lower()
    assert "type **help**" in reply.lower()


def test_conversation_formula_only_prompt_is_not_rejected_in_refinement():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s3",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Final Exam", weight=100.0)]),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="=if([[midterm]]>[[final]],[[midterm]],[[final]])",
    )

    assert "couldn't understand" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_undo_prompt_is_not_rejected_in_refinement():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s3u",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="undo")

    assert "couldn't understand" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_upload_signal_in_refinement_returns_analysis_message():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s3up",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="Uploaded syllabus_example.docx")

    assert "couldn't understand" not in reply.lower()
    assert "received your syllabus/supporting document" in reply.lower()


def test_conversation_upload_signal_with_proposal_request_returns_proposal():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s3up2",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        extraction={"has_syllabus": True},
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="use syllabus and give me a proposal")

    assert "received your syllabus/supporting document" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_give_me_proposal_based_on_what_you_know_is_supported():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s3up3",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)]),
    )

    reply = cm.make_reply(session, session.proposal, prompt="give me proposal based on what you know")

    assert "couldn't understand" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_formula_target_prompt_is_not_rejected_in_refinement():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s4",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Final Exam", weight=100.0)]),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="Use formula for Final Exam: =([[final_theory]]*0.7)+([[final_practical]]*0.3)",
    )

    assert "couldn't understand" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_clear_formula_prompt_is_not_rejected_in_refinement():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s5",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=GradebookProposal(categories=[GradebookCategory(name="Labs", weight=100.0)]),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="Clear formula from Labs and keep the category weight unchanged.",
    )

    assert "couldn't understand" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_formula_error_returns_friendly_warning_instead_of_proposal():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s5b",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        extraction={"proposal_changed": True},
        proposal=GradebookProposal(
            categories=[GradebookCategory(name="Midterm", weight=100.0)],
            notes=["Formula ignored: Missing closing parenthesis in formula."],
        ),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="Set Midterm formula to malformed input: =round(([[quiz1]]+[[quiz2]],2)",
    )

    assert "updated proposal" not in reply.lower()
    assert "couldn't apply that formula yet" in reply.lower()
    assert "retry" in reply.lower()
    assert "lthelp.yorku.ca/gradebook/creating-a-custom-formula" in reply.lower()
    assert "support.microsoft.com/excel" in reply.lower()


def test_conversation_does_not_repeat_formula_error_when_prompt_is_unrelated():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s5c",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        extraction={"proposal_changed": False},
        proposal=GradebookProposal(
            categories=[GradebookCategory(name="Assignments", weight=100.0, formula_unresolved_refs=["project"])],
            notes=["Formula warning: unresolved references [project]."],
        ),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="give me a proposal",
    )

    assert "saved the formula" not in reply.lower()
    assert "please fix these references to fully apply it" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_conversation_does_not_repeat_formula_error_when_formula_unchanged():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s5d",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        extraction={"proposal_changed": False},
        proposal=GradebookProposal(
            categories=[GradebookCategory(name="Assignments", weight=100.0, formula_unresolved_refs=["project"])],
            notes=["Formula warning: unresolved references [project]."],
        ),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="Use this formula for grade calculation: =average([[hw1]],[[project]])",
    )

    assert "saved the formula" not in reply.lower()
    assert "updated proposal" in reply.lower()


def test_refinement_reply_surfaces_item_weight_rejection_warning():
    cm = ConversationManager()
    session = GradebookSessionRecord(
        session_id="s_item_warning",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        extraction={"proposal_changed": True},
        proposal=GradebookProposal(
            categories=[
                GradebookCategory(name="Assignments", weight=25.0, items=[]),
                GradebookCategory(name="Midterm", weight=25.0, items=[]),
                GradebookCategory(name="Final Exam", weight=25.0, items=[]),
                GradebookCategory(
                    name="Quizzes",
                    weight=25.0,
                    items=["quiz 1", "quiz 2"],
                    item_weights={"quiz 1": 40.0, "quiz 2": 60.0},
                ),
            ],
            notes=[
                "Effect: Set item weight for 'quiz 1' in Quizzes to 40.0%",
                "Effect: Set item weight for 'quiz 2' in Quizzes to 60.0%",
                "Item weight check: Quizzes item weights would total 110.0% (max 100%).",
            ],
            aggregation_method=0,
        ),
    )

    reply = cm.make_reply(
        session,
        session.proposal,
        prompt="set weight of quiz 1 to 40% and set weight of quiz 2 to 70%",
    )

    assert "**weight update warning:**" in reply.lower()
    assert "were not applied" in reply.lower()
    assert "kept unchanged" in reply.lower()


def test_formula_if_expression_is_parsed_without_truncation():
    result = FormulaParser.extract_formula_and_detect("=if([[midterm]]>[[final]],[[midterm]],[[final]])")
    assert result is not None
    assert result["formula"] == "=if([[midterm]]>[[final]],[[midterm]],[[final]])"


def test_formula_prompt_with_average_does_not_change_aggregation_method():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "Replace Labs formula with: =average([[lab1]],[[lab2]],[[lab3]],[[lab4]])",
    )

    assert updated.aggregation_method == base.aggregation_method
    assert not any("Aggregation method set to" in n for n in (updated.notes or []))


def test_clear_formula_from_category_removes_formula_settings():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    with_formula = generator.update_from_prompt(base, "Set Labs formula to =average([[lab1]],[[lab2]])")
    cleared = generator.update_from_prompt(with_formula, "Clear formula from Labs and keep the category weight unchanged.")

    by_name = {c.name: c for c in cleared.categories}
    assert by_name["Labs"].calculation_formula is None
    assert by_name["Labs"].formula_item_refs == []
    assert any("Cleared formula from Labs" in n for n in (cleared.notes or []))


def test_clear_named_category_formula_phrase_only_clears_that_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    with_final = generator.update_from_prompt(base, "Set Final Exam formula to =average([[final_theory]],[[final_practical]])")
    with_both = generator.update_from_prompt(with_final, "Set Midterm formula to =if([[midterm]]>[[final]],[[midterm]],[[final]])")
    cleared = generator.update_from_prompt(with_both, "clear final exam formula")

    by_name = {c.name: c for c in cleared.categories}
    assert by_name["Final Exam"].calculation_formula is None
    assert by_name["Midterm"].calculation_formula is not None

    effects = [n for n in (cleared.notes or []) if str(n).startswith("Effect:")]
    assert any("Cleared formula from Final Exam" in n for n in effects)
    assert not any("Cleared formula from Midterm" in n for n in effects)


def test_formula_effects_properly_deduplicate_on_clear():
    """Regression test: formula effects should deduplicate so clear properly removes apply.
    
    When a formula is applied and then cleared in subsequent prompts, the effects
    should deduplicate so that the final effect shows only "Cleared formula from X",
    not both "Applied formula" and "Cleared formula".
    """
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    # Apply a formula
    with_formula = generator.update_from_prompt(
        base, 
        "Set Midterm formula to =if([[midterm]]>[[final]],[[midterm]],[[final]])"
    )
    midterm_cat = next(c for c in with_formula.categories if c.name == "Midterm")
    assert midterm_cat.calculation_formula == "=if([[midterm]]>[[final]],[[midterm]],[[final]])"
    
    # Count "Applied formula" effects
    apply_effects = [n for n in (with_formula.notes or []) if "Applied formula to Midterm" in n]
    assert len(apply_effects) == 1, "Should have one 'Applied formula' effect"
    
    # Clear the formula using explicit "clear formula" directive
    cleared = generator.update_from_prompt(with_formula, "clear formula from Midterm")
    midterm_cat = next(c for c in cleared.categories if c.name == "Midterm")
    assert midterm_cat.calculation_formula is None, "Formula should be None after clear"
    
    # Check effects: should have "Cleared formula", and old "Applied formula" should be gone due to deduplication
    effects = [n for n in (cleared.notes or []) if "formula" in n.lower()]
    apply_effects = [e for e in effects if "Applied formula to Midterm" in e]
    clear_effects = [e for e in effects if "Cleared formula from Midterm" in e]
    
    assert len(clear_effects) == 1, f"Should have exactly one 'Cleared formula' effect, got {clear_effects}"
    assert len(apply_effects) == 0, f"Old 'Applied formula' effects should be gone (deduplicated), got {apply_effects}"


def test_formula_ignored_note_does_not_persist_to_next_turn():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    invalid = generator.update_from_prompt(base, "Set Midterm formula to malformed input: =round(([[quiz1]]+[[quiz2]],2)")
    assert any("Formula ignored:" in n for n in (invalid.notes or []))

    valid = generator.update_from_prompt(invalid, "Set Midterm formula to =if([[midterm]]>[[final]],[[midterm]],[[final]])")
    assert not any("Formula ignored:" in n for n in (valid.notes or []))


def test_next_phase_mixed_affirmation_and_proposal_request_stays_proposal():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)])
    session = GradebookSessionRecord(
        session_id="s6",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="PROPOSAL",
        proposal=proposal,
    )

    phase = cm.next_phase(session, "okay use it and give me a proposal")
    assert phase == "PROPOSAL"


def test_next_phase_refinement_stays_refinement_for_refinement_prompt():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[GradebookCategory(name="Assignments", weight=100.0)])
    session = GradebookSessionRecord(
        session_id="s7",
        course_id="c1",
        professor_id="p1",
        bot_name="b1",
        phase="REFINEMENT",
        proposal=proposal,
    )

    phase = cm.next_phase(session, "set assignments formula to =average([[hw1]],[[hw2]])")
    assert phase == "REFINEMENT"


# --- Grade max, grade_pass, hidden, locked, display_type, decimals tests ---

def test_proposal_grade_max_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "max grade for Assignments is 150")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].grade_max == 150.0
    assert by_name["Labs"].grade_max == 100.0  # unchanged


def test_proposal_grade_max_to_syntax_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "max grade for Labs to 50")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].grade_max == 50.0


def test_proposal_grade_max_out_of_syntax():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "Labs out of 50")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].grade_max == 50.0


def test_proposal_grade_pass_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "passing grade for Labs is 60")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].grade_pass == 60.0
    assert by_name["Assignments"].grade_pass is None


def test_proposal_grade_pass_to_syntax_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "passing grade for Labs to 30")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].grade_pass == 30.0


def test_proposal_grade_min_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "minimum grade for Labs is 0")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].grade_min == 0.0


def test_proposal_hidden_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "hide the Midterm category")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Midterm"].hidden is True
    assert by_name["Assignments"].hidden is False


def test_proposal_show_unhides():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    # First hide, then show
    p1 = generator.update_from_prompt(base, "hide the Midterm category")
    p2 = generator.update_from_prompt(p1, "show the Midterm category")
    by_name = {c.name: c for c in p2.categories}
    assert by_name["Midterm"].hidden is False


def test_proposal_hidden_until_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "hide Labs until 2026-12-20")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Labs"].hidden is True
    assert by_name["Labs"].hidden_until is not None


def test_proposal_hidden_until_relative_parsed_from_hide_phrase():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "hide Final Exam until next week")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Final Exam"].hidden is True
    assert by_name["Final Exam"].hidden_until is not None
    assert any("effect: final exam hidden until next week" in str(n).lower() for n in (updated.notes or []))
    assert not any("effect: final exam hidden from students" in str(n).lower() for n in (updated.notes or []))


def test_proposal_hidden_until_relative_parsed_for_chained_categories():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "hide midterm until next week and final until next month")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Midterm"].hidden is True
    assert by_name["Final Exam"].hidden is True
    assert by_name["Midterm"].hidden_until is not None
    assert by_name["Final Exam"].hidden_until is not None
    assert by_name["Final Exam"].hidden_until > by_name["Midterm"].hidden_until
    assert any("effect: midterm hidden until next week" in str(n).lower() for n in (updated.notes or []))
    assert any("effect: final exam hidden until next month" in str(n).lower() for n in (updated.notes or []))


def test_proposal_hidden_until_accepts_untill_typo():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "hide midterm untill 2026-08-12")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Midterm"].hidden is True
    assert by_name["Midterm"].hidden_until is not None


def test_proposal_locked_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "lock the Final Exam")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Final Exam"].locked is True
    assert by_name["Assignments"].locked is False


def test_proposal_lock_time_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "lock Midterm until 2026-11-15")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Midterm"].locked is False
    assert by_name["Midterm"].lock_time is not None


def test_settings_prompt_does_not_change_weights():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(
        base,
        "Set minimum grade for Labs to 0, max grade for Labs to 50, passing grade for Labs to 30, show Labs as percentage with 1 decimal.",
    )
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].weight == 25.0
    assert by_name["Labs"].weight == 15.0
    assert by_name["Midterm"].weight == 30.0
    assert by_name["Final Exam"].weight == 30.0


def test_hide_lock_until_prompt_does_not_change_weights():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "Hide Midterm until 2026-09-20 and lock Final Exam until 2026-11-15")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].weight == 25.0
    assert by_name["Labs"].weight == 15.0
    assert by_name["Midterm"].weight == 30.0
    assert by_name["Final Exam"].weight == 30.0
    assert by_name["Midterm"].hidden is True
    assert by_name["Final Exam"].lock_time is not None


def test_create_prompt_rebuilds_full_category_set_with_new_names():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(
        base,
        "Build a balanced gradebook for this course: Quizzes 15%, Assignments 35%, Project 20%, Midterm 10%, Final 20%.",
    )
    by_name = {c.name: c for c in updated.categories}
    assert set(by_name.keys()) == {"Quizzes", "Assignments", "Project", "Midterm", "Final Exam"}
    assert by_name["Project"].weight == 20.0
    total = sum(c.weight for c in updated.categories)
    assert abs(total - 100.0) < 0.1


def test_create_prompt_simple_three_categories_replaces_defaults():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(
        base,
        "Propose a simple 3-category gradebook: Coursework 50%, Midterm 20%, Final 30%.",
    )
    by_name = {c.name: c for c in updated.categories}
    assert set(by_name.keys()) == {"Coursework", "Midterm", "Final Exam"}
    assert by_name["Coursework"].weight == 50.0
    total = sum(c.weight for c in updated.categories)
    assert abs(total - 100.0) < 0.1


def test_proposal_unlock_clears_lock_time():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    p1 = generator.update_from_prompt(base, "lock Midterm until 2026-11-15")
    p2 = generator.update_from_prompt(p1, "unlock Midterm")
    by_name = {c.name: c for c in p2.categories}
    assert by_name["Midterm"].locked is False
    assert by_name["Midterm"].lock_time is None


def test_proposal_decimals_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "use 2 decimal places for Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].decimals == 2
    assert by_name["Labs"].decimals == -1


def test_proposal_display_type_percentage():
    from criabot.gradebook.schemas import GRADE_DISPLAY_TYPE_PERCENTAGE
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "show Assignments as percentage")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].display_type == GRADE_DISPLAY_TYPE_PERCENTAGE


def test_proposal_display_type_letter():
    from criabot.gradebook.schemas import GRADE_DISPLAY_TYPE_LETTER
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "display Final Exam as letter")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Final Exam"].display_type == GRADE_DISPLAY_TYPE_LETTER


def test_category_all_new_fields_in_schema_dump():
    from criabot.gradebook.schemas import GRADE_DISPLAY_TYPE_PERCENTAGE
    cat = GradebookCategory(
        name="Labs",
        weight=20.0,
        aggregate_outcomes=True,
        grade_min=0.0,
        grade_max=150.0,
        grade_pass=60.0,
        hidden=True,
        hidden_until=1797724800,
        locked=False,
        lock_time=1797292800,
        display_type=GRADE_DISPLAY_TYPE_PERCENTAGE,
        decimals=2,
    )
    d = cat.model_dump()
    assert d["aggregate_outcomes"] is True
    assert d["grade_min"] == 0.0
    assert d["grade_max"] == 150.0
    assert d["grade_pass"] == 60.0
    assert d["hidden"] is True
    assert d["hidden_until"] == 1797724800
    assert d["locked"] is False
    assert d["lock_time"] == 1797292800
    assert d["display_type"] == GRADE_DISPLAY_TYPE_PERCENTAGE
    assert d["decimals"] == 2


def test_format_categories_shows_grade_max_pass_hidden_locked():
    cm = ConversationManager()
    proposal = GradebookProposal(categories=[
        GradebookCategory(
            name="Labs",
            weight=25.0,
            aggregate_outcomes=True,
            grade_min=0.0,
            grade_max=50.0,
            grade_pass=30.0,
            hidden=True,
            hidden_until=1797724800,
        ),
        GradebookCategory(name="Final Exam", weight=75.0, locked=True),
    ])
    text = cm._format_categories(proposal)
    assert "include outcomes" in text
    assert "min 0" in text
    assert "max 50" in text
    assert "pass ≥ 30" in text
    assert "hidden until" in text
    assert "hidden" in text.lower()
    assert "locked" in text.lower()


def test_normalize_trigger_rounds_total_to_100():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    over = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    total_before = sum(cat.weight for cat in over.categories)
    assert abs(total_before - 100.0) > 0.1, "Precondition: total should not be 100 yet"

    normalized = generator.update_from_prompt(over, "make the total weight 100%")
    total_after = sum(cat.weight for cat in normalized.categories)
    assert abs(total_after - 100.0) < 0.5, f"Expected ~100% after normalization, got {total_after}"

    normalized2 = generator.update_from_prompt(over, "round up to 100")
    total2 = sum(cat.weight for cat in normalized2.categories)
    assert abs(total2 - 100.0) < 0.5


def test_multi_weight_update_does_not_auto_normalize():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    updated = generator.update_from_prompt(base, "set Assignments to 20% and Labs to 10%")
    cat_map = {cat.name: cat for cat in updated.categories}
    assert cat_map["Assignments"].weight == 20.0
    assert cat_map["Labs"].weight == 10.0
    total = sum(cat.weight for cat in updated.categories)
    assert abs(total - 100.0) > 0.1, "Should allow non-100 totals; UI will block accept/finalize"



def test_weight_from_x_to_y_uses_destination_value():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    # Pre-set Assignments to 55%
    base2 = generator.update_from_prompt(base, "set Assignments to 55%")
    assert any(cat.name == "Assignments" and cat.weight == 55.0 for cat in base2.categories)

    result = generator.update_from_prompt(base2, "set Assignments weight from 55% to 50%")
    cat_map = {cat.name: cat for cat in result.categories}
    assert cat_map["Assignments"].weight == 50.0, "Should use destination value 50, not source 55"


def test_split_does_not_alter_parent_category_weight():
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    base2 = generator.update_from_prompt(base, "set Assignments to 40%")

    result = generator.update_from_prompt(
        base2,
        "Split Assignments (40%) into Homework 15% and Projects 25%, but keep Assignments as the parent category.",
    )
    cat_map = {cat.name: cat for cat in result.categories}
    assert cat_map["Assignments"].weight == 40.0, "Parent category weight must not change during split recording"
    split_notes = [n for n in result.notes if "split" in n.lower()]
    assert split_notes, "Should record split note"


def test_weight_without_percent_symbol_is_parsed():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(base, "change lap to 15 and final to 25")
    cat_map = {cat.name: cat for cat in updated.categories}

    assert cat_map["Labs"].weight == 15.0
    assert cat_map["Final Exam"].weight == 25.0


def test_increase_and_decrease_accordingly_adjusts_only_target():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "Increase Labs to 20% and decrease Final Exam accordingly to keep total exactly 100%.",
    )
    cat_map = {cat.name: cat for cat in updated.categories}

    assert cat_map["Labs"].weight == 20.0
    assert cat_map["Assignments"].weight == 25.0
    assert cat_map["Midterm"].weight == 30.0
    assert cat_map["Final Exam"].weight == 25.0
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(100.0)


def test_zero_midterm_redistributes_proportionally():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "Set Midterm to 0% and redistribute that weight proportionally across other categories.",
    )
    cat_map = {cat.name: cat for cat in updated.categories}

    assert cat_map["Midterm"].weight == 0.0
    assert cat_map["Assignments"].weight == pytest.approx(35.71, abs=0.02)
    assert cat_map["Labs"].weight == pytest.approx(21.43, abs=0.02)
    assert cat_map["Final Exam"].weight == pytest.approx(42.86, abs=0.02)
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(100.0, abs=0.05)


def test_remaining_weight_directive_updates_target_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(
        base,
        "make assignment 35% final 40% and give remaing weight ot midterm",
    )
    cat_map = {cat.name: cat for cat in updated.categories}

    assert cat_map["Assignments"].weight == 35.0
    assert cat_map["Final Exam"].weight == 40.0
    assert cat_map["Labs"].weight == 15.0
    assert cat_map["Midterm"].weight == 10.0
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(100.0)


def test_split_consistency_note_added_after_parent_weight_change():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    split = generator.update_from_prompt(
        base,
        "Split Assignments (25%) into Homework 10% and Projects 15%, but keep Assignments as the parent category.",
    )
    changed = generator.update_from_prompt(split, "set Assignments to 30%")

    assert any("internal split totals" in note.lower() for note in changed.notes)


@pytest.mark.asyncio
async def test_gradebook_chat_supports_undo_and_redo():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Lab 1", module="lab"),
        ],
    )

    updated = await engine.chat(session.session_id, "set labs to 20%")
    assert updated.proposal is not None
    assert {cat.name: cat.weight for cat in updated.proposal.categories}["Labs"] == 20.0

    undone = await engine.chat(session.session_id, "undo")
    assert undone.proposal is not None
    assert {cat.name: cat.weight for cat in undone.proposal.categories}["Labs"] == 15.0

    redone = await engine.chat(session.session_id, "redo")
    assert redone.proposal is not None
    assert {cat.name: cat.weight for cat in redone.proposal.categories}["Labs"] == 20.0


@pytest.mark.asyncio
async def test_gradebook_chat_undo_redo_survives_history_cache_loss():
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1001",
        professor_id="prof_b",
        bot_name="eecs-bot-2",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%")],
        course_activities=[
            CourseActivity(name="Homework 1", module="assign"),
            CourseActivity(name="Lab 1", module="lab"),
        ],
    )

    # Simulate stateless request handling by dropping process-local history.
    engine._proposal_history.clear()
    engine._proposal_history_index.clear()

    updated = await engine.chat(session.session_id, "set labs to 20%")
    assert updated.proposal is not None
    assert {cat.name: cat.weight for cat in updated.proposal.categories}["Labs"] == 20.0

    # Simulate another request served without in-memory history.
    engine._proposal_history.clear()
    engine._proposal_history_index.clear()

    undone = await engine.chat(session.session_id, "undo")
    assert undone.proposal is not None
    assert {cat.name: cat.weight for cat in undone.proposal.categories}["Labs"] == 15.0

    # And redo should still work after another memory drop.
    engine._proposal_history.clear()
    engine._proposal_history_index.clear()

    redone = await engine.chat(session.session_id, "redo")
    assert redone.proposal is not None
    assert {cat.name: cat.weight for cat in redone.proposal.categories}["Labs"] == 20.0


@pytest.mark.asyncio
async def test_content_mapper_uses_llm_assignment_when_available():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": '[{"moodle_cmid": 10, "category": "Assignments", "confidence": 0.93, "reasoning": "name match"}]'
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = ProposalGenerator().generate_initial([])
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=10)],
        proposal=proposal,
    )

    assert result["graded_activities"][0]["suggested_category"] == "Assignments"
    assert result["graded_activities"][0]["confirmed_category"] == "Assignments"


@pytest.mark.asyncio
async def test_content_mapper_falls_back_when_llm_fails():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.chat = AsyncMock(side_effect=RuntimeError("llm down"))

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = ProposalGenerator().generate_initial([])
    result = await mapper.build_mapping(
        course_activities=[CourseActivity(name="Lab 1", module="lab", cmid=11)],
        proposal=proposal,
    )

    assert result["graded_activities"][0]["suggested_category"] == "Labs"


@pytest.mark.asyncio
async def test_content_mapper_llm_call_includes_chat_id():
    """Ensure ensure_dialog is called before chat, and chat receives the required chat_id."""
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": '[{"moodle_cmid": 301, "category": "Quizzes", "confidence": 0.9, "reasoning": "quiz module"}]'
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = ProposalGenerator().generate_initial([])
    await mapper.build_mapping(
        course_activities=[
            CourseActivity(name="Knowledge Check 1", module="quiz", cmid=301),
        ],
        proposal=proposal,
    )

    sdk.agents.azure.chat.assert_called_once()
    sdk.agents.azure.ensure_dialog.assert_called_once()
    ensure_dialog_kwargs = sdk.agents.azure.ensure_dialog.call_args[1]
    ensured_chat_id = ensure_dialog_kwargs.get("chat_id")
    assert isinstance(ensured_chat_id, str)
    assert ensured_chat_id.startswith("gradebook-mapper-")
    _, call_kwargs = sdk.agents.azure.chat.call_args
    agent_config = call_kwargs.get("agent_config") or sdk.agents.azure.chat.call_args[1].get("agent_config")
    assert "chat_id" in agent_config, "agent_config must include chat_id"
    assert agent_config["chat_id"] == ensured_chat_id


@pytest.mark.asyncio
async def test_content_mapper_retries_with_new_chat_id_on_chat_ownership_conflict():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(side_effect=[
        RuntimeError("Ragflow API validation error: You don't own the chat gradebook-mapper-deadbeef"),
        {
            "agent_response": {
                "chat_response": {
                    "message": {
                        "content": '[{"moodle_cmid": 301, "category": "Assignments", "confidence": 0.9, "reasoning": "name match"}]'
                    }
                }
            }
        },
    ])

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    result = await mapper._llm_assignments(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=301)],
        proposal=_mapper_proposal_with_categories("Assignments"),
    )

    assert result[301]["category"] == "Assignments"
    assert sdk.agents.azure.chat.await_count == 2
    assert sdk.agents.azure.ensure_dialog.await_count == 2


@pytest.mark.asyncio
async def test_content_mapper_keeps_chat_id_on_all_retries_after_ownership_conflict():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(side_effect=[
        RuntimeError("Ragflow API validation error: You don't own the chat gradebook-mapper-deadbeef"),
        RuntimeError("Ragflow API validation error: You don't own the chat gradebook-mapper-badf00d"),
        {
            "agent_response": {
                "chat_response": {
                    "message": {
                        "content": '[{"moodle_cmid": 301, "category": "Assignments", "confidence": 0.9, "reasoning": "name match"}]'
                    }
                }
            }
        },
    ])

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    result = await mapper._llm_assignments(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=301)],
        proposal=_mapper_proposal_with_categories("Assignments"),
    )

    assert result[301]["category"] == "Assignments"
    assert sdk.agents.azure.chat.await_count == 3
    assert sdk.agents.azure.ensure_dialog.await_count == 3

    for call in sdk.agents.azure.chat.await_args_list:
        agent_config = call.kwargs.get("agent_config") or {}
        assert "chat_id" in agent_config
        assert isinstance(agent_config.get("chat_id"), str)
        assert agent_config.get("chat_id").startswith("gradebook-mapper-")


@pytest.mark.asyncio
async def test_content_mapper_reuses_preferred_chat_id_on_first_attempt():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": '[{"moodle_cmid": 301, "category": "Assignments", "confidence": 0.9, "reasoning": "name match"}]'
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    result, resolved_chat_id = await mapper._llm_assignments(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=301)],
        proposal=_mapper_proposal_with_categories("Assignments"),
        preferred_chat_id="gradebook-mapper-session123",
        return_chat_id=True,
    )

    assert result[301]["category"] == "Assignments"
    assert resolved_chat_id == "gradebook-mapper-session123"

    ensure_call_kwargs = sdk.agents.azure.ensure_dialog.await_args_list[0].kwargs
    assert ensure_call_kwargs.get("chat_id") == "gradebook-mapper-session123"

    chat_call_kwargs = sdk.agents.azure.chat.await_args_list[0].kwargs
    assert (chat_call_kwargs.get("agent_config") or {}).get("chat_id") == "gradebook-mapper-session123"


@pytest.mark.asyncio
async def test_content_mapper_rotates_chat_id_after_ownership_conflict_when_preferred_used():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(side_effect=[
        RuntimeError("Ragflow API validation error: You don't own the chat gradebook-mapper-session123"),
        {
            "agent_response": {
                "chat_response": {
                    "message": {
                        "content": '[{"moodle_cmid": 301, "category": "Assignments", "confidence": 0.9, "reasoning": "name match"}]'
                    }
                }
            }
        },
    ])

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    result, resolved_chat_id = await mapper._llm_assignments(
        course_activities=[CourseActivity(name="Homework 1", module="assign", cmid=301)],
        proposal=_mapper_proposal_with_categories("Assignments"),
        preferred_chat_id="gradebook-mapper-session123",
        return_chat_id=True,
    )

    assert result[301]["category"] == "Assignments"
    assert isinstance(resolved_chat_id, str)
    assert resolved_chat_id.startswith("gradebook-mapper-")
    assert resolved_chat_id != "gradebook-mapper-session123"


@pytest.mark.asyncio
async def test_accept_persists_mapper_chat_id_in_session_extraction():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": '[{"moodle_cmid": 501, "category": "Assignments", "confidence": 0.9, "reasoning": "assign module"}]'
                }
            }
        }
    })

    engine = GradebookSessionEngine(criadex=sdk, mapping_llm_model_id="gpt-3.5-turbo")
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%")],
        course_activities=[CourseActivity(name="Unknown Item", module="other", cmid=501)],
    )

    accepted = await engine.accept(session.session_id)
    persisted_chat_id = str((accepted.extraction or {}).get("llm_mapper_chat_id") or "")

    assert accepted.phase in {"ACCEPTED", "REFINEMENT"}
    assert persisted_chat_id.startswith("gradebook-mapper-")


@pytest.mark.asyncio
async def test_gradebook_get_returns_none_for_expired_active_session():
    engine = GradebookSessionEngine()
    engine._session_expire_seconds = 1
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Lecture 1 slides")],
        course_activities=[CourseActivity(name="Homework 1", module="assign")],
    )
    session.last_touched_at = int(time.time()) - 10

    assert await engine.get(session.session_id) is None


# --- Grade-Item-First Schema Tests ---

def test_course_activity_with_all_fields():
    """Test CourseActivity with complete grade-item-first fields."""
    activity = CourseActivity(
        cmid=101,
        module="assign",
        name="Homework 1",
        grade_item_id=501,
        itemtype="mod"
    )
    assert activity.cmid == 101
    assert activity.module == "assign"
    assert activity.name == "Homework 1"
    assert activity.grade_item_id == 501
    assert activity.itemtype == "mod"


def test_course_activity_with_optional_grade_item_fields():
    """Test CourseActivity with missing optional grade_item_id."""
    activity = CourseActivity(
        cmid=101,
        module="quiz",
        name="Midterm Exam"
    )
    assert activity.cmid == 101
    assert activity.module == "quiz"
    assert activity.name == "Midterm Exam"
    assert activity.grade_item_id is None
    assert activity.itemtype == "mod"  # defaults to "mod"


def test_course_activity_itemtype_defaults_to_mod():
    """Test that itemtype defaults to 'mod' when not specified."""
    activity = CourseActivity(
        name="Lab Work",
        module="lab",
        grade_item_id=502
    )
    assert activity.itemtype == "mod"


def test_course_activity_with_manual_itemtype():
    """Test CourseActivity with manual grade entry."""
    activity = CourseActivity(
        name="Extra Credit",
        grade_item_id=503,
        itemtype="manual"
    )
    assert activity.name == "Extra Credit"
    assert activity.grade_item_id == 503
    assert activity.itemtype == "manual"


def test_course_activity_only_requires_name():
    """Test that only 'name' is required field."""
    activity = CourseActivity(name="Assignment")
    assert activity.name == "Assignment"
    assert activity.cmid is None
    assert activity.module is None
    assert activity.grade_item_id is None
    assert activity.itemtype == "mod"


# --- Grade-Item-First ContentMapper Tests ---

@pytest.mark.asyncio
async def test_mapper_includes_grade_item_id_in_output():
    """Test that build_mapping includes grade_item_id and itemtype in result."""
    from criabot.gradebook.content_mapper import ContentMapper
    from criabot.gradebook.proposal import ProposalGenerator
    
    activities = [
        CourseActivity(
            name="Homework 1",
            module="assign",
            cmid=101,
            grade_item_id=501,
            itemtype="mod"
        ),
        CourseActivity(
            name="Quiz 1",
            module="quiz",
            cmid=102,
            grade_item_id=502,
            itemtype="mod"
        ),
    ]

    # Generate a proposal to use with the mapper
    generator = ProposalGenerator()
    proposal = generator.generate_initial(activities)

    mapper = ContentMapper()
    result = await mapper.build_mapping(
        course_activities=activities,
        proposal=proposal
    )

    assert "graded_activities" in result
    graded = result["graded_activities"]

    # Verify grade_item_id is present in mapping
    assert len(graded) > 0
    for item in graded:
        assert "grade_item_id" in item
        assert "itemtype" in item
        assert "moodle_cmid" in item
        assert "activity_name" in item
        assert "suggested_category" in item


@pytest.mark.asyncio
async def test_mapper_preserves_grade_item_id_across_mapping():
    """Test that grade_item_id is preserved exactly as provided."""
    from criabot.gradebook.content_mapper import ContentMapper
    from criabot.gradebook.proposal import ProposalGenerator
    
    test_grade_item_id = 505
    activities = [
        CourseActivity(
            name="Test Activity",
            module="forum",
            cmid=105,
            grade_item_id=test_grade_item_id,
            itemtype="mod"
        ),
    ]

    generator = ProposalGenerator()
    proposal = generator.generate_initial(activities)

    mapper = ContentMapper()
    result = await mapper.build_mapping(
        course_activities=activities,
        proposal=proposal
    )

    graded = result["graded_activities"]
    assert len(graded) > 0
    assert graded[0]["grade_item_id"] == test_grade_item_id


@pytest.mark.asyncio
async def test_mapper_handles_missing_grade_item_id():
    """Test mapper behavior when grade_item_id is not provided."""
    from criabot.gradebook.content_mapper import ContentMapper
    from criabot.gradebook.proposal import ProposalGenerator
    
    activities = [
        CourseActivity(
            name="Unmapped Assignment",
            module="assign",
            cmid=110
            # grade_item_id is None
        ),
    ]

    generator = ProposalGenerator()
    proposal = generator.generate_initial(activities)

    mapper = ContentMapper()
    result = await mapper.build_mapping(
        course_activities=activities,
        proposal=proposal
    )

    graded = result["graded_activities"]
    if graded:
        # grade_item_id should be None or 0 if not provided
        assert graded[0]["grade_item_id"] is None or graded[0]["grade_item_id"] == 0


@pytest.mark.asyncio
async def test_mapper_tracks_itemtype_from_course_activity():
    """Test that mapper correctly propagates itemtype."""
    from criabot.gradebook.content_mapper import ContentMapper
    from criabot.gradebook.proposal import ProposalGenerator
    
    activities = [
        CourseActivity(
            name="Manual Grade Item",
            grade_item_id=510,
            itemtype="manual"
        ),
        CourseActivity(
            name="Module Grade Item",
            module="assign",
            cmid=111,
            grade_item_id=511,
            itemtype="mod"
        ),
    ]

    generator = ProposalGenerator()
    proposal = generator.generate_initial(activities)

    mapper = ContentMapper()
    result = await mapper.build_mapping(
        course_activities=activities,
        proposal=proposal
    )

    graded = result["graded_activities"]
    assert len(graded) > 0

    # Find items and check itemtype
    for item in graded:
        if item["grade_item_id"] == 510:
            assert item["itemtype"] == "manual"
        elif item["grade_item_id"] == 511:
            assert item["itemtype"] == "mod"


@pytest.mark.asyncio
async def test_mapper_output_structure_includes_all_required_fields():
    """Test complete mapping output structure for finalize."""
    from criabot.gradebook.content_mapper import ContentMapper
    from criabot.gradebook.proposal import ProposalGenerator
    
    activities = [
        CourseActivity(
            name="Graded Lab",
            module="lab",
            cmid=120,
            grade_item_id=520,
            itemtype="mod"
        ),
    ]

    generator = ProposalGenerator()
    proposal = generator.generate_initial(activities)

    mapper = ContentMapper()
    result = await mapper.build_mapping(
        course_activities=activities,
        proposal=proposal
    )

    graded = result["graded_activities"]
    if graded:
        item = graded[0]
        # Verify all expected fields are present
        required_fields = [
            "grade_item_id",
            "itemtype",
            "moodle_cmid",
            "module_type",
            "activity_name",
            "suggested_category",
            "confirmed_category",
            "finalized",
            "confidence",
            "mapping_method"
        ]
        for field in required_fields:
            assert field in item, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_mapper_keyword_mapping_no_assignment_bias():
    """Test that Quiz, Midterm, Final keywords map correctly and don't default to Assignments."""
    mapper = ContentMapper()
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0),
            GradebookCategory(name="Quizzes", weight=25.0),
            GradebookCategory(name="Midterm", weight=25.0),
            GradebookCategory(name="Final Exam", weight=25.0),
        ]
    )
    
    activities = [
        CourseActivity(cmid=1, module="quiz", name="Quiz 1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=2, module="quiz", name="Midterm Exam", grade_item_id=102, itemtype="mod"),
        CourseActivity(cmid=3, module="quiz", name="Final Exam Week", grade_item_id=103, itemtype="mod"),
        CourseActivity(cmid=4, module="assign", name="Assignment 1", grade_item_id=104, itemtype="mod"),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    
    assert len(graded) == 4
    # Verify no defaulting to Assignments
    assert graded[0]["suggested_category"] == "Quizzes", "Quiz 1 should map to Quizzes"
    assert graded[1]["suggested_category"] == "Midterm", "Midterm Exam should map to Midterm"
    assert graded[2]["suggested_category"] == "Final Exam", "Final Exam Week should map to Final Exam"
    assert graded[3]["suggested_category"] == "Assignments", "Assignment 1 should map to Assignments"


@pytest.mark.asyncio
async def test_mapper_uncategorized_when_no_match():
    """Test that unmatchable items are marked UNCATEGORIZED, not defaulted to first category."""
    mapper = ContentMapper()
    # Explicitly set criadex to None to disable LLM (simulates LLM not available)
    mapper._criadex = None
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=50.0),
            GradebookCategory(name="Exams", weight=50.0),
        ]
    )
    
    # Create an activity with a name that has NO matching keywords
    # "Foo Bar Baz" contains no keywords from NAME_CATEGORY_KEYWORDS
    activities = [
        CourseActivity(
            cmid=100,
            module="workshop",  # workshop is not in MODULE_CATEGORY_HINTS
            name="Foo Bar Baz Activity XYZ",  # Name has no matching keywords
            grade_item_id=200,
            itemtype="mod"
        ),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    uncategorized = result["uncategorized_activities"]
    
    # Item should be marked UNCATEGORIZED, not defaulted to "Assignments"
    assert len(uncategorized) == 1, f"Expected 1 uncategorized item, got {len(uncategorized)}"
    assert graded[0]["suggested_category"] == mapper.UNCATEGORIZED
    assert graded[0]["confidence"] == 0.0


@pytest.mark.asyncio
async def test_mapper_item_source_distinction():
    """Test that mapper tracks item_source: 'activity' vs 'manual'."""
    mapper = ContentMapper()
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=100.0),
        ]
    )
    
    activities = [
        CourseActivity(cmid=1, module="assign", name="HW1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=None, module=None, name="Participation", grade_item_id=102, itemtype="manual"),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    
    assert graded[0]["item_source"] == "activity", "First item should be marked as activity"
    assert graded[1]["item_source"] == "manual", "Second item should be marked as manual"


@pytest.mark.asyncio
async def test_mapper_multiple_manual_items_without_cmid_are_distinct():
    """Ensure manual grade items (cmid=None) do not overwrite each other in mapping."""
    mapper = ContentMapper()
    mapper._criadex = None

    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Participation", weight=50.0),
            GradebookCategory(name="Midterm", weight=50.0),
        ]
    )

    activities = [
        CourseActivity(cmid=None, module=None, name="Participation", grade_item_id=501, itemtype="manual"),
        CourseActivity(cmid=None, module=None, name="Mid-term Exam", grade_item_id=502, itemtype="manual"),
    ]

    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]

    assert len(graded) == 2
    assert graded[0]["item_source"] == "manual"
    assert graded[1]["item_source"] == "manual"
    assert graded[0]["suggested_category"] == "Participation"
    assert graded[1]["suggested_category"] == "Midterm"


@pytest.mark.asyncio
async def test_content_mapper_llm_uses_activity_key_for_manual_grade_items_without_cmid():
    sdk = MagicMock()
    sdk.agents = MagicMock()
    sdk.agents.azure = MagicMock()
    sdk.agents.azure.ensure_dialog = AsyncMock(return_value={"status": 200})
    sdk.agents.azure.chat = AsyncMock(return_value={
        "agent_response": {
            "chat_response": {
                "message": {
                    "content": (
                        '[{"activity_key": "gradeitem:501:0", "moodle_cmid": null, '
                        '"category": "Midterm", "confidence": 0.92, "reasoning": "midterm keyword"}, '
                        '{"activity_key": "gradeitem:502:1", "moodle_cmid": null, '
                        '"category": "Final Exam", "confidence": 0.94, "reasoning": "final exam keyword"}]'
                    )
                }
            }
        }
    })

    mapper = ContentMapper(criadex=sdk, llm_model_id="gpt-3.5-turbo")
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Midterm", weight=40.0),
            GradebookCategory(name="Final Exam", weight=60.0),
        ]
    )

    result = await mapper.build_mapping(
        course_activities=[
            CourseActivity(name="Term Assessment", module=None, cmid=None, grade_item_id=501, itemtype="manual"),
            CourseActivity(name="Summative Assessment", module=None, cmid=None, grade_item_id=502, itemtype="manual"),
        ],
        proposal=proposal,
    )

    graded = result["graded_activities"]
    assert len(graded) == 2
    assert graded[0]["suggested_category"] == "Midterm"
    assert graded[1]["suggested_category"] == "Final Exam"
    assert graded[0]["activity_key"] == "gradeitem:501:0"
    assert graded[1]["activity_key"] == "gradeitem:502:1"


@pytest.mark.asyncio
async def test_mapper_keyword_confidence_levels():
    """Test that keyword matches report appropriate confidence scores."""
    mapper = ContentMapper()
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Quizzes", weight=50.0),
            GradebookCategory(name="Assignments", weight=50.0),
        ]
    )
    
    activities = [
        CourseActivity(cmid=1, module=None, name="quiz 1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=2, module=None, name="knowledge check", grade_item_id=102, itemtype="mod"),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    
    # "quiz" keyword has high confidence (0.90)
    assert graded[0]["confidence"] >= 0.90, "Quiz keyword should have high confidence"
    # "knowledge check" has lower confidence (0.80)
    assert graded[1]["confidence"] >= 0.80, "Knowledge check keyword should have good confidence"


@pytest.mark.asyncio
async def test_mapper_extended_keyword_set():
    """Test that new extended keyword set (lab, project, midterm, etc.) works correctly."""
    mapper = ContentMapper()
    # Disable LLM for this test to ensure deterministic matching
    mapper._criadex = None
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Labs", weight=15.0),
            GradebookCategory(name="Projects", weight=20.0),
            GradebookCategory(name="Midterm", weight=30.0),
            GradebookCategory(name="Final Exam", weight=35.0),
        ]
    )
    
    activities = [
        CourseActivity(cmid=1, module="lab", name="Lab 1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=2, module=None, name="Final Project", grade_item_id=102, itemtype="mod"),
        CourseActivity(cmid=3, module=None, name="Mid-term Exam", grade_item_id=103, itemtype="mod"),
        CourseActivity(cmid=4, module=None, name="Final Written Exam", grade_item_id=104, itemtype="mod"),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    
    # Diagnostic: print what we got for each item
    for i, item in enumerate(graded):
        print(f"Item {i}: {item['activity_name']} -> {item['suggested_category']} (confidence: {item['confidence']})")
    
    assert graded[0]["suggested_category"] == "Labs"
    assert graded[1]["suggested_category"] == "Projects"
    assert graded[2]["suggested_category"] == "Midterm", f"Expected Midterm but got {graded[2]['suggested_category']}"
    assert graded[3]["suggested_category"] == "Final Exam"


@pytest.mark.asyncio
async def test_mapper_respects_proposal_items_list():
    """Test that items explicitly listed in proposal categories are matched with high confidence."""
    mapper = ContentMapper()
    
    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=100.0, items=["Specific HW 1", "Specific HW 2"]),
        ]
    )
    
    activities = [
        CourseActivity(cmid=1, module="assign", name="Specific HW 1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=2, module="assign", name="Different Assignment", grade_item_id=102, itemtype="mod"),
    ]
    
    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]
    
    # First item is explicitly in proposal items list
    assert graded[0]["suggested_category"] == "Assignments"
    assert graded[0]["confidence"] == 0.95, "Explicit proposal item should have highest confidence (0.95)"
    
    # Second item matches by keyword
    assert graded[1]["suggested_category"] == "Assignments"
    assert graded[1]["confidence"] < 0.95, "Keyword match should have lower confidence than explicit"


@pytest.mark.asyncio
async def test_mapper_ignores_assignment_only_proposal_items_bias():
    """If only Assignments has proposal items, keyword/module mapping should still route quizzes correctly."""
    mapper = ContentMapper()
    mapper._criadex = None

    proposal = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=["assignment1", "quiz 1", "quiz 2"]),
            GradebookCategory(name="Labs", weight=15.0, items=[]),
            GradebookCategory(name="Midterm", weight=30.0, items=[]),
            GradebookCategory(name="Final Exam", weight=30.0, items=[]),
        ]
    )

    activities = [
        CourseActivity(cmid=1, module="assign", name="assignment1", grade_item_id=101, itemtype="mod"),
        CourseActivity(cmid=2, module="quiz", name="quiz 1", grade_item_id=102, itemtype="mod"),
        CourseActivity(cmid=3, module="quiz", name="quiz 2", grade_item_id=103, itemtype="mod"),
    ]

    result = await mapper.build_mapping(activities, proposal)
    graded = result["graded_activities"]

    assert graded[0]["suggested_category"] == "Assignments"
    assert graded[1]["suggested_category"] != "Assignments"
    assert graded[2]["suggested_category"] != "Assignments"

def test_delete_category_basic():
    """Test basic delete without explicit target - should redistribute equally."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    updated = generator.update_from_prompt(base, "delete labs and give weight to midterm")
    by_name = {c.name: c for c in updated.categories}
    
    # Midterm should have gained the labs weight
    assert "Labs" not in by_name, "Labs category should be deleted"
    assert by_name["Midterm"].weight == 30.0 + 15.0, "Midterm should have gained Labs' weight"


def test_delete_category_redistribute_equally():
    """Test delete with no explicit targets - weight is freed (mode 1, total decreases)."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    final_weight = next(c.weight for c in base.categories if c.name == "Final Exam")
    original_total = sum(c.weight for c in base.categories)

    updated = generator.update_from_prompt(base, "remove Final Exam")
    by_name = {c.name: c for c in updated.categories}

    assert "Final Exam" not in by_name, "Final Exam should be deleted"
    # Mode 1: freed weight — total decreases by the removed category's weight
    assert abs(sum(c.weight for c in updated.categories) - (original_total - final_weight)) < 0.1


def test_delete_category_single_target():
    """Test delete with single explicit target."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    updated = generator.update_from_prompt(base, "drop labs and give weight to assignments")
    by_name = {c.name: c for c in updated.categories}
    
    assert "Labs" not in by_name, "Labs should be deleted"
    assert by_name["Assignments"].weight == 25.0 + 15.0, "Assignments should gain Labs' weight"
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1, "Total should still be 100%"


def test_delete_category_multiple_targets():
    """Test delete with multiple explicit targets - should split weight."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    updated = generator.update_from_prompt(
        base,
        "remove midterm and split weight among assignments and labs"
    )
    by_name = {c.name: c for c in updated.categories}
    
    assert "Midterm" not in by_name, "Midterm should be deleted"
    # 30% of midterm should be split between 2 targets = 15% each
    assert abs(by_name["Assignments"].weight - (25.0 + 15.0)) < 0.1
    assert abs(by_name["Labs"].weight - (15.0 + 15.0)) < 0.1
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1


def test_delete_category_pattern_variations():
    """Test various natural language patterns for deletion."""
    generator = ProposalGenerator()
    patterns = [
        "remove quiz",
        "delete quiz",
        "drop quiz",
        "remove the quiz",
        "delete the quiz",
        "drop the Quizzes category",
    ]
    
    for pattern in patterns:
        base = generator.generate_initial([])
        updated = generator.update_from_prompt(base, pattern)
        by_name = {c.name: c for c in updated.categories}
        assert "Quizzes" not in by_name, f"Pattern '{pattern}' should delete Quizzes"


def test_item_weight_set_does_not_mutate_parent_category_weight():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Midterm", weight=25.0, items=[]),
            GradebookCategory(name="Final Exam", weight=25.0, items=[]),
            GradebookCategory(name="Quizzes", weight=25.0, items=["quiz 1", "quiz 2"]),
        ],
    )

    updated = generator.update_from_prompt(base, "set weight of quiz 1 to 40")
    by_name = {c.name: c for c in updated.categories}

    assert by_name["Quizzes"].weight == 25.0
    assert by_name["Quizzes"].item_weights.get("quiz 1") == 40.0
    assert by_name["Quizzes"].item_weights.get("quiz 2") is None
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1
    assert any("set item weight for 'quiz 1'" in str(n).lower() for n in (updated.notes or []))
    assert not any("set quizzes to" in str(n).lower() for n in (updated.notes or []))


def test_item_weight_assign_keyword_is_supported_without_parent_mutation():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Midterm", weight=25.0, items=[]),
            GradebookCategory(name="Final Exam", weight=25.0, items=[]),
            GradebookCategory(name="Quizzes", weight=25.0, items=["quiz 1", "quiz 2"]),
        ],
    )

    updated = generator.update_from_prompt(base, "assign quiz 1 to 40")
    by_name = {c.name: c for c in updated.categories}

    assert by_name["Quizzes"].weight == 25.0
    assert by_name["Quizzes"].item_weights.get("quiz 1") == 40.0
    assert by_name["Quizzes"].item_weights.get("quiz 2") is None
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1
    assert any("set item weight for 'quiz 1'" in str(n).lower() for n in (updated.notes or []))
    assert not any("set quizzes to" in str(n).lower() for n in (updated.notes or []))


def test_item_weight_multi_set_keeps_parent_weight_stable():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Midterm", weight=25.0, items=[]),
            GradebookCategory(name="Final Exam", weight=25.0, items=[]),
            GradebookCategory(name="Quizzes", weight=25.0, items=["quiz 1", "quiz 2"]),
        ],
    )

    updated = generator.update_from_prompt(
        base,
        "set weight of quiz 1 to 40 and set weight of quiz 2 to 60",
    )
    by_name = {c.name: c for c in updated.categories}

    assert by_name["Quizzes"].weight == 25.0
    assert by_name["Quizzes"].item_weights.get("quiz 1") == 40.0
    assert by_name["Quizzes"].item_weights.get("quiz 2") == 60.0
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1
    assert not any("set quizzes to" in str(n).lower() for n in (updated.notes or []))


def test_item_weight_validation_blocks_sum_over_100():
    generator = ProposalGenerator()
    base = GradebookProposal(
        categories=[
            GradebookCategory(name="Assignments", weight=25.0, items=[]),
            GradebookCategory(name="Midterm", weight=25.0, items=[]),
            GradebookCategory(name="Final Exam", weight=25.0, items=[]),
            GradebookCategory(name="Quizzes", weight=25.0, items=["quiz 1", "quiz 2"]),
        ],
    )

    updated = generator.update_from_prompt(
        base,
        "set weight of quiz 1 to 70 and set weight of quiz 2 to 40",
    )
    by_name = {c.name: c for c in updated.categories}

    assert by_name["Quizzes"].weight == 25.0
    assert by_name["Quizzes"].item_weights.get("quiz 1") == 70.0
    assert by_name["Quizzes"].item_weights.get("quiz 2") is None
    assert any("item weight check:" in str(n).lower() for n in (updated.notes or []))
    assert any("max 100%" in str(n).lower() for n in (updated.notes or []))


def test_delete_category_with_redistribution_syntax():
    """Test various patterns for specifying redistribution targets (mode 3)."""
    generator = ProposalGenerator()
    patterns = [
        "remove labs and give weight to assignments",
        "delete labs and redistribute to assignments",
        "drop labs and split among assignments",
        "remove labs and split weight among assignments and midterm",
    ]
    
    for pattern in patterns:
        base = generator.generate_initial([])
        updated = generator.update_from_prompt(base, pattern)
        by_name = {c.name: c for c in updated.categories}
        assert "Labs" not in by_name, f"Pattern '{pattern}' should delete Labs"
        # At least one target should have gained weight
        assert by_name["Assignments"].weight > 25.0 or by_name["Midterm"].weight > 30.0


def test_delete_non_existent_category():
    """Test that deleting non-existent category doesn't break proposal."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    original_count = len(base.categories)
    
    updated = generator.update_from_prompt(base, "delete NonExistentCategory")
    
    # Should not change anything
    assert len(updated.categories) == original_count, "Non-existent category delete should be ignored"
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1, "Total should remain 100%"


def test_delete_preserves_other_settings():
    """Test that delete doesn't affect category settings (formulas, hidden, etc)."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    # Set some properties on assignments
    with_props = generator.update_from_prompt(base, "set Assignments formula to =average([[hw1]],[[hw2]])")
    with_props = generator.update_from_prompt(with_props, "hide midterm")
    
    # Now delete final exam
    updated = generator.update_from_prompt(with_props, "drop final exam")
    by_name = {c.name: c for c in updated.categories}
    
    # Check properties are preserved
    assert by_name["Assignments"].calculation_formula is not None, "Formula should be preserved"
    assert by_name["Midterm"].hidden is True, "Hidden property should be preserved"


def test_delete_creates_effect_note():
    """Test that delete operation creates effect note in proposal."""
    generator = ProposalGenerator()
    base = generator.generate_initial([])
    
    updated = generator.update_from_prompt(base, "drop labs and give weight to assignments")
    
    effect_notes = [n for n in (updated.notes or []) if "Removed" in n or "distributed" in n.lower()]
    assert len(effect_notes) > 0, "Delete should create effect note"

@pytest.mark.asyncio
async def test_session_tracks_uploaded_documents():
    """Test that session tracks document IDs uploaded in it."""
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="DOC-TEST-001",
        professor_id="prof_doc",
        bot_name="doc-test-bot",
        moodle_resources=[MoodleResource(name="Syllabus.pdf", content_preview="Course info")],
        course_activities=[CourseActivity(name="Quiz 1", module="quiz")],
    )
    
    # Session should have empty uploaded_document_ids initially
    assert hasattr(session, 'uploaded_document_ids'), "Session should have uploaded_document_ids field"
    assert isinstance(session.uploaded_document_ids, list), "uploaded_document_ids should be a list"
    assert len(session.uploaded_document_ids) == 0, "Should start empty"


@pytest.mark.asyncio
async def test_session_delete_cleans_uploaded_documents():
    """Test that deleting session also cleans up tracked documents."""
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="DOC-TEST-002",
        professor_id="prof_doc",
        bot_name="doc-test-bot-2",
        moodle_resources=[MoodleResource(name="Syllabus.pdf", content_preview="Course info")],
        course_activities=[CourseActivity(name="Quiz 1", module="quiz")],
    )
    
    session_id = session.session_id
    
    # Simulate document upload by adding document IDs to the session
    session.uploaded_document_ids = ["doc-uploaded-session-1", "doc-uploaded-session-2"]
    
    # Delete the session
    result = await engine.delete(session_id)
    
    assert result['success'] is True, "Session deletion should succeed"
    assert "message" in result, "Response should have message"


@pytest.mark.asyncio
async def test_session_delete_gracefully_handles_missing_documents():
    """Test that session deletion doesn't fail if documents can't be deleted."""
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="DOC-TEST-003",
        professor_id="prof_doc",
        bot_name="doc-test-bot-3",
        moodle_resources=[MoodleResource(name="Syllabus.pdf")],
        course_activities=[],
    )
    
    session_id = session.session_id
    
    # Add non-existent document IDs
    session.uploaded_document_ids = ["non-existent-doc-123"]
    
    # Delete should still succeed even if documents don't exist
    result = await engine.delete(session_id)
    
    assert result['success'] is True, "Session deletion should succeed even with missing documents"


@pytest.mark.asyncio
async def test_gradebook_delete_in_conversation_flow():
    """Test delete/redistribute commands in full gradebook conversation."""
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="DELETE-FLOW-001",
        professor_id="prof_delete",
        bot_name="delete-bot",
        moodle_resources=[MoodleResource(name="Syllabus.pdf", content_preview="Assignments 25%, Quiz 15%")],
        course_activities=[
            CourseActivity(name="Homework", module="assign"),
            CourseActivity(name="Quiz", module="quiz"),
            CourseActivity(name="Midterm", module="quiz"),
        ],
    )
    
    # Initial proposal should have default categories
    updated = await engine.chat(session.session_id, "create proposal")
    by_name = {c.name: c for c in (updated.proposal.categories if updated.proposal else [])}
    initial_cat_count = len(by_name)
    
    # Delete a category and redistribute
    updated = await engine.chat(session.session_id, "remove Labs and give weight to Assignments")
    by_name = {c.name: c for c in (updated.proposal.categories if updated.proposal else [])}
    
    assert "Labs" not in by_name, "Labs should be deleted"
    assert len(by_name) < initial_cat_count, "Category count should decrease"
    assert abs(sum(c.weight for c in updated.proposal.categories) - 100.0) < 0.1


@pytest.mark.asyncio
async def test_delete_multiple_times_in_session():
    """Test multiple delete operations in same session."""
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="MULTI-DELETE-001",
        professor_id="prof_multi",
        bot_name="multi-delete-bot",
        moodle_resources=[MoodleResource(name="Syllabus.pdf")],
        course_activities=[],
    )
    
    # First delete
    updated = await engine.chat(session.session_id, "drop quizzes")
    by_name = {c.name: c for c in (updated.proposal.categories if updated.proposal else [])}
    assert "Quizzes" not in by_name
    
    # Second delete
    updated = await engine.chat(session.session_id, "and also remove labs")
    by_name = {c.name: c for c in (updated.proposal.categories if updated.proposal else [])}
    assert "Labs" not in by_name
    # Mode 1 (default): each bare delete frees weight — total is below 100%
    assert sum(c.weight for c in updated.proposal.categories) < 100.0


def test_task1_subcategory_can_override_aggregation_method():
    proposal = GradebookProposal(
        aggregation_method=10,
        categories=[
            GradebookCategory(
                name="Assignments",
                weight=70.0,
                subcategories=[
                    GradebookSubcategory(name="Homework", weight=60.0, aggregation_method=0),
                    GradebookSubcategory(name="Projects", weight=40.0, aggregation_method=11),
                ],
            ),
            GradebookCategory(name="Final Exam", weight=30.0),
        ],
    )

    payload = proposal.model_dump()
    subcats = payload["categories"][0]["subcategories"]
    assert subcats[0]["aggregation_method"] == 0
    assert subcats[1]["aggregation_method"] == 11


def test_task4_drop_lowest_quiz_weight_to_final_exam_phrase():
    generator = ProposalGenerator()
    base = generator.generate_initial([
        CourseActivity(name="Quiz 1", module="quiz"),
        CourseActivity(name="Quiz 2", module="quiz"),
    ])

    # Seed quiz category weight to emulate a realistic course setup.
    for category in base.categories:
        if category.name == "Quizzes":
            category.weight = 20.0
        elif category.name == "Labs":
            category.weight = 0.0
        elif category.name == "Assignments":
            category.weight = 20.0

    updated = generator.update_from_prompt(
        base,
        "Drop the lowest quiz and give its weight to the final exam",
    )
    by_name = {c.name: c for c in updated.categories}

    assert "Quizzes" not in by_name
    assert abs(by_name["Final Exam"].weight - 50.0) < 0.1
    assert abs(sum(c.weight for c in updated.categories) - 100.0) < 0.1


@pytest.mark.asyncio
async def test_session_delete_calls_criadex_content_delete_for_uploaded_docs():
    criadex = MagicMock()
    criadex.content = MagicMock()
    criadex.content.delete = AsyncMock(return_value={"success": True})

    engine = GradebookSessionEngine(criadex=criadex)
    session = await engine.start(
        course_id="DOC-CLEANUP-001",
        professor_id="prof_cleanup",
        bot_name="cleanup-bot",
        moodle_resources=[MoodleResource(name="Syllabus.pdf")],
        course_activities=[CourseActivity(name="Quiz 1", module="quiz")],
    )

    session.uploaded_document_ids = ["doc-a", "doc-b"]
    result = await engine.delete(session.session_id)

    assert result["success"] is True
    assert result["docs_deleted"] == 2
    assert criadex.content.delete.await_count == 2


@pytest.mark.asyncio
async def test_mapping_validation_recognizes_category_with_manual_grade_item():
    """
    Regression: categories with manual grade items added via proposal (e.g. 'final'
    in Final Exam) must NOT appear as empty in mapping validation warnings.
    Bug: _pre_accept_validation() only checked graded_activities rows from the
    mapping, ignoring proposal.category.items (manual grade items).
    """
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Final Exam 75%")],
        course_activities=[CourseActivity(name="Assignment 1", module="assign", cmid=10)],
    )

    # Add a manual grade item to Final Exam (which has no Moodle activities)
    session = await engine.chat(session.session_id, "add grade item 'final' to Final Exam")
    final_exam_cat = next((c for c in session.proposal.categories if c.name == "Final Exam"), None)
    assert final_exam_cat is not None
    assert any("final" in item.lower() for item in (final_exam_cat.items or [])), (
        f"Manual grade item 'final' must be in Final Exam items, got: {final_exam_cat.items}"
    )

    # Accept to trigger validation
    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    # Validation should NOT warn that Final Exam has no activities assigned
    validation = (accepted.content_mapping or {}).get("validation") or {}
    warnings = [str(w) for w in (validation.get("warnings") or [])]
    assert not any("Final Exam" in w and "no activities" in w for w in warnings), (
        f"Final Exam should not appear as empty — it has a manual grade item. Got warnings: {warnings}"
    )


@pytest.mark.asyncio
async def test_mapping_validation_still_warns_categories_with_no_items_or_activities():
    """
    The fix should only suppress the warning for categories that truly have manual
    grade items. Categories with zero items AND no mapped activities must still warn.
    """
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Assignments 25%, Labs 15%, Final Exam 60%")],
        course_activities=[CourseActivity(name="Assignment 1", module="assign", cmid=10)],
    )

    # Accept without adding any manual grade items — Labs and Final Exam have nothing
    accepted = await engine.accept(session.session_id)
    assert accepted.phase == "ACCEPTED"

    validation = (accepted.content_mapping or {}).get("validation") or {}
    warnings = [str(w) for w in (validation.get("warnings") or [])]
    # Labs and/or Final Exam have no activities and no manual grade items → must warn
    assert any("no activities" in w for w in warnings), (
        f"Expected a warning for empty categories, got: {warnings}"
    )


@pytest.mark.asyncio
async def test_mapping_validation_multiple_categories_manual_items_suppress_correctly():
    """
    When multiple categories each have manual grade items, none of them should
    appear in empty-category warnings.
    """
    engine = GradebookSessionEngine()
    session = await engine.start(
        course_id="EECS-1000",
        professor_id="prof_a",
        bot_name="eecs-bot",
        moodle_resources=[MoodleResource(name="Course Syllabus.pdf", content_preview="Midterm 40%, Final Exam 60%")],
        course_activities=[],
    )

    # Add manual grade items to both categories
    session = await engine.chat(session.session_id, "add grade item 'midterm exam' to Midterm")
    session = await engine.chat(session.session_id, "add grade item 'final exam' to Final Exam")

    midterm_cat = next((c for c in session.proposal.categories if c.name == "Midterm"), None)
    final_cat = next((c for c in session.proposal.categories if c.name == "Final Exam"), None)
    assert midterm_cat is not None and final_cat is not None

    accepted = await engine.accept(session.session_id)

    validation = (accepted.content_mapping or {}).get("validation") or {}
    warnings = [str(w) for w in (validation.get("warnings") or [])]
    # Neither category should be flagged
    assert not any("Midterm" in w and "no activities" in w for w in warnings), (
        f"Midterm has a manual grade item but is flagged empty. Warnings: {warnings}"
    )
    assert not any("Final Exam" in w and "no activities" in w for w in warnings), (
        f"Final Exam has a manual grade item but is flagged empty. Warnings: {warnings}"
    )
