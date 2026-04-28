import pytest
import time
import json
from unittest.mock import AsyncMock, MagicMock

from criabot.gradebook.analyzer import SyllabusAnalyzer
from criabot.gradebook.content_mapper import ContentMapper
from criabot.gradebook.proposal import ProposalGenerator
from criabot.gradebook.schemas import CourseActivity, MoodleResource
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
