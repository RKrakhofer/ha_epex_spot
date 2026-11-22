"""Unit tests for the Spotty source.

Only in the fork: upstream has no pytest setup, the pull request ships the
executable check script custom_components/epex_spot/test_spotty.py instead.
The package is loaded via conftest.py, so this exercises the production code.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

import aiohttp

from homeassistant.exceptions import ConfigEntryError

from epex_spot.EPEXSpot import Spotty

VIENNA = ZoneInfo("Europe/Vienna")
CONTRACT_ID = "11111111-2222-3333-4444-555555555555"


class FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class FakeGet:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        return self._response

    async def __aexit__(self, *args) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.requested_url = None
        self.requested_params = None

    def get(self, url: str, params=None) -> FakeGet:
        self.requested_url = url
        self.requested_params = params
        return FakeGet(self._response)


def fetch(payload, duration: int = 15, status: int = 200):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    session = FakeSession(FakeResponse(status, body))
    source = Spotty.Spotty(
        market_area="at",
        duration=duration,
        session=session,
        token=CONTRACT_ID,
    )
    asyncio.run(source.fetch())
    return source, session


def slots(start: datetime, count: int, step_minutes: int = 15):
    """Build a payload with absolute (UTC) stepping and local offsets."""
    entries = []
    current = start
    for index in range(count):
        entries.append(
            {
                "from": current.astimezone(VIENNA).isoformat(),
                "price": 20.0 + index * 0.01,
            }
        )
        current = current.astimezone(timezone.utc) + timedelta(minutes=step_minutes)
    return entries


def day_payload(day: str):
    """All 15 minute slots of one local day (96, or 92/100 on DST days)."""
    start = datetime.fromisoformat(day).replace(tzinfo=VIENNA)
    end = start + timedelta(days=1)
    entries = []
    current = start
    while current < end:
        entries.append(
            {
                "from": current.isoformat(),
                "price": 20.0 + len(entries) * 0.01,
            }
        )
        current = current.astimezone(timezone.utc) + timedelta(minutes=15)
    return entries


def test_slot_end_is_start_of_next_slot():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    source, _ = fetch(slots(start, 4))

    assert [entry.start_time for entry in source.marketdata] == [
        start.astimezone(VIENNA) + timedelta(minutes=15 * i) for i in range(4)
    ]
    for entry, next_entry in zip(source.marketdata, source.marketdata[1:]):
        assert entry.end_time == next_entry.start_time
    assert source.marketdata[-1].end_time == start + timedelta(minutes=60)


def test_last_slot_inherits_predecessor_length():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 3)
    payload[-1]["from"] = (start + timedelta(minutes=50)).isoformat()

    source, _ = fetch(payload)

    assert source.marketdata[-1].start_time == start + timedelta(minutes=50)
    assert source.marketdata[-1].end_time == start + timedelta(minutes=85)


def test_price_is_converted_from_ct_to_eur():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 2)
    payload[0]["price"] = 22.49
    payload[1]["price"] = -3.5

    source, _ = fetch(payload)

    assert source.marketdata[0].market_price_per_kwh == 0.2249
    assert source.marketdata[1].market_price_per_kwh == -0.035


def test_timestamps_are_normalized_to_vienna():
    payload = [
        {"from": "2026-07-01T10:00:00+00:00", "price": 10.0},
        {"from": "2026-07-01T10:15:00+00:00", "price": 11.0},
    ]

    source, _ = fetch(payload)

    assert source.marketdata[0].start_time.isoformat() == "2026-07-01T12:00:00+02:00"


def test_request_uses_normalized_contract_id_and_timezone():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    _, session = fetch(slots(start, 2))

    assert session.requested_url.endswith(
        "/api/prices/MARKET/11111111-2222-3333-4444-555555555555"
    )
    assert session.requested_params == {"timezone": "at"}


def test_average_from_15_to_60_minutes():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 8)
    for index, entry in enumerate(payload):
        entry["price"] = 20.0 + index

    source, _ = fetch(payload, duration=60)

    assert len(source.marketdata) == 2
    assert source.marketdata[0].market_price_per_kwh == 0.215
    assert source.marketdata[0].end_time == start + timedelta(minutes=60)
    assert source.marketdata[1].market_price_per_kwh == 0.255


def test_incomplete_trailing_group_is_dropped():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 6)

    source, _ = fetch(payload, duration=60)

    assert len(source.marketdata) == 1
    assert source.marketdata[0].end_time == start + timedelta(minutes=60)


def test_dst_spring_forward_day():
    payload = day_payload("2026-03-29")

    source, _ = fetch(payload)
    assert len(payload) == 92
    assert len(source.marketdata) == 92
    for entry, next_entry in zip(source.marketdata, source.marketdata[1:]):
        assert entry.end_time.timestamp() == next_entry.start_time.timestamp()

    hourly, _ = fetch(payload, duration=60)
    assert len(hourly.marketdata) == 23
    for entry, next_entry in zip(hourly.marketdata, hourly.marketdata[1:]):
        assert entry.end_time.timestamp() == next_entry.start_time.timestamp()


def test_dst_fall_back_day():
    payload = day_payload("2026-10-25")

    source, _ = fetch(payload)
    assert len(payload) == 100
    assert len(source.marketdata) == 100

    hourly, _ = fetch(payload, duration=60)
    assert len(hourly.marketdata) == 25
    for entry, next_entry in zip(hourly.marketdata, hourly.marketdata[1:]):
        assert entry.end_time.timestamp() == next_entry.start_time.timestamp()


def test_plain_text_200_is_a_contract_error_without_leaking_the_token():
    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=FakeSession(
            FakeResponse(200, f"Kein Abonnement gefunden für {CONTRACT_ID}")
        ),
        token=CONTRACT_ID,
    )

    with pytest.raises(ConfigEntryError) as error:
        asyncio.run(source.fetch())

    assert CONTRACT_ID not in str(error.value)


def test_404_means_series_not_in_contract():
    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=FakeSession(FakeResponse(404, "")),
        token=CONTRACT_ID,
    )

    with pytest.raises(Spotty.SpottySeriesError):
        asyncio.run(source.fetch())


def test_500_is_a_generic_error():
    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=FakeSession(FakeResponse(503, "")),
        token=CONTRACT_ID,
    )

    with pytest.raises(Spotty.SpottyError):
        asyncio.run(source.fetch())


def test_empty_payload_is_rejected():
    with pytest.raises(Spotty.SpottyError):
        fetch([])


def test_duplicate_timestamps_are_rejected():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 3)
    payload[1]["from"] = payload[0]["from"]

    with pytest.raises(Spotty.SpottyError):
        fetch(payload)


def test_timestamp_without_timezone_is_rejected():
    payload = [
        {"from": "2026-11-12T10:00:00", "price": 10.0},
        {"from": "2026-11-12T10:15:00", "price": 11.0},
    ]

    with pytest.raises(Spotty.SpottyError):
        fetch(payload)


def test_invalid_contract_id_is_rejected_before_request():
    for token in ("", "abc", "../../etc/passwd", None):
        with pytest.raises(ValueError):
            Spotty.Spotty(
                market_area="at",
                duration=15,
                session=FakeSession(FakeResponse(200, "[]")),
                token=token,
            )


def test_unsupported_duration_and_market_area_are_rejected():
    with pytest.raises(ValueError):
        Spotty.Spotty(
            market_area="de",
            duration=15,
            session=FakeSession(FakeResponse(200, "[]")),
            token=CONTRACT_ID,
        )

    with pytest.raises(ValueError):
        Spotty.Spotty(
            market_area="at",
            duration=30,
            session=FakeSession(FakeResponse(200, "[]")),
            token=CONTRACT_ID,
        )


def test_public_properties():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    source, _ = fetch(slots(start, 2))

    assert source.name == "Spotty Energie API"
    assert source.market_area == "at"
    assert source.duration == 15
    assert source.currency == "EUR"
    assert len(source.marketdata) == 2


def test_single_slot_keeps_base_duration():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)

    source, _ = fetch(slots(start, 1))

    assert len(source.marketdata) == 1
    assert source.marketdata[0].end_time == start + timedelta(minutes=15)


def test_gap_does_not_raise_the_detected_resolution():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    starts = [start, start + timedelta(minutes=15), start + timedelta(minutes=60)]

    assert Spotty.Spotty._detect_duration(starts) == 15

    payload = slots(start, 3)
    payload[2]["from"] = starts[2].isoformat()

    source, _ = fetch(payload)
    assert source.marketdata[0].end_time == starts[1]
    assert source.marketdata[1].end_time == starts[2]
    assert source.marketdata[-1].end_time == starts[2] + timedelta(minutes=45)


def test_shorter_duration_than_provided_is_kept():
    start = datetime(2026, 11, 12, 10, 0, tzinfo=VIENNA)
    payload = slots(start, 2, step_minutes=60)

    source, _ = fetch(payload, duration=15)

    assert len(source.marketdata) == 2
    assert source.marketdata[0].end_time == start + timedelta(minutes=60)


def test_401_is_a_contract_error():
    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=FakeSession(FakeResponse(401, "")),
        token=CONTRACT_ID,
    )

    with pytest.raises(ConfigEntryError):
        asyncio.run(source.fetch())


def test_connection_error_is_wrapped():
    class FailingSession:
        def get(self, url, params=None):
            raise aiohttp.ClientError("boom")

    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=FailingSession(),
        token=CONTRACT_ID,
    )

    with pytest.raises(Spotty.SpottyError):
        asyncio.run(source.fetch())


def test_timeout_is_wrapped():
    class TimingOutSession:
        def get(self, url, params=None):
            raise asyncio.TimeoutError()

    source = Spotty.Spotty(
        market_area="at",
        duration=15,
        session=TimingOutSession(),
        token=CONTRACT_ID,
    )

    with pytest.raises(Spotty.SpottyError, match="Error connecting"):
        asyncio.run(source.fetch())


def test_non_finite_and_malformed_prices_are_rejected():
    payload = [{"from": "2026-11-12T10:00:00+01:00", "price": float("nan")}]
    with pytest.raises(Spotty.SpottyError):
        fetch(payload)

    with pytest.raises(Spotty.SpottyError):
        fetch([{"price": 10.0}])

    with pytest.raises(Spotty.SpottyError):
        fetch([{"from": "not-a-date", "price": 10.0}])

    with pytest.raises(Spotty.SpottyError):
        fetch(["nope"])
