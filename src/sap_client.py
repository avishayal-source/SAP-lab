"""
SAP Business Accelerator Hub sandbox OData V2 client (GET only).

Documented sandbox patterns (do not call until SAP_API_KEY is configured):

  Host:    https://sandbox.api.sap.com/s4hanacloud
  Service: /sap/opu/odata/sap/API_SALES_ORDER_SRV
  Auth:    APIKey header (value from .env via python-dotenv)

Endpoints:
  GET /A_SalesOrder
  GET /A_SalesOrder('{SalesOrder}')/to_Item
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Transient statuses worth retrying on the sandbox
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SapClientError(Exception):
    """Raised when a sandbox GET request fails after retries."""


class SapClient:
    """Thin GET-only client for the Sales Order OData V2 sandbox API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        service_path: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        load_dotenv()

        self.api_key = api_key or os.getenv("SAP_API_KEY", "")
        self.base_url = (base_url or os.getenv("SAP_BASE_URL", "")).rstrip("/")
        self.service_path = (
            service_path
            or os.getenv("SAP_SALES_ORDER_PATH", "/sap/opu/odata/sap/API_SALES_ORDER_SRV")
        ).rstrip("/")
        self.timeout = float(timeout if timeout is not None else os.getenv("SAP_REQUEST_TIMEOUT", "30"))
        self.max_retries = int(
            max_retries if max_retries is not None else os.getenv("SAP_MAX_RETRIES", "3")
        )

        if not self.api_key:
            raise SapClientError(
                "SAP_API_KEY is missing. Copy .env.example to .env and set your sandbox API key."
            )
        if not self.base_url:
            raise SapClientError("SAP_BASE_URL is missing. Set it in .env (see .env.example).")

        # Never log the API key value
        self._session = requests.Session()
        self._session.headers.update(
            {
                "APIKey": self.api_key,
                "Accept": "application/json",
            }
        )

    @property
    def service_root(self) -> str:
        return f"{self.base_url}{self.service_path}"

    def _get(self, relative_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Perform a GET against the sandbox with timeout and retries.

        relative_path examples:
          /A_SalesOrder
          /A_SalesOrder('5000001')/to_Item
        """
        url = f"{self.service_root}{relative_path}"
        query = {"$format": "json", **(params or {})}
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                # Log URL only — never log headers (contain APIKey)
                logger.info("GET %s (attempt %s/%s)", url, attempt, self.max_retries)
                response = self._session.get(url, params=query, timeout=self.timeout)

                if response.status_code in _RETRYABLE_STATUS:
                    logger.warning(
                        "Transient status %s for %s", response.status_code, url
                    )
                    last_error = SapClientError(
                        f"Transient HTTP {response.status_code} for {url}"
                    )
                    self._backoff(attempt)
                    continue

                if response.status_code >= 400:
                    # Do not include response body if it might echo credentials
                    raise SapClientError(
                        f"Sandbox GET failed with HTTP {response.status_code} for {url}"
                    )

                payload = response.json()
                if not isinstance(payload, dict):
                    raise SapClientError(f"Unexpected JSON payload type from {url}")
                return payload

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                logger.warning("Transient network error on %s: %s", url, type(exc).__name__)
                self._backoff(attempt)
            except ValueError as exc:
                # JSON decode errors are not retryable
                raise SapClientError(f"Invalid JSON from {url}") from exc

        raise SapClientError(
            f"Exhausted {self.max_retries} retries for {url}"
        ) from last_error

    @staticmethod
    def _backoff(attempt: int) -> None:
        # Simple exponential backoff; kept short for a learning skeleton
        time.sleep(min(2 ** (attempt - 1), 8))

    def get_sales_orders(self, *, top: int | None = None, filter_expr: str | None = None) -> dict[str, Any]:
        """
        GET A_SalesOrder — sales order headers.

        Example:
          GET .../API_SALES_ORDER_SRV/A_SalesOrder?$format=json&$top=10
        """
        params: dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        if filter_expr:
            params["$filter"] = filter_expr
        return self._get("/A_SalesOrder", params=params)

    def get_sales_order_items(self, sales_order_id: str) -> dict[str, Any]:
        """
        GET A_SalesOrder('{SalesOrder}')/to_Item — line items for one header.

        SalesOrder is treated as a string identifier (SAP key), never coerced to int.
        Example:
          GET .../API_SALES_ORDER_SRV/A_SalesOrder('5000001')/to_Item?$format=json
        """
        # Preserve identifier as string; quote for OData key literal safety
        key = quote(str(sales_order_id), safe="")
        return self._get(f"/A_SalesOrder('{key}')/to_Item")
