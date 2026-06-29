-- Migration: Add FAQ fallback controls and sync history
-- Created: 2026-04-16
-- Description: Persists bot-level FAQ fallback configuration and stores FAQ sync runs

SET @faq_fallback_enabled_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'BotParameters'
      AND COLUMN_NAME = 'faq_fallback_enabled'
);
SET @faq_fallback_enabled_sql = IF(
    @faq_fallback_enabled_exists = 0,
    'ALTER TABLE `BotParameters` ADD COLUMN `faq_fallback_enabled` BOOLEAN NOT NULL DEFAULT TRUE COMMENT ''Enable FAQ fallback when primary retrieval confidence is low''',
    'SELECT 1'
);
PREPARE faq_fallback_enabled_stmt FROM @faq_fallback_enabled_sql;
EXECUTE faq_fallback_enabled_stmt;
DEALLOCATE PREPARE faq_fallback_enabled_stmt;

SET @faq_fallback_threshold_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'BotParameters'
      AND COLUMN_NAME = 'faq_fallback_threshold'
);
SET @faq_fallback_threshold_sql = IF(
    @faq_fallback_threshold_exists = 0,
    'ALTER TABLE `BotParameters` ADD COLUMN `faq_fallback_threshold` DECIMAL(3,2) NOT NULL DEFAULT 0.50 COMMENT ''Minimum primary retrieval score before FAQ fallback is triggered''',
    'SELECT 1'
);
PREPARE faq_fallback_threshold_stmt FROM @faq_fallback_threshold_sql;
EXECUTE faq_fallback_threshold_stmt;
DEALLOCATE PREPARE faq_fallback_threshold_stmt;

CREATE TABLE IF NOT EXISTS `FAQSyncLogs` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `run_at` TIMESTAMP NOT NULL COMMENT 'When the sync started',
    `completed_at` TIMESTAMP NULL COMMENT 'When the sync finished',
    `source_url` VARCHAR(512) NOT NULL COMMENT 'Crawler entrypoint URL',
    `group_name` VARCHAR(255) NOT NULL COMMENT 'Target FAQ group name',
    `max_pages` INT NOT NULL COMMENT 'Configured crawl page cap',
    `timeout_seconds` DECIMAL(6,2) NOT NULL COMMENT 'Crawler timeout for the run',
    `trigger_graph_build` BOOLEAN NOT NULL DEFAULT TRUE COMMENT 'Whether graph build was requested',
    `state` VARCHAR(32) NOT NULL COMMENT 'Run state: RUNNING, READY, ERROR',
    `pages_crawled` INT NOT NULL DEFAULT 0 COMMENT 'Number of pages crawled',
    `indexed_files` INT NOT NULL DEFAULT 0 COMMENT 'Number of newly indexed files',
    `duplicate_files` INT NOT NULL DEFAULT 0 COMMENT 'Number of duplicate uploads tolerated',
    `error` VARCHAR(2048) NULL COMMENT 'Last error for failed runs',
    `graph_build_job` JSON NULL COMMENT 'Graph build job metadata',
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX `idx_faq_sync_run_at` (`run_at`),
    INDEX `idx_faq_sync_state` (`state`),
    INDEX `idx_faq_sync_group` (`group_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE `FAQSyncLogs` COMMENT = 'Historical FAQ crawler/indexer runs for monitoring and recovery';
