-- Migration: Add Gradebook tables for AI-powered gradebook generation
-- Created: 2026-04-10
-- Description: Creates tables to support gradebook session management, proposals, and results
--              for the AI gradebook generator feature

-- Create GradebookSessions table
CREATE TABLE IF NOT EXISTS `GradebookSessions` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` VARCHAR(64) NOT NULL UNIQUE COMMENT 'UUID for API reference',
    `course_id` VARCHAR(128) NOT NULL COMMENT 'Moodle course ID',
    `professor_id` VARCHAR(128) NOT NULL COMMENT 'Moodle user ID',
    `bot_name` VARCHAR(128) NOT NULL COMMENT 'Associated Cria bot',
    `phase` ENUM('INTAKE', 'ANALYSIS', 'PROPOSAL', 'REFINEMENT', 'ACCEPTED', 'CATEGORIZING', 'COMPLETED', 'FAILED')
           NOT NULL DEFAULT 'INTAKE' COMMENT 'Current session phase',
    `moodle_resources_json` JSON NULL COMMENT 'Captured Moodle resources for the session',
    `course_activities_json` JSON NULL COMMENT 'Captured Moodle course activities for the session',
    `proposal_json` JSON NULL COMMENT 'Current proposal state',
    `extraction_json` JSON NULL COMMENT 'Extracted syllabus data',
    `metadata_json` JSON NULL COMMENT 'Additional metadata',
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX `idx_course` (`course_id`),
    INDEX `idx_professor` (`professor_id`),
    INDEX `idx_phase` (`phase`),
    INDEX `idx_session_id` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Create GradebookResults table
CREATE TABLE IF NOT EXISTS `GradebookResults` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `session_id` INT NOT NULL COMMENT 'Reference to GradebookSessions.id',
    `course_id` VARCHAR(128) NOT NULL COMMENT 'Moodle course ID',
    `professor_id` VARCHAR(128) NOT NULL COMMENT 'Moodle user ID',
    `gradebook_json` JSON NOT NULL COMMENT 'Final accepted gradebook structure',
    `content_mapping_json` JSON NULL COMMENT 'Activity-to-category mapping',
    `pushed_to_moodle` BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'Whether pushed to Moodle',
    `moodle_sync_at` TIMESTAMP NULL COMMENT 'When pushed to Moodle',
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (`session_id`) REFERENCES `GradebookSessions`(`id`) ON DELETE CASCADE,
    INDEX `idx_course_result` (`course_id`),
    INDEX `idx_professor_result` (`professor_id`),
    INDEX `idx_session_result` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Add comments to tables
ALTER TABLE `GradebookSessions` COMMENT = 'Active and historical gradebook generation sessions';
ALTER TABLE `GradebookResults` COMMENT = 'Finalized gradebook proposals and Moodle sync status';