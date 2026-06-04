-- Migration: Add BASELINE_READY phase to gradebook sessions
-- Created: 2026-05-27
-- Description: Extends the GradebookSessions phase enum to support baseline-import initialization.

ALTER TABLE `GradebookSessions`
    MODIFY COLUMN `phase` ENUM(
        'INTAKE',
        'ANALYSIS',
        'BASELINE_READY',
        'PROPOSAL',
        'REFINEMENT',
        'ACCEPTED',
        'CATEGORIZING',
        'COMPLETED',
        'FAILED'
    ) NOT NULL DEFAULT 'INTAKE' COMMENT 'Current session phase';