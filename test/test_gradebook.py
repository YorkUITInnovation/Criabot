import pytest
import time
import json
from unittest.mock import AsyncMock, MagicMock

from criabot.gradebook.analyzer import SyllabusAnalyzer
from criabot.gradebook.content_mapper import ContentMapper
from criabot.gradebook.conversation import ConversationManager
from criabot.gradebook.proposal import ProposalGenerator
from criabot.gradebook.schemas import CourseActivity, GradebookCategory, GradebookProposal, MoodleResource
from criabot.gradebook.session import GradebookSessionEngine


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
async def test_gradebook_proposal_remove_category_rebalances_weights():
    generator = ProposalGenerator()
    base_proposal = generator.generate_initial([
        CourseActivity(name="Homework 1", module="assign"),
        CourseActivity(name="Lab 1", module="lab"),
    ])

    updated = generator.update_from_prompt(
        base_proposal,
        "remove assignments",
    )

    names = {cat.name for cat in updated.categories}
    assert "Assignments" not in names
    assert sum(cat.weight for cat in updated.categories) == pytest.approx(100.0)


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

    p1 = generator.update_from_prompt(base, "set Assignments to 40% and Final Exam to 40%")
    weight_notes_p1 = [n for n in p1.notes if n.lower().startswith("weight check:")]
    assert len(weight_notes_p1) == 1, "First update should add exactly one weight-check note"

    p2 = generator.update_from_prompt(p1, "set Assignments to 35%")
    weight_notes_p2 = [n for n in p2.notes if n.lower().startswith("weight check:")]
    assert len(weight_notes_p2) == 1, "Second update must not accumulate old weight-check notes"


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


def test_proposal_drop_lowest_per_category():
    generator = ProposalGenerator()
    base = generator.generate_initial([])

    updated = generator.update_from_prompt(base, "drop the lowest 1 from Assignments")
    by_name = {c.name: c for c in updated.categories}
    assert by_name["Assignments"].drop_lowest == 1
    assert by_name["Midterm"].drop_lowest == 0


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
