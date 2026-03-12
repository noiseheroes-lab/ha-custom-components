"""Octopus Energy Italy — Kraken GraphQL API client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

from .const import KRAKEN_GQL, TOKEN_TTL_SECONDS

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL mutations / queries
# ---------------------------------------------------------------------------

_OBTAIN_TOKEN = """
mutation ObtainToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    refreshToken
  }
}
"""

_ACCOUNT_QUERY = """
query AccountData($accountNumber: String!, $startAt: DateTime!, $endAt: DateTime!, $pod: String!) {
  account(accountNumber: $accountNumber) {
    id
    balance
    properties {
      id
      address
      electricitySupplyPoints {
        id
        pod
        status
        product {
          code
          displayName
          prices {
            consumptionCharge
            annualStandingCharge
          }
        }
        agreements(first: 1) {
          edges {
            node {
              validFrom
              validTo
            }
          }
        }
      }
      gasSupplyPoints {
        id
        pdr
        status
        product {
          code
          displayName
          prices {
            consumptionCharge
            annualStandingCharge
          }
        }
      }
      monthlyMeasurements: measurements(
        utilityFilters: [{
          electricityFilters: {
            marketSupplyPointId: $pod
            readingFrequencyType: MONTH_INTERVAL
            readingDirection: CONSUMPTION
          }
        }]
        startAt: $startAt
        endAt: $endAt
        first: 24
      ) {
        edges {
          node {
            readAt
            value
            unit
          }
        }
      }
      dailyMeasurements: measurements(
        utilityFilters: [{
          electricityFilters: {
            marketSupplyPointId: $pod
            readingFrequencyType: DAY_INTERVAL
            readingDirection: CONSUMPTION
          }
        }]
        startAt: $startAt
        endAt: $endAt
        first: 45
      ) {
        edges {
          node {
            readAt
            value
            unit
          }
        }
      }
    }
  }
}
"""

_GAS_READINGS = """
query GasReadings($accountNumber: String!, $pdr: String!) {
  gasMeterReadings(accountNumber: $accountNumber, pdr: $pdr, first: 15) {
    edges {
      node {
        readingDate
        readingType
        readingSource
        consumptionValue
      }
    }
  }
}
"""

_VIEWER_ACCOUNTS = """
query ViewerAccounts {
  viewer {
    accounts {
      ... on AccountType {
        number
        balance
        billingName
        properties {
          id
          address
          electricitySupplyPoints {
            id
            pod
            status
            supplyStartDate
          }
          gasSupplyPoints {
            id
            pdr
            status
          }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SupplyPoint:
    id: str
    external_id: str       # POD or PDR
    supply_type: str       # "electricity" | "gas"
    status: str
    product_name: str | None = None
    rate: float | None = None           # €/kWh or €/Smc
    standing_charge: float | None = None  # €/year


@dataclass
class OctopusAccountInfo:
    """Returned by list_accounts() for config flow."""
    account_number: str
    display_name: str
    address: str
    electricity_pod: str | None
    gas_pdr: str | None


@dataclass
class OctopusData:
    """Full data snapshot published to HA."""
    account_number: str
    account_balance: float = 0.0          # € (positive = credit)

    # Electricity
    electricity_pod: str | None = None
    electricity_rate: float | None = None
    electricity_standing_year: float | None = None
    electricity_yesterday_kwh: float | None = None
    electricity_monthly_kwh: float | None = None
    electricity_yearly_kwh: float | None = None

    # Gas
    gas_pdr: str | None = None
    gas_rate: float | None = None
    gas_standing_year: float | None = None
    gas_monthly_smc: float | None = None
    gas_yearly_smc: float | None = None

    last_updated: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class OctopusEnergyItalyAPI:
    """Kraken GraphQL client for Octopus Energy Italy."""

    def __init__(self, email: str, password: str, account_number: str) -> None:
        self._email = email
        self._password = password
        self.account_number = account_number

        self._token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0.0

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Ensure we have a valid JWT token."""
        now = time.monotonic()
        if self._token and now < self._token_expiry - 60:
            return

        # Try refresh token first (cheaper)
        if self._refresh_token:
            try:
                self._do_auth({"refreshToken": self._refresh_token})
                return
            except AuthError:
                _LOGGER.debug("Refresh token expired, falling back to password auth")
                self._refresh_token = None

        self._do_auth({"email": self._email, "password": self._password})

    def _do_auth(self, input_data: dict) -> None:
        data = self._gql_raw(_OBTAIN_TOKEN, {"input": input_data})
        result = data.get("obtainKrakenToken", {})
        token = result.get("token")
        if not token:
            raise AuthError("No token in response")
        self._token = token
        self._refresh_token = result.get("refreshToken", self._refresh_token)
        self._token_expiry = time.monotonic() + TOKEN_TTL_SECONDS
        self._session.headers["Authorization"] = token
        _LOGGER.debug("Authenticated to Kraken API")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def test_credentials(self) -> bool:
        """Test email/password and return True if valid."""
        try:
            self._do_auth({"email": self._email, "password": self._password})
            return True
        except (AuthError, APIError):
            return False

    def list_accounts(self) -> list[OctopusAccountInfo]:
        """Return all accounts for the authenticated user (used in config flow)."""
        self.authenticate()
        data = self._gql(_VIEWER_ACCOUNTS)
        accounts = []
        for acc in data.get("viewer", {}).get("accounts", []):
            number = acc.get("number", "")
            props = acc.get("properties", [])
            address = props[0].get("address", "") if props else ""
            pod = None
            pdr = None
            if props:
                esp = props[0].get("electricitySupplyPoints", [])
                gsp = props[0].get("gasSupplyPoints", [])
                if esp:
                    pod = esp[0].get("pod")
                if gsp:
                    pdr = gsp[0].get("pdr")
            accounts.append(OctopusAccountInfo(
                account_number=number,
                display_name=acc.get("billingName") or number,
                address=address,
                electricity_pod=pod,
                gas_pdr=pdr,
            ))
        return accounts

    def fetch_data(self) -> OctopusData:
        """Fetch all data for the account and return OctopusData."""
        self.authenticate()

        # Determine date range: start of current year → today
        today = date.today()
        start = f"{today.year}-01-01T00:00:00Z"
        end = f"{today.isoformat()}T23:59:59Z"

        # We need POD first — get it from a quick account query
        pod = self._get_pod()

        result = OctopusData(account_number=self.account_number)

        # --- Main account query ---
        try:
            data = self._gql(_ACCOUNT_QUERY, {
                "accountNumber": self.account_number,
                "startAt": start,
                "endAt": end,
                "pod": pod or "UNKNOWN",
            })
            account = data.get("account", {})
        except APIError as e:
            _LOGGER.error("Failed to fetch account data: %s", e)
            return result

        # Balance: HA in cents → convert to EUR (negative = credit in Kraken)
        balance_cents = account.get("balance", 0)
        result.account_balance = round(-balance_cents / 100, 2)  # negate: negative balance = credit for us

        props = account.get("properties", [])
        if not props:
            return result

        prop = props[0]

        # --- Electricity supply point ---
        for sp in prop.get("electricitySupplyPoints", []):
            result.electricity_pod = sp.get("pod")
            prices = sp.get("product", {}).get("prices", {}) or {}
            charge = prices.get("consumptionCharge")
            standing = prices.get("annualStandingCharge")
            if charge:
                result.electricity_rate = float(charge)
            if standing:
                result.electricity_standing_year = float(standing)
            break  # only first supply point

        # --- Gas supply point ---
        for sp in prop.get("gasSupplyPoints", []):
            result.gas_pdr = sp.get("pdr")
            prices = sp.get("product", {}).get("prices", {}) or {}
            charge = prices.get("consumptionCharge")
            standing = prices.get("annualStandingCharge")
            if charge:
                result.gas_rate = float(charge)
            if standing:
                result.gas_standing_year = float(standing)
            break

        # --- Electricity monthly measurements ---
        monthly: dict[tuple[int, int], float] = {}
        for edge in prop.get("monthlyMeasurements", {}).get("edges", []):
            node = edge["node"]
            try:
                dt = datetime.fromisoformat(node["readAt"].replace("Z", "+00:00"))
                kwh = float(node["value"])
                monthly[(dt.year, dt.month)] = kwh
            except (ValueError, KeyError):
                continue

        now_key = (today.year, today.month)
        if now_key in monthly:
            result.electricity_monthly_kwh = round(monthly[now_key], 2)
        elif monthly:
            latest = max(monthly.keys())
            result.electricity_monthly_kwh = round(monthly[latest], 2)

        result.electricity_yearly_kwh = round(
            sum(v for (y, _), v in monthly.items() if y == today.year), 2
        )

        # --- Electricity daily measurements (yesterday) ---
        daily_edges = prop.get("dailyMeasurements", {}).get("edges", [])
        if daily_edges:
            # Sort by date descending, pick the most recent complete day
            daily: list[tuple[date, float]] = []
            for edge in daily_edges:
                node = edge["node"]
                try:
                    dt = datetime.fromisoformat(node["readAt"].replace("Z", "+00:00")).date()
                    kwh = float(node["value"])
                    daily.append((dt, kwh))
                except (ValueError, KeyError):
                    continue
            daily.sort(key=lambda x: x[0], reverse=True)
            # Skip today (incomplete), take first full day
            for d, kwh in daily:
                if d < today:
                    result.electricity_yesterday_kwh = round(kwh, 3)
                    break

        # --- Gas meter readings ---
        try:
            gas_data = self._gql(_GAS_READINGS, {
                "accountNumber": self.account_number,
                "pdr": result.gas_pdr or "",
            })
            gas_months = self._parse_gas_readings(gas_data)

            if now_key in gas_months:
                result.gas_monthly_smc = round(gas_months[now_key], 2)
            elif gas_months:
                latest = max(gas_months.keys())
                result.gas_monthly_smc = round(gas_months[latest], 2)

            result.gas_yearly_smc = round(
                sum(v for (y, _), v in gas_months.items() if y == today.year), 2
            )
        except APIError as e:
            _LOGGER.warning("Could not fetch gas readings: %s", e)

        _LOGGER.debug(
            "Fetched data for %s: elec=%.2f kWh/month, gas=%.2f Smc/month",
            self.account_number,
            result.electricity_monthly_kwh or 0,
            result.gas_monthly_smc or 0,
        )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_pod(self) -> str | None:
        """Quick fetch to get the electricity POD."""
        try:
            data = self._gql(_VIEWER_ACCOUNTS)
            for acc in data.get("viewer", {}).get("accounts", []):
                if acc.get("number") == self.account_number:
                    for prop in acc.get("properties", []):
                        for sp in prop.get("electricitySupplyPoints", []):
                            return sp.get("pod")
        except APIError:
            pass
        return None

    def _parse_gas_readings(self, data: dict) -> dict[tuple[int, int], float]:
        """Convert cumulative gas meter readings to monthly deltas."""
        readings: list[tuple[date, float]] = []
        for edge in data.get("gasMeterReadings", {}).get("edges", []):
            node = edge["node"]
            try:
                d = datetime.strptime(node["readingDate"], "%Y-%m-%d").date()
                val = float(node["consumptionValue"])
                readings.append((d, val))
            except (ValueError, KeyError):
                continue

        readings.sort(key=lambda x: x[0])
        months: dict[tuple[int, int], float] = {}
        for i in range(1, len(readings)):
            prev_date, prev_val = readings[i - 1]
            curr_date, curr_val = readings[i]
            delta = curr_val - prev_val
            if delta >= 0:
                months[(curr_date.year, curr_date.month)] = round(delta, 3)
        return months

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        """Execute a GraphQL query and return data dict."""
        data = self._gql_raw(query, variables)
        return data

    def _gql_raw(self, query: str, variables: dict | None = None) -> dict:
        """POST a GraphQL query and return the data payload."""
        try:
            resp = self._session.post(
                KRAKEN_GQL,
                json={"query": query, "variables": variables or {}},
                timeout=20,
            )
            resp.raise_for_status()
        except requests.Timeout as e:
            raise APIError(f"Request timed out: {e}") from e
        except requests.RequestException as e:
            raise APIError(f"HTTP error: {e}") from e

        payload = resp.json()

        if errors := payload.get("errors"):
            codes = [e.get("extensions", {}).get("errorCode", "") for e in errors]
            if any(c in ("KT-CT-1124", "KT-CT-1111") for c in codes):
                self._token = None  # force re-auth next call
                raise AuthError("Token expired or unauthorized")
            messages = "; ".join(e.get("message", "unknown") for e in errors)
            if not payload.get("data"):
                raise APIError(f"GraphQL errors: {messages}")
            _LOGGER.debug("GraphQL partial errors: %s", messages)

        return payload.get("data", {})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OctopusEnergyError(Exception):
    """Base exception."""


class AuthError(OctopusEnergyError):
    """Authentication failed."""


class APIError(OctopusEnergyError):
    """API call failed."""
