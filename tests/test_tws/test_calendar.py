"""Tests fuer broker_gateway.tws.calendar.TWSCalendarService.

Decken folgende Faelle ab (Coverage-Ziel >= 90 %):

1. cached_exchanges liefert die statische Liste aller Exchanges (sortiert).
2. time_zone_for + description_for fuer bekannte und unbekannte Exchanges.
3. get(...) liefert 14 Tage ab today_provider.
4. Wochentage haben pre/rth/post-Sessions mit korrekten ET-Zeiten.
5. Wochenenden sind is_holiday=True mit leeren sessions.
6. Holidays aus 2026 werden als is_holiday=True erkannt.
7. Half-Days schliessen RTH um 13:00 ohne post-Session.
8. exchange_id wird normalisiert (case + whitespace).
9. Unbekannte exchange_id liefert 404 + exchange_not_found.
10. Leere exchange_id liefert 422.
11. symbol-Parameter wird ignoriert (API-Parity).
12. Timezone-Anhang an Sessions ist America/New_York.
13. Eigene Datendatei kann via data_path injiziert werden.
14. Defekte Datendateien werfen aussagekraeftige Fehler.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from broker_gateway.tws.calendar import (
    CalendarDay,
    CalendarSession,
    ExchangeCalendar,
    TWSCalendarService,
)


_FIXED_TODAY = date(2026, 5, 11)  # Montag, kein Holiday
_NY_TZ = ZoneInfo("America/New_York")


def _fixed_today() -> date:
    return _FIXED_TODAY


@pytest.fixture
def service() -> TWSCalendarService:
    return TWSCalendarService(today_provider=_fixed_today)


# ---------------------------------------------------------------------------
# cached_exchanges + time_zone_for + description_for
# ---------------------------------------------------------------------------


class TestCachedExchanges:
    def test_returns_static_list_sorted(
        self, service: TWSCalendarService
    ) -> None:
        result = service.cached_exchanges
        assert result == sorted(result)
        assert "NASDAQ" in result
        assert "NYSE" in result
        assert "ARCA" in result

    def test_is_never_empty_in_static_mode(
        self, service: TWSCalendarService
    ) -> None:
        # Das ist der Kern des 503-Fixes: TWS-Backend liefert nie leer,
        # damit GET /v1/exchanges nie HTTP 503+calendar_unavailable
        # ausspielt - der 503-Pfad bleibt cp-Backend mit DNS-Fehler
        # vorbehalten.
        assert len(service.cached_exchanges) >= 5


class TestTimeZoneFor:
    def test_known_exchange_returns_iana_zone(
        self, service: TWSCalendarService
    ) -> None:
        assert service.time_zone_for("NASDAQ") == "America/New_York"
        assert service.time_zone_for("NYSE") == "America/New_York"

    def test_case_insensitive(self, service: TWSCalendarService) -> None:
        assert service.time_zone_for("nasdaq") == "America/New_York"
        assert service.time_zone_for(" Nasdaq ") == "America/New_York"

    def test_unknown_exchange_returns_none(
        self, service: TWSCalendarService
    ) -> None:
        assert service.time_zone_for("LSE") is None
        assert service.time_zone_for("XETRA") is None


class TestDescriptionFor:
    def test_known_exchange_returns_description(
        self, service: TWSCalendarService
    ) -> None:
        assert service.description_for("NASDAQ") == "NASDAQ Stock Market"
        assert service.description_for("NYSE") == "New York Stock Exchange"

    def test_unknown_exchange_returns_none(
        self, service: TWSCalendarService
    ) -> None:
        assert service.description_for("LSE") is None


# ---------------------------------------------------------------------------
# get(...) - Calendar-Berechnung
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_returns_14_days_starting_today(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        assert isinstance(cal, ExchangeCalendar)
        assert cal.exchange_id == "NASDAQ"
        assert cal.time_zone == "America/New_York"
        assert len(cal.days) == 14
        assert cal.days[0].date == _FIXED_TODAY
        assert cal.days[-1].date == date(2026, 5, 24)

    @pytest.mark.asyncio
    async def test_normalizes_exchange_id_case_insensitively(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get(" nasdaq ")
        assert cal.exchange_id == "NASDAQ"

    @pytest.mark.asyncio
    async def test_unknown_exchange_raises_404(
        self, service: TWSCalendarService
    ) -> None:
        with pytest.raises(HTTPException) as info:
            await service.get("LSE")
        assert info.value.status_code == 404
        assert info.value.detail["code"] == "exchange_not_found"

    @pytest.mark.asyncio
    async def test_empty_exchange_id_raises_422(
        self, service: TWSCalendarService
    ) -> None:
        with pytest.raises(HTTPException) as info:
            await service.get("")
        assert info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_whitespace_only_exchange_id_raises_422(
        self, service: TWSCalendarService
    ) -> None:
        with pytest.raises(HTTPException) as info:
            await service.get("   ")
        assert info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_symbol_parameter_is_ignored(
        self, service: TWSCalendarService
    ) -> None:
        # Static-Strategie braucht kein Probe-Symbol; der Parameter
        # existiert nur fuer API-Parity.
        cal_a = await service.get("NASDAQ")
        cal_b = await service.get("NASDAQ", symbol="AAPL")
        cal_c = await service.get("NASDAQ", symbol="IBM")
        assert cal_a == cal_b == cal_c


class TestRegularDay:
    @pytest.mark.asyncio
    async def test_monday_has_three_sessions(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        # Index 0 = Montag 2026-05-11 (kein Holiday).
        day = cal.days[0]
        assert day.date == _FIXED_TODAY
        assert day.is_holiday is False
        assert len(day.sessions) == 3
        types = [s.type for s in day.sessions]
        assert types == ["pre", "rth", "post"]

    @pytest.mark.asyncio
    async def test_rth_session_is_0930_to_1600_et(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        rth = next(s for s in cal.days[0].sessions if s.type == "rth")
        assert rth.opens_at == datetime(2026, 5, 11, 9, 30, tzinfo=_NY_TZ)
        assert rth.closes_at == datetime(2026, 5, 11, 16, 0, tzinfo=_NY_TZ)

    @pytest.mark.asyncio
    async def test_pre_session_is_0400_to_0930_et(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        pre = next(s for s in cal.days[0].sessions if s.type == "pre")
        assert pre.opens_at == datetime(2026, 5, 11, 4, 0, tzinfo=_NY_TZ)
        assert pre.closes_at == datetime(2026, 5, 11, 9, 30, tzinfo=_NY_TZ)

    @pytest.mark.asyncio
    async def test_post_session_is_1600_to_2000_et(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        post = next(s for s in cal.days[0].sessions if s.type == "post")
        assert post.opens_at == datetime(2026, 5, 11, 16, 0, tzinfo=_NY_TZ)
        assert post.closes_at == datetime(2026, 5, 11, 20, 0, tzinfo=_NY_TZ)


class TestWeekend:
    @pytest.mark.asyncio
    async def test_saturday_is_holiday(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        # 2026-05-16 ist Samstag (Index 5 ab Montag 2026-05-11).
        saturday = next(d for d in cal.days if d.date == date(2026, 5, 16))
        assert saturday.is_holiday is True
        assert saturday.sessions == []

    @pytest.mark.asyncio
    async def test_sunday_is_holiday(
        self, service: TWSCalendarService
    ) -> None:
        cal = await service.get("NASDAQ")
        sunday = next(d for d in cal.days if d.date == date(2026, 5, 17))
        assert sunday.is_holiday is True
        assert sunday.sessions == []


class TestHoliday:
    @pytest.mark.asyncio
    async def test_memorial_day_2026_is_holiday(self) -> None:
        # 2026-05-25 = Memorial Day.
        service = TWSCalendarService(today_provider=lambda: date(2026, 5, 18))
        cal = await service.get("NASDAQ")
        memorial = next(d for d in cal.days if d.date == date(2026, 5, 25))
        assert memorial.is_holiday is True
        assert memorial.sessions == []

    @pytest.mark.asyncio
    async def test_juneteenth_2026_is_holiday(self) -> None:
        service = TWSCalendarService(today_provider=lambda: date(2026, 6, 15))
        cal = await service.get("NASDAQ")
        juneteenth = next(d for d in cal.days if d.date == date(2026, 6, 19))
        assert juneteenth.is_holiday is True
        assert juneteenth.sessions == []

    @pytest.mark.asyncio
    async def test_thanksgiving_2026_is_holiday(self) -> None:
        service = TWSCalendarService(today_provider=lambda: date(2026, 11, 23))
        cal = await service.get("NASDAQ")
        thanksgiving = next(
            d for d in cal.days if d.date == date(2026, 11, 26)
        )
        assert thanksgiving.is_holiday is True
        assert thanksgiving.sessions == []

    @pytest.mark.asyncio
    async def test_holidays_are_consistent_across_exchanges(self) -> None:
        # NYSE und NASDAQ teilen die US-Holidays.
        service = TWSCalendarService(today_provider=lambda: date(2026, 5, 18))
        cal_nasdaq = await service.get("NASDAQ")
        cal_nyse = await service.get("NYSE")
        holidays_nasdaq = {d.date for d in cal_nasdaq.days if d.is_holiday}
        holidays_nyse = {d.date for d in cal_nyse.days if d.is_holiday}
        assert holidays_nasdaq == holidays_nyse


class TestHalfDay:
    @pytest.mark.asyncio
    async def test_black_friday_2026_is_early_close(self) -> None:
        # 2026-11-27 = Black Friday, early close 13:00 ET.
        service = TWSCalendarService(today_provider=lambda: date(2026, 11, 23))
        cal = await service.get("NASDAQ")
        black_friday = next(
            d for d in cal.days if d.date == date(2026, 11, 27)
        )
        assert black_friday.is_holiday is False
        assert len(black_friday.sessions) == 2  # pre + rth, kein post
        types = {s.type for s in black_friday.sessions}
        assert types == {"pre", "rth"}
        rth = next(s for s in black_friday.sessions if s.type == "rth")
        assert rth.closes_at == datetime(
            2026, 11, 27, 13, 0, tzinfo=_NY_TZ
        )

    @pytest.mark.asyncio
    async def test_christmas_eve_2026_is_early_close(self) -> None:
        service = TWSCalendarService(today_provider=lambda: date(2026, 12, 18))
        cal = await service.get("NASDAQ")
        eve = next(d for d in cal.days if d.date == date(2026, 12, 24))
        assert eve.is_holiday is False
        rth = next(s for s in eve.sessions if s.type == "rth")
        assert rth.closes_at == datetime(2026, 12, 24, 13, 0, tzinfo=_NY_TZ)


# ---------------------------------------------------------------------------
# Datendatei-Handling
# ---------------------------------------------------------------------------


class TestCustomDataPath:
    def test_custom_data_path_is_used(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.json"
        custom.write_text(
            json.dumps(
                {
                    "version": "test",
                    "default_hours": {
                        "regular": {"open": "09:00", "close": "17:00"},
                    },
                    "exchanges": [
                        {
                            "exchange_id": "TEST",
                            "description": "Test Exchange",
                            "time_zone": "Europe/Berlin",
                        }
                    ],
                    "holidays_us": {},
                }
            ),
            encoding="utf-8",
        )
        service = TWSCalendarService(
            data_path=custom,
            today_provider=lambda: date(2026, 5, 11),
        )
        assert service.cached_exchanges == ["TEST"]
        assert service.time_zone_for("TEST") == "Europe/Berlin"

    @pytest.mark.asyncio
    async def test_custom_hours_take_effect(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.json"
        custom.write_text(
            json.dumps(
                {
                    "version": "test",
                    "default_hours": {
                        "regular": {"open": "09:00", "close": "17:00"},
                    },
                    "exchanges": [
                        {
                            "exchange_id": "TEST",
                            "description": "Test Exchange",
                            "time_zone": "Europe/Berlin",
                        }
                    ],
                    "holidays_us": {},
                }
            ),
            encoding="utf-8",
        )
        service = TWSCalendarService(
            data_path=custom,
            today_provider=lambda: date(2026, 5, 11),
        )
        cal = await service.get("TEST")
        rth = cal.days[0].sessions[0]
        assert rth.type == "rth"
        assert rth.opens_at.hour == 9
        assert rth.closes_at.hour == 17
        # Pre und Post fehlen in der Test-Datei.
        assert len(cal.days[0].sessions) == 1


class TestDataLoading:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        with pytest.raises(FileNotFoundError):
            TWSCalendarService(data_path=missing)

    def test_non_object_payload_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="muss ein Objekt sein"):
            TWSCalendarService(data_path=path)

    def test_missing_exchanges_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"default_hours": {"regular": {"open": "09:30", "close": "16:00"}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="exchanges"):
            TWSCalendarService(data_path=path)

    def test_missing_default_hours_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"exchanges": [{"exchange_id": "X", "time_zone": "UTC"}]}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="default_hours"):
            TWSCalendarService(data_path=path)


class TestHHMMParsing:
    def test_short_format_is_padded(self, tmp_path: Path) -> None:
        # 3-stelliges Format wie "930" wird zu 09:30.
        path = tmp_path / "data.json"
        path.write_text(
            json.dumps(
                {
                    "default_hours": {
                        "regular": {"open": "930", "close": "1600"},
                    },
                    "exchanges": [
                        {
                            "exchange_id": "X",
                            "description": "X",
                            "time_zone": "UTC",
                        }
                    ],
                    "holidays_us": {},
                }
            ),
            encoding="utf-8",
        )
        service = TWSCalendarService(
            data_path=path,
            today_provider=lambda: date(2026, 5, 11),
        )
        import asyncio

        cal = asyncio.run(service.get("X"))
        rth = cal.days[0].sessions[0]
        assert rth.opens_at.hour == 9
        assert rth.opens_at.minute == 30

    def test_invalid_hhmm_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text(
            json.dumps(
                {
                    "default_hours": {
                        "regular": {"open": "abcd", "close": "1600"},
                    },
                    "exchanges": [
                        {
                            "exchange_id": "X",
                            "description": "X",
                            "time_zone": "UTC",
                        }
                    ],
                    "holidays_us": {},
                }
            ),
            encoding="utf-8",
        )
        service = TWSCalendarService(
            data_path=path,
            today_provider=lambda: date(2026, 5, 11),
        )
        import asyncio

        with pytest.raises(ValueError, match="ungueltige HHMM"):
            asyncio.run(service.get("X"))


# ---------------------------------------------------------------------------
# Default today_provider
# ---------------------------------------------------------------------------


class TestDefaultTodayProvider:
    @pytest.mark.asyncio
    async def test_default_today_uses_utcnow(self) -> None:
        # Ohne explizites today_provider zieht der Service das aktuelle
        # UTC-Datum. Wir verifizieren nur, dass der erste Tag <= heute
        # liegt und 14 Tage zurueckkommen.
        service = TWSCalendarService()
        cal = await service.get("NASDAQ")
        assert len(cal.days) == 14
        today_utc = datetime.now(timezone.utc).date()
        assert cal.days[0].date in (today_utc, today_utc.replace(day=today_utc.day))
