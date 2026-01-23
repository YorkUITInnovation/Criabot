from sqlalchemy.ext.asyncio import AsyncEngine

from criabot.database.bots.tables.bot_params import BotParametersAPI
from criabot.database.bots.tables.bot_parents import BotParentsAPI
from criabot.database.bots.tables.bot_api_keys import BotApiKeysAPI
from criabot.database.bots.tables.bots import BotsAPI
from criabot.database.table import BaseDatabaseAPI


class BotDatabaseAPI(BaseDatabaseAPI):
    """
    API for interfacing with the index in the database

    """

    def __init__(self, engine: AsyncEngine):
        """
        Instantiate the index database API
        :param engine: SQL Pool

        """

        super().__init__(engine)

        self.bots: BotsAPI = BotsAPI(engine)
        self.bot_params: BotParametersAPI = BotParametersAPI(engine)
        self.bot_parents: BotParentsAPI = BotParentsAPI(engine)
        self.bot_api_keys: BotApiKeysAPI = BotApiKeysAPI(engine)

    async def initialize(self) -> None:
        """
        Initialize the database to create objects if they don't exist

        :return: None

        """

        await self.bots.initialize()
        await self.bot_params.initialize()
        await self.bot_parents.initialize()
        await self.bot_api_keys.initialize()
