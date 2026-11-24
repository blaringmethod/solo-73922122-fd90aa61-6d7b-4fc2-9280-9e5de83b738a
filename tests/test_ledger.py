"""Unit and integration tests for the exposure ledger.

Run with:  python -m pytest -q
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from ledger.app import create_app
from ledger.store import Ledger, LeapSwapRejected, ValidationError
from ledger.timecode import (
    DEFAULT_LEAP_TABLE,
    LeapTable,
    TimecodeError,
    day_range_real,
    fmt_utc,
    parse_leap_table,
    real_to_utc,
    utc_to_real,
)

LEAP = dt.date(2016, 12, 31)
NON_LEAP = dt.date(2016, 12, 30)


def header():
    return "telescope,exposure_id,start,duration_s\n"


def make_ledger(tmp_path, name="ledger.sqlite3"):
    return Ledger(tmp_path / name)


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------


def test_epoch_is_zero_real_seconds():
    assert utc_to_real("2000-01-01 00:00:00", DEFAULT_LEAP_TABLE) == 0
    assert real_to_utc(0, DEFAULT_LEAP_TABLE) == (dt.datetime(2000, 1, 1), False)


def test_leap_day_has_86401_seconds_and_own_last_second():
    start, end = day_range_real(LEAP, DEFAULT_LEAP_TABLE)
    assert end - start == 86401
    last_dt, last_leap = real_to_utc(end - 1, DEFAULT_LEAP_TABLE)
    assert fmt_utc(last_dt, last_leap) == "2016-12-31 23:59:60"
    next_dt, next_leap = real_to_utc(end, DEFAULT_LEAP_TABLE)
    assert fmt_utc(next_dt, next_leap) == "2017-01-01 00:00:00"


def test_adjacent_leap_seconds_are_consecutive():
    base = utc_to_real("2016-12-31 23:59:59", DEFAULT_LEAP_TABLE)
    assert utc_to_real("2016-12-31 23:59:60", DEFAULT_LEAP_TABLE) == base + 1
    assert utc_to_real("2017-01-01 00:00:00", DEFAULT_LEAP_TABLE) == base + 2


def test_second_60_rejected_outside_leap_day_or_time():
    with pytest.raises(TimecodeError):
        utc_to_real("2016-12-31 23:58:60", DEFAULT_LEAP_TABLE)
    with pytest.raises(TimecodeError):
        utc_to_real("2016-12-30 23:59:60", DEFAULT_LEAP_TABLE)


def test_roundtrip_every_second_around_leap_day():
    start, end = day_range_real(LEAP, DEFAULT_LEAP_TABLE)
    for real in range(start - 3, end + 3):
        dt_value, leap = real_to_utc(real, DEFAULT_LEAP_TABLE)
        text = fmt_utc(dt_value, leap)
        assert utc_to_real(text, DEFAULT_LEAP_TABLE) == real


def test_real_seconds_are_utc_free():
    # 2016-12-31 23:59:60 is real second 536544004 with the default table.
    assert utc_to_real("2016-12-31 23:59:60", DEFAULT_LEAP_TABLE) == 536544004


def test_parse_leap_table_rejects_bad_batches():
    for bad in ["2016-13-01", "2016-12-31 00:00:00", "2016-12-31\n2016-12-31"]:
        with pytest.raises(TimecodeError):
            parse_leap_table(bad)


def test_parse_leap_table_ignores_comments_and_blanks():
    table = parse_leap_table("# comment\n\n2015-06-30\n2016-12-31 # trailing\n")
    assert table.dates == (dt.date(2015, 6, 30), dt.date(2016, 12, 31))


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_import_preserves_raw_values(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.import_csv(
        header() + "T1,X1,2016-12-31 23:59:60,1\n", source_name="night.csv"
    )
    assert result["row_count"] == 1
    rows = ledger.all_rows()
    assert rows[0]["start_raw"] == "2016-12-31 23:59:60"
    assert rows[0]["start_source"] == "utc"


def test_import_accepts_both_start_spellings(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(
        header() + "T1,A,2016-12-31 23:59:59,60\n" + "T1,B,536544005,60\n"
    )
    assert {r["exposure_id"] for r in ledger.all_rows()} == {"A", "B"}


def test_import_is_all_or_nothing(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(ValidationError) as exc:
        ledger.import_csv(
            header()
            + "T1,GOOD,2016-12-31 00:00:00,10\n"
            + "T2,BAD,2016-12-31 00:00:00,3601\n"
        )
    assert any("line 3" in detail for detail in exc.value.details)
    assert ledger.stats()["exposures"] == 0
    assert ledger.stats()["batches"] == 0


def test_import_rejects_duplicate_ids_inside_batch(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(ValidationError):
        ledger.import_csv(
            header()
            + "T1,DUP,2016-12-31 00:00:00,1\n"
            + "T1,DUP,2016-12-31 00:00:01,1\n"
        )
    assert ledger.stats()["exposures"] == 0


def test_import_rejects_existing_id_whole_batch(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(header() + "T1,A,2016-12-31 00:00:00,1\n")
    with pytest.raises(ValidationError):
        ledger.import_csv(
            header() + "T1,A,2016-12-31 00:00:00,1\n" + "T1,B,2016-12-31 00:00:00,1\n"
        )
    assert ledger.stats()["exposures"] == 1


def test_import_checks_against_table_at_submit_time(tmp_path):
    table = LeapTable((dt.date(2015, 6, 30),))
    ledger = Ledger(tmp_path / "t.sqlite3", initial_table=table)
    with pytest.raises(ValidationError) as exc:
        ledger.import_csv(header() + "T1,X,2016-12-31 23:59:60,1\n")
    assert any("not a leap day" in detail for detail in exc.value.details)


# ---------------------------------------------------------------------------
# Day queries
# ---------------------------------------------------------------------------


def test_day_query_clips_and_uses_half_open_intervals(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(
        header()
        + "T1,BEFORE,2016-12-31 23:59:00,120\n"  # crosses midnight
        + "T1,INSIDE,2016-12-31 12:00:00,60\n"
        + "T1,AT_END,2017-01-01 00:00:00,10\n"  # starts at exclusive end
        + "T1,LEAP,2016-12-31 23:59:60,1\n"
    )
    report = ledger.day_report(LEAP)
    ids = [row["exposure_id"] for row in report.rows]
    assert ids == ["INSIDE", "BEFORE", "LEAP"]
    before = next(r for r in report.rows if r["exposure_id"] == "BEFORE")
    assert before["window_seconds"] == 61
    assert before["window_end_utc"] == "2017-01-01 00:00:00"
    leap = next(r for r in report.rows if r["exposure_id"] == "LEAP")
    assert leap["window_start_utc"] == "2016-12-31 23:59:60"
    assert leap["window_seconds"] == 1
    assert report.day_seconds == 86401
    assert report.day_last_utc == "2016-12-31 23:59:60"


def test_day_query_reads_only_relevant_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(
        header()
        + "T1,LONG_AGO,2016-11-01 00:00:00,3600\n"
        + "T1,EDGE,2016-12-31 00:00:00,1\n"
    )
    report = ledger.day_report(LEAP)
    assert [row["exposure_id"] for row in report.rows] == ["EDGE"]


def test_non_leap_day_is_86400_seconds(tmp_path):
    ledger = make_ledger(tmp_path)
    report = ledger.day_report(NON_LEAP)
    assert report.day_seconds == 86400
    assert report.day_last_utc == "2016-12-30 23:59:59"


# ---------------------------------------------------------------------------
# Leap table replacement
# ---------------------------------------------------------------------------


def test_leap_swap_rejected_lists_offenders_and_keeps_old_table(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(header() + "T1,LEAP,2016-12-31 23:59:60,1\n")
    with pytest.raises(LeapSwapRejected) as exc:
        ledger.replace_leap_table("2015-06-30\n")
    assert [item["exposure_id"] for item in exc.value.offending] == ["LEAP"]
    version, table = ledger.active_table()
    assert version == 1
    assert LEAP in table


def test_leap_swap_succeeds_and_recomputes_text_rows(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(header() + "T1,X,2016-12-31 23:59:00,120\n")
    before = ledger.day_report(LEAP)
    assert before.day_seconds == 86401
    result = ledger.replace_leap_table("2015-06-30\n")
    assert result["version"] == 2
    after = ledger.day_report(LEAP)
    assert after.day_seconds == 86400
    assert after.rows[0]["window_seconds"] == 60


def test_leap_swap_keeps_real_second_rows_absolute(tmp_path):
    ledger = make_ledger(tmp_path)
    noon_real = utc_to_real("2016-12-31 12:00:00", DEFAULT_LEAP_TABLE)
    ledger.import_csv(header() + f"T1,R,{noon_real},5\n")
    ledger.replace_leap_table("2015-06-30\n")
    row = ledger.all_rows()[0]
    # Absolute real seconds never move, even though the UTC rendering does
    # (three leap days were removed from the table).
    assert row["start_real"] == noon_real
    report = ledger.day_report(LEAP)
    assert report.rows[0]["window_start_real"] == noon_real
    assert report.rows[0]["window_start_utc"] == "2016-12-31 12:00:03"


def test_leap_swap_with_identical_table_is_noop(tmp_path):
    ledger = make_ledger(tmp_path)
    from ledger.timecode import format_leap_table

    result = ledger.replace_leap_table(format_leap_table(DEFAULT_LEAP_TABLE))
    assert result["unchanged"] is True
    assert ledger.stats()["leap_version"] == 1


def test_leap_swap_accepts_new_leap_day_and_import_after_swap(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.replace_leap_table("2015-06-30\n2016-12-31\n2020-12-31\n")
    ledger.import_csv(header() + "T1,NEW,2020-12-31 23:59:60,1\n")
    report = ledger.day_report(dt.date(2020, 12, 31))
    assert report.day_seconds == 86401
    assert report.rows[0]["window_seconds"] == 1


# ---------------------------------------------------------------------------
# Concurrency while replacing
# ---------------------------------------------------------------------------


def test_reads_continue_during_leap_replacement(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.import_csv(
        header() + "".join(f"T{i},E{i},2016-12-31 12:00:00,60\n" for i in range(50))
    )
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                report = ledger.day_report(LEAP)
                assert all(row["window_seconds"] == 60 for row in report.rows)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)
                return

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(5):
            ledger.replace_leap_table("2015-06-30\n2016-12-31\n")
            ledger.replace_leap_table("2015-06-30\n")
    finally:
        stop.set()
        thread.join()
    assert not errors


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app({"TESTING": True, "LEDGER_DB": str(tmp_path / "web.sqlite3")})
    app.secret_key = "test"
    return app.test_client()


def test_web_day_page_and_csv_download(client):
    response = client.post(
        "/import",
        data={
            "csv_text": header()
            + "T1,A,2016-12-31 23:59:00,120\n"
            + "T2,B,2016-12-31 23:59:60,1\n"
        },
    )
    assert response.status_code == 200
    page = client.get("/day?date=2016-12-31")
    text = page.get_data(as_text=True)
    assert "86401" in text
    assert "2016-12-31 23:59:60" in text
    download = client.get("/day.csv?date=2016-12-31")
    assert download.status_code == 200
    assert download.mimetype == "text/csv"
    body = download.get_data(as_text=True)
    assert "A" in body and "61" in body


def test_web_import_rejects_batch_and_reports_lines(client):
    response = client.post(
        "/import",
        data={"csv_text": header() + "T1,BAD,2016-12-31 00:00:00,0\n"},
    )
    text = response.get_data(as_text=True)
    assert "拒绝" in text or "rejected" in text
    assert "line 2" in text


def test_web_leap_replacement_rejection_shows_ids(client):
    client.post(
        "/import",
        data={"csv_text": header() + "T1,KEEP,2016-12-31 23:59:60,1\n"},
    )
    response = client.post("/leap", data={"leap_text": "2015-06-30\n"})
    text = response.get_data(as_text=True)
    assert "KEEP" in text
    assert "换表未生效" in text
    assert client.get("/day?date=2016-12-31").get_data(as_text=True).count("86401") >= 1


def test_web_bad_date(client):
    response = client.get("/day?date=1999-12-31")
    assert response.status_code == 200
    assert "2000-01-01" in response.get_data(as_text=True)
