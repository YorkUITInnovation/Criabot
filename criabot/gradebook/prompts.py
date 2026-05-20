ANALYSIS_PROMPT = (
    "Extract gradebook structure from the syllabus. "
    "Return assessment types, weights, item counts, due date patterns, and grading policies."
)

REFINEMENT_PROMPT = (
    "Refine the current gradebook proposal according to professor feedback while ensuring weights match the selected aggregation method. "
    "Support dual-action instructions like deleting/dropping one category and reallocating its weight to one or more targets."
)
