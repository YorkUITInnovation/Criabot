import os
import re
import hashlib
import logging
import asyncio
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text, select, insert, create_engine, MetaData, Table, Column, Integer, String, DateTime
from sqlalchemy.ext.asyncio import create_async_engine

from .models import MigrationFile, MigrationRecord

logger = logging.getLogger(__name__)


class MigrationRunner:
    """
    Handles running database migrations
    
    Creates a migrations tracking table and applies migration files in order.
    """
    
    MIGRATIONS_TABLE = "schema_migrations"
    MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"
    
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.migrations_dir = self.MIGRATIONS_DIR
    
    async def initialize(self) -> None:
        """Create the migrations tracking table if it doesn't exist"""
        async with self.engine.begin() as conn:
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS `{self.MIGRATIONS_TABLE}` (
                    `id` INT AUTO_INCREMENT PRIMARY KEY,
                    `migration_name` VARCHAR(255) NOT NULL UNIQUE,
                    `applied_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    `checksum` VARCHAR(64) NULL,
                    INDEX `idx_migration_name` (`migration_name`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """))
        logger.info(f"Initialized migrations tracking table: {self.MIGRATIONS_TABLE}")
    
    def _get_migration_files(self) -> List[MigrationFile]:
        """Scan migrations directory and return sorted list of migration files"""
        migrations = []
        
        if not self.migrations_dir.exists():
            logger.warning(f"Migrations directory not found: {self.migrations_dir}")
            return migrations
        
        pattern = re.compile(r'^(\d+)_(.+)\.sql$')
        
        for file_path in sorted(self.migrations_dir.glob("*.sql")):
            match = pattern.match(file_path.name)
            if match:
                version = int(match.group(1))
                name = match.group(2)
                migrations.append(MigrationFile(
                    name=name,
                    path=str(file_path),
                    version=version
                ))
            else:
                logger.warning(f"Skipping migration file with invalid name format: {file_path.name}")
        
        return sorted(migrations)
    
    async def _calculate_checksum(self, file_path: str) -> str:
        """Calculate SHA256 checksum of migration file (async)"""
        def _read_and_hash():
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        return await asyncio.to_thread(_read_and_hash)
    
    async def _get_applied_migrations(self) -> List[str]:
        """Get list of already applied migration names"""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                text(f"SELECT `migration_name` FROM `{self.MIGRATIONS_TABLE}`")
            )
            return [row[0] for row in result]
    
    async def _record_migration(self, migration_name: str, checksum: str) -> None:
        """Record that a migration has been applied"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(f"""
                    INSERT INTO `{self.MIGRATIONS_TABLE}` (`migration_name`, `applied_at`, `checksum`)
                    VALUES (:name, NOW(), :checksum)
                """),
                {"name": migration_name, "checksum": checksum}
            )
    
    async def _apply_migration(self, migration: MigrationFile) -> None:
        """Apply a single migration file"""
        logger.info(f"Applying migration: {migration.name} (version {migration.version})")
        
        # Read migration SQL asynchronously
        def _read_sql():
            with open(migration.path, 'r', encoding='utf-8') as f:
                return f.read()
        sql_content = await asyncio.to_thread(_read_sql)
        
        # Calculate checksum
        checksum = await self._calculate_checksum(migration.path)
        
        # Execute migration in a transaction
        async with self.engine.begin() as conn:
            # Execute the migration SQL
            await conn.execute(text(sql_content))
            
            # Record the migration
            await self._record_migration(migration.name, checksum)
        
        logger.info(f"Successfully applied migration: {migration.name}")
    
    async def run_pending(self) -> List[str]:
        """
        Run all pending migrations
        
        :return: List of migration names that were applied
        """
        await self.initialize()
        
        applied_migrations = await self._get_applied_migrations()
        migration_files = self._get_migration_files()
        
        applied_names = []
        
        for migration in migration_files:
            migration_name = migration.name
            
            if migration_name in applied_migrations:
                logger.debug(f"Skipping already applied migration: {migration_name}")
                continue
            
            try:
                await self._apply_migration(migration)
                applied_names.append(migration_name)
            except Exception as e:
                logger.error(f"Failed to apply migration {migration_name}: {e}")
                raise
        
        if applied_names:
            logger.info(f"Applied {len(applied_names)} migration(s): {', '.join(applied_names)}")
        else:
            logger.info("No pending migrations to apply")
        
        return applied_names
    
    async def status(self) -> dict:
        """
        Get migration status
        
        :return: Dictionary with pending and applied migrations
        """
        await self.initialize()
        
        applied_migrations = set(await self._get_applied_migrations())
        migration_files = self._get_migration_files()
        
        pending = []
        applied = []
        
        for migration in migration_files:
            if migration.name in applied_migrations:
                applied.append(migration.name)
            else:
                pending.append(migration.name)
        
        return {
            "pending": pending,
            "applied": applied,
            "total": len(migration_files)
        }
