-- Migration: Add BotParents table for parent-child bot relationships
-- Created: 2025-01-19
-- Description: Creates junction table to support many-to-many parent-child bot relationships
--              with priority-based configuration inheritance

-- Create BotParents junction table
CREATE TABLE IF NOT EXISTS `BotParents` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `child_bot_id` INT NOT NULL,
    `parent_bot_id` INT NOT NULL,
    `priority` INT NOT NULL DEFAULT 0 COMMENT 'Lower number = higher priority (0 is highest)',
    FOREIGN KEY (`child_bot_id`) REFERENCES `Bots`(`id`) ON DELETE CASCADE,
    FOREIGN KEY (`parent_bot_id`) REFERENCES `Bots`(`id`) ON DELETE CASCADE,
    UNIQUE KEY `uq_bot_parents` (`child_bot_id`, `parent_bot_id`),
    INDEX `idx_child_bot_id` (`child_bot_id`),
    INDEX `idx_parent_bot_id` (`parent_bot_id`),
    INDEX `idx_priority` (`priority`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Add comment to table
ALTER TABLE `BotParents` COMMENT = 'Junction table for many-to-many parent-child bot relationships';

