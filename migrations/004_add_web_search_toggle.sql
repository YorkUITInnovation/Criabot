-- Migration: Add bot-level web search toggle
-- Created: 2026-04-29
-- Description: Persists per-bot control for web search fallback usage

SET @web_search_enabled_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'BotParameters'
      AND COLUMN_NAME = 'web_search_enabled'
);

SET @web_search_global_enabled_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'BotParameters'
      AND COLUMN_NAME = 'web_search_global_enabled'
);

SET @web_search_global_enabled_sql = IF(
    @web_search_global_enabled_exists = 0,
    'ALTER TABLE `BotParameters` ADD COLUMN `web_search_global_enabled` BOOLEAN NOT NULL DEFAULT TRUE COMMENT ''Global web search switch sent by Moodle admin setting''',
    'SELECT 1'
);

PREPARE web_search_global_enabled_stmt FROM @web_search_global_enabled_sql;
EXECUTE web_search_global_enabled_stmt;
DEALLOCATE PREPARE web_search_global_enabled_stmt;

SET @web_search_enabled_sql = IF(
    @web_search_enabled_exists = 0,
    'ALTER TABLE `BotParameters` ADD COLUMN `web_search_enabled` BOOLEAN NOT NULL DEFAULT FALSE COMMENT ''Enable web search fallback for this bot''',
    'SELECT 1'
);
PREPARE web_search_enabled_stmt FROM @web_search_enabled_sql;
EXECUTE web_search_enabled_stmt;
DEALLOCATE PREPARE web_search_enabled_stmt;
