"""Flask web app for the exposure ledger."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import os
from pathlib import Path

from flask import (
    Flask,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from .store import (
    CSV_FIELDS,
    Ledger,
    LedgerError,
    LeapSwapRejected,
    ValidationError,
)
from .timecode import (
    TimecodeError,
    format_leap_table,
    parse_date,
)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _csv_report(report) -> str:
    """Render one day report as a CSV attachment (raw values preserved)."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "telescope",
            "exposure_id",
            "start_utc",
            "end_utc_exclusive",
            "seconds",
            "source_start",
            "source_duration_s",
        ]
    )
    for row in report.rows:
        writer.writerow(
            [
                row["telescope"],
                row["exposure_id"],
                row["window_start_utc"],
                row["window_end_utc"],
                row["window_seconds"],
                row["source_start"],
                row["source_duration_s"],
            ]
        )
    return buffer.getvalue()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.environ.get("LEDGER_SECRET", "dev-only-change-me")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["LEDGER_DB"] = os.environ.get(
        "LEDGER_DB", str(Path(app.root_path).parent / "var" / "ledger.sqlite3")
    )
    if config:
        app.config.update(config)
    app.ledger = Ledger(app.config["LEDGER_DB"])

    @app.get("/")
    def index():
        return redirect(url_for("day_view"))

    @app.get("/day")
    def day_view():
        raw_day = request.args.get("date", "").strip()
        day = None
        report = None
        error = None
        if raw_day:
            try:
                day = parse_date(raw_day)
                report = app.ledger.day_report(day)
            except (TimecodeError, ValidationError) as exc:
                error = str(exc)
        else:
            today = _dt.datetime.now(_dt.timezone.utc).date()
            raw_day = today.isoformat()
        return render_template(
            "day.html",
            active="day",
            raw_day=raw_day,
            error=error,
            report=report,
            stats=app.ledger.stats(),
            leap_version=app.ledger.active_table()[0],
        )

    @app.get("/day.csv")
    def day_csv():
        raw_day = request.args.get("date", "").strip()
        try:
            day = parse_date(raw_day)
            report = app.ledger.day_report(day)
        except (TimecodeError, ValidationError) as exc:
            return Response(str(exc), status=400, mimetype="text/plain")
        body = _csv_report(report)
        filename = f"exposures_{report.day.isoformat()}.csv"
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.route("/import", methods=("GET", "POST"))
    def import_view():
        result = None
        errors: list[str] = []
        message = None
        if request.method == "POST":
            uploaded = request.files.get("csv_file")
            pasted = request.form.get("csv_text", "")
            source_name = "pasted.csv"
            text = pasted
            if uploaded is not None and uploaded.filename:
                source_name = os.path.basename(uploaded.filename)
                raw = uploaded.read()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    errors.append("the uploaded file is not valid UTF-8")
                if len(raw) > MAX_UPLOAD_BYTES:
                    errors.append("the uploaded file is larger than 5 MiB")
            if not errors:
                try:
                    result = app.ledger.import_csv(text, source_name=source_name)
                except ValidationError as exc:
                    message = str(exc)
                    errors = exc.details
                except LedgerError as exc:
                    message = str(exc)
        return render_template(
            "import.html",
            active="import",
            result=result,
            errors=errors,
            message=message,
            stats=app.ledger.stats(),
            leap_version=app.ledger.active_table()[0],
            csv_fields=",".join(CSV_FIELDS),
        )

    @app.get("/template.csv")
    def template_csv():
        sample = (
            "telescope,exposure_id,start,duration_s\n"
            "T1,EXP-0001,2026-11-24 00:00:00,600\n"
            "T2,EXP-0002,2026-11-24 12:30:00,1200\n"
            "T3,EXP-0003,848793605,900\n"
        )
        return Response(
            sample,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="exposures_template.csv"'
            },
        )

    @app.route("/leap", methods=("GET", "POST"))
    def leap_view():
        offending: list[dict] | None = None
        result = None
        message = None
        if request.method == "POST":
            text = request.form.get("leap_text", "")
            try:
                result = app.ledger.replace_leap_table(text)
            except LeapSwapRejected as exc:
                offending = exc.offending
                message = str(exc)
            except ValidationError as exc:
                message = str(exc)
                offending = [{"error": detail} for detail in exc.details]
            except LedgerError as exc:
                message = str(exc)
        version, table = app.ledger.active_table()
        return render_template(
            "leap.html",
            active="leap",
            version=version,
            table=table,
            table_text=format_leap_table(table),
            history=app.ledger.leap_history(),
            offending=offending,
            result=result,
            message=message,
            stats=app.ledger.stats(),
        )

    @app.errorhandler(413)
    def too_large(_exc):
        return (
            render_template(
                "import.html",
                active="import",
                result=None,
                errors=["the upload is larger than 5 MiB"],
                message="upload rejected",
                stats=app.ledger.stats(),
                leap_version=app.ledger.active_table()[0],
                csv_fields=",".join(CSV_FIELDS),
            ),
            413,
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "ledger": app.ledger.stats()}

    return app
