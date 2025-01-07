import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

import backoff
from aiohttp import (
    ClientConnectionError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
)
from base_client._exceptions import BaseError, ClientError
from base_client._settnigs import MAX_RETRY, ClientConfig
from multidict import CIMultiDictProxy
from pydantic import BaseModel, ValidationError

T_SCHEMA = TypeVar("T_SCHEMA", bound=BaseModel)
T_CONFIG = TypeVar("T_CONFIG", bound=ClientConfig)


@dataclass
class Response:
    headers: CIMultiDictProxy[str]
    body: bytes | None


class BaseClient(Generic[T_CONFIG]):

    def __init__(self, config: T_CONFIG) -> None:
        self._config = config
        self.logger = logging.getLogger(self.name)
        self.session = ClientSession(timeout=ClientTimeout(total=self.config.CLIENT_TIMEOUT))

    @property
    def config(self) -> T_CONFIG:
        return self._config

    async def __aenter__(self) -> "BaseClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:  # noqa: ANN001
        await self.stop()

    async def stop(self) -> None:
        await self.session.close()

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @property
    def base_path(self) -> str:
        return self.config.HOST.strip("/")

    def get_path(self, url: str) -> str:
        url = url.lstrip("/")
        return f"{self.base_path}/{url}"

    @staticmethod
    def load_schema(data: bytes, schema: type[T_SCHEMA]) -> T_SCHEMA:
        try:
            return schema.parse_raw(data)
        except ValidationError as exc:
            errors = [f"'{'.'.join(error['loc'])}' - {error['msg']}" for error in exc.errors()]
            raise BaseError(
                f"ValidationError in '{schema.__name__}': {', '.join(errors)}. input: {data}"
            ) from exc

    @staticmethod
    async def _raise_for_status(resp: ClientResponse) -> None:
        # good statuses -> return
        if resp.status in (200, 201, 202, 204):
            return

        content = await resp.text()
        error_msg = str(resp).replace("\n", " ")

        # request exceptions -> raise up
        if 400 <= resp.status < 500:  # noqa: PLR2004
            raise BaseError(error_msg, content)

        # other unhandled exceptions -> retry (backoff)
        raise ClientError(error_msg, content)

    async def _handle_response(self, resp: ClientResponse, request_info: dict) -> Response:
        await self._raise_for_status(resp)
        body = await resp.read()
        self.logger.debug(
            "Response: content_type: '%s', content: %s, request_info: %s",
            resp.content_type,
            body.decode("utf-8") if body else None,
            request_info,
        )
        return Response(headers=resp.headers, body=body)

    @backoff.on_exception(
        backoff.expo,
        (ClientConnectionError, ClientError),
        max_tries=MAX_RETRY,
    )
    async def _perform_request(self, method: str, url: str, **kwargs: str | bool) -> Response:
        kwargs.setdefault("ssl", self.config.SSL_VERIFY)
        start_time = monotonic()
        status_code = 500
        request_info = {
            "method": method.upper(),
            "request_id": str(uuid.uuid4()),
            "url": url,
        }
        try:
            self.logger.debug("Request: %s", request_info)
            async with self.session.request(method, url, **kwargs) as resp:
                status_code = resp.status
                return await self._handle_response(resp, request_info)
        except asyncio.TimeoutError as exc:
            self.logger.error("TimeoutError request_info: %s", request_info)
            raise ClientError("TimeoutError", json.dumps(request_info)) from exc
        except Exception as exc:
            self.logger.exception(
                "Request error [%s]: %s request_info: %s",
                status_code,
                str(exc),
                request_info,
            )
            raise
        finally:
            elapsed = 1000.0 * (monotonic() - start_time)
            duration = "{:0.3f} ms".format(elapsed)
            self.logger.info("Response: [%s] Duration: %s: %s", status_code, duration, request_info)