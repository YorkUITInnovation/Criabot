from CriadexSDK.ragflow_schemas import AuthCheckResponse

from app.core.security.get_api_key import GetApiKey, BadAPIKeyException


class GetApiKeyMaster(GetApiKey):

    async def execute(self) -> str:

        response: AuthCheckResponse = await self.get_auth()

        # Support both dict and model responses from SDK
        is_master = None
        if isinstance(response, dict):
            is_master = response.get("master")
        else:
            try:
                is_master = response.master
            except Exception:
                # Attempt verify() pattern if available
                verified = getattr(response, "verify", lambda: response)()
                is_master = getattr(verified, "master", None)

        if not is_master:
            raise BadAPIKeyException(
                status_code=401,
                detail="API key was not found or is not a master key."
            )

        return self.api_key
