from CriadexSDK.ragflow_schemas import AuthCheckResponse

from app.core.security.get_api_key import GetApiKey, BadAPIKeyException


class GetApiKeyMaster(GetApiKey):

    async def execute(self) -> str:

        response: AuthCheckResponse = await self.get_auth()

        if not response.master:
            raise BadAPIKeyException(
                status_code=401,
                detail="API key was not found or is not a master key."
            )

        return self.api_key
