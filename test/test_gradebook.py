import pytest
import time
import json
from unittest.mock import AsyncMock, MagicMock

from criabot.gradebook.analyzer import SyllabusAnalyzer
from criabot.gradebook.content_mapper import ContentMapper
from criabot.gradebook.conversation import ConversationManager
from criabot.gradebook.formula_parser import FormulaParser
from criabot.gradebook.proposal import ProposalGenerator
from criabot.gradebook.schemas import CourseActivity, GradebookCategory, GradebookProposal, GradebookSessionRecord, MoodleResource
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
    assert "formula:" in text
    assert "([[midterm]]*0.4)+([[final]]*0.6)" in text


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
    assert "type 'help'" in reply.lower()


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
