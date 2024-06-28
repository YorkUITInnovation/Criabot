/*
Superuser commands
SET GLOBAL time_zone = '+0:00';
CREATE DATABASE IF NOT EXISTS criadex;
 */

CREATE TABLE IF NOT EXISTS `Bots` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `name` VARCHAR(128) NOT NULL UNIQUE,
    `created` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `BotParameters` (

    /* Identifiers */
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `bot_id` INT NOT NULL UNIQUE,

    /* Model Query Params */
    `max_input_tokens` INT NOT NULL, # prev: max_context
    `max_reply_tokens` INT NOT NULL, # prev: max_tokens
    `temperature` DECIMAL(2, 1) NOT NULL,
    `top_p` DECIMAL (2, 1) NOT NULL,

    /* Retrieval Params */
    `top_k` INT NOT NULL,
    `min_k` DECIMAL(2, 1) NOT NULL,  # NEW

    /* Rerank Params */
    `top_n` INT NOT NULL,  # NEW
    `min_n` DECIMAL(2, 1) NOT NULL,  # prev: min_relevance

    /* Chat Params */
    `no_context_message` LONGTEXT NOT NULL,
    `no_context_use_message` TINYINT NOT NULL,
    `no_context_llm_guess` TINYINT NOT NULL,
    `system_message` LONGTEXT NOT NULL,

    FOREIGN KEY(bot_id) REFERENCES Bots(`id`)
)

