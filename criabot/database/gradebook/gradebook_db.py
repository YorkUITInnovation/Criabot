from sqlalchemy.ext.asyncio import AsyncEngine

from criabot.database.gradebook.tables.gradebook_sessions import GradebookSessionsAPI
from criabot.database.gradebook.tables.gradebook_results import GradebookResultsAPI
from criabot.database.table import BaseDatabaseAPI


class GradebookDatabaseAPI(BaseDatabaseAPI):
    """
    API for interfacing with gradebook tables in the database
    """

    def __init__(self, engine: AsyncEngine):
        """
        Instantiate the gradebook database API
        :param engine: SQL Pool
        """
        super().__init__(engine)

        self.sessions: GradebookSessionsAPI = GradebookSessionsAPI(engine)
        self.results: GradebookResultsAPI = GradebookResultsAPI(engine)

    async def initialize(self) -> None:
        """
        Initialize the database to create objects if they don't exist
        :return: None
        """
        await self.sessions.initialize()
        await self.results.initialize()