import json

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class StatusMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):

        response: Response = await call_next(request)

        if response.headers.get('content-type') == 'application/json':
            return await self.handle_json_status(request, response)

        return response

    async def handle_json_status(self, request: Request, response: Response):
        binary = b''

        # noinspection PyUnresolvedReferences
        async for data in response.body_iterator:
            binary += data

        body: dict = json.loads(binary.decode())

        if "error" in body and not self.stack_trace_enabled(request):
            del body["error"]

        return JSONResponse(
            content=body,
            status_code=body.get('status', response.status_code)
        )

    @classmethod
    def stack_trace_enabled(cls, request: Request) -> bool:
        return request.headers.get("x-api-stacktrace", "") == "true"
