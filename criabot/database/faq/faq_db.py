from sqlalchemy.ext.asyncio import AsyncEngine

from criabot.database.faq.tables.faq_sync_logs import FAQSyncLogsAPI
from criabot.database.table import BaseDatabaseAPI


class FAQDatabaseAPI(BaseDatabaseAPI):
    """
    API for FAQ-specific database tables.
    """

    def __init__(self, engine: AsyncEngine):
        super().__init__(engine)
        self.sync_logs: FAQSyncLogsAPI = FAQSyncLogsAPI(engine)

    async def initialize(self) -> None:
        await self.sync_logs.initialize()
