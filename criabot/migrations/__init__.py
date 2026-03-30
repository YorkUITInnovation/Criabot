"""
Database migrations framework for Criabot

This module provides a simple migration system to track and apply database schema changes.
"""

from .runner import MigrationRunner
from .models import MigrationRecord

__all__ = ["MigrationRunner", "MigrationRecord"]
