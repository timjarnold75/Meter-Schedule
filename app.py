# app.py
# Lightweight multi-user meter tracker with Eagleville/Eagleford, Excel import, CSV export.
# Run: pip install flask sqlalchemy pandas openpyxl
# Then: python app.py

import csv
import io
import os
from datetime import datetime
from typing import Optional, Dict

from flask import (
    Flask, request, redirect, url_for, render_template_string,
    send_file, flash
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Date, Text, Enum
)
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session
from sqlalchemy.exc import IntegrityError
import pandas as pd

# -------------------- Config --------------------
DATABASE_URL = "sqlite:///meters.db"   # change filename if you want to "rename db"
ALLOWED_STATIONS = ("Eagleville", "Eagleford")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}  # fine for small multi-user setups
)
Session = scoped_session(sessionmaker(bind=engine))
Base = declarative_base()


# -------------------- Model --------------------
class Meter(Base):
    __tablename__ = "meters"

    id = Column(Integer, primary_key=True)
    station = Column(Enum(*ALLOWED_STATIONS, name="station_enum"), nullable=False)

    meter_name = Column(String(200), nullable=False)
    flow_cal_id = Column(String(200), nullable=True)

    test_date = Column(Date, nullable=True)
    h2s_ppm = Column(Float, nullable=True)

    meter_type = Column(String(200), nullable=True)
    meter_address = Column(String(200), nullable=True)

    serial_number = Column(String(200), nullable=True)
    tube_serial_number = Column(String(200), nullable=True)

    tube_size = Column(String(100), nullable=True)
    orifice_plate_size = Column(String(100), nullable=True)

    notes = Column(Text, nullable=True)


Base.metadata.create_all(engine)


# -------------------- Helpers --------------------
def parse_date(s: Optional[str]) -> Optional[datetime.date]:
    if not s or str(s).strip() == "":
        return None
    # Try multiple formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            continue
    # Try pandas to_datetime fallback (handles Excel serials too)
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def parse_float(s: Optional[str]) -> Optional[float]:
    if s is None or str(s).strip() == "":
        return None
    try:
        return float(str(s).strip())
    except ValueError:
        return None


# Mapping possible Excel header variations -> canonical field names
HEADER_MAP: Dict[str, str] = {
    # station
    "station": "station",

    # core fields
    "meter name": "meter_name",
    "meter_name": "meter_name",

    "flow cal id": "flow_cal_id",
    "flow_cal_id": "flow_cal_id",
    "flowcal id": "flow_cal_id",

    "test date": "test_date",
    "test_date": "test_date",
    "test dates": "test_date",

    "h2s": "h2s_ppm",
    "h2s ppm": "h2s_ppm",
    "h2s_ppm": "h2s_ppm",

    "meter type": "meter_type",
    "meter_type": "meter_type",

    "meter address": "meter_address",
    "meter_address": "meter_address",
    "address": "meter_address",

    "serial number": "serial_number",
    "device s/n": "serial_number",
    "serial_number": "serial_number",

    "tube serial number": "tube_serial_number",
    "tube s/n": "tube_serial_number",
    "tube_serial_number": "tube_serial_number",

    "tube size": "tube_size",
    "tube_size": "tube_size",

    "orifice plate size": "orifice_plate_size",
    "orifice/plate size": "orifice_plate_size",
    "orifice_plate_size": "orifice_plate_size",

    "notes": "notes",
    "comments": "notes",
}


def normalize_header(h: str) -> str:
    return str(h).strip().lower().replace("\n", " ").replace("\r", " ").replace("  ", " ")


# -------------------- Routes --------------------
@app.route("/")
def home():
    return redirect(url_for("list_by_station", station="Eagleville"))


@app.route("/station/<station>")
def list_by_station(station: str):
    if station not in ALLOWED_STATIONS:
        flash("Unknown station.", "danger")
        return redirect(url_for("home"))
    s = Session()
    try:
        items = (
            s.query(Meter)
            .filter(Meter.station == station)
            .order_by(Meter.meter_name.asc())
            .all()
        )
    finally:
        s.close()
    return render_template_string(TEMPLATE, active_station=station, items=items, stations=ALLOWED_STATIONS)


@app.route("/add", methods=["POST"])
def add_meter():
    s = Session()
    try:
        station = request.form.get("station")
        if station not in ALLOWED_STATIONS:
            flash("Invalid station.", "danger")
            return redirect(url_for("home"))

        m = Meter(
            station=station,
            meter_name=request.form.get("meter_name", "").strip() or "Unnamed",
            flow_cal_id=request.form.get("flow_cal_id", "").strip() or None,
            test_date=parse_date(request.form.get("test_date")),
            h2s_ppm=parse_float(request.form.get("h2s_ppm")),
            meter_type=request.form.get("meter_type", "").strip() or None,
            meter_address=request.form.get("meter_address", "").strip() or None,
            serial_number=request.form.get("serial_number", "").strip() or None,
            tube_serial_number=request.form.get("tube_serial_number", "").strip() or None,
            tube_size=request.form.get("tube_size", "").strip() or None,
            orifice_plate_size=request.form.get("orifice_plate_size", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        s.add(m)
        s.commit()
        flash("Meter added.", "success")
        return redirect(url_for("list_by_station", station=station))
    except IntegrityError:
        s.rollback()
        flash("Failed to add meter due to a database error.", "danger")
        return redirect(url_for("home"))
    finally:
        s.close()


@app.route("/edit/<int:meter_id>", methods=["POST"])
def edit_meter(meter_id: int):
    s = Session()
    try:
        m = s.get(Meter, meter_id)
        if not m:
            flash("Meter not found.", "danger")
            return redirect(url_for("home"))

        station = request.form.get("station")
        if station not in ALLOWED_STATIONS:
            flash("Invalid station.", "danger")
            return redirect(url_for("home"))

        m.station = station
        m.meter_name = request.form.get("meter_name", "").strip() or m.meter_name
        m.flow_cal_id = request.form.get("flow_cal_id", "").strip() or None
        m.test_date = parse_date(request.form.get("test_date"))
        m.h2s_ppm = parse_float(request.form.get("h2s_ppm"))
        m.meter_type = request.form.get("meter_type", "").strip() or None
        m.meter_address = request.form.get("meter_address", "").strip() or None
        m.serial_number = request.form.get("serial_number", "").strip() or None
        m.tube_serial_number = request.form.get("tube_serial_number", "").strip() or None
        m.tube_size = request.form.get("tube_size", "").strip() or None
        m.orifice_plate_size = request.form.get("orifice_plate_size", "").strip() or None
        m.notes = request.form.get("notes", "").strip() or None

        s.commit()
        flash("Meter updated.", "success")
        return redirect(url_for("list_by_station", station=m.station))
    except IntegrityError:
        s.rollback()
        flash("Failed to update meter due to a database error.", "danger")
        return redirect(url_for("home"))
    finally:
        s.close()


@app.route("/delete/<int:meter_id>", methods=["POST"])
def delete_meter(meter_id: int):
    s = Session()
    try:
        m = s.get(Meter, meter_id)
        if not m:
            flash("Meter not found.", "warning")
            return redirect(url_for("home"))
        station = m.station
        s.delete(m)
        s.commit()
        flash("Meter deleted.", "success")
        return redirect(url_for("list_by_station", station=station))
    finally:
        s.close()


@app.route("/import", methods=["POST"])
def import_excel():
    file = request.files.get("file")
    target_station = request.form.get("target_station")
    if not file or file.filename == "":
        flash("No file selected.", "warning")
        return redirect(url_for("home"))
    if target_station not in ALLOWED_STATIONS:
        flash("Choose a valid target station for import.", "danger")
        return redirect(url_for("home"))

    try:
        df = pd.read_excel(file, engine="openpyxl")
    except Exception as e:
        flash(f"Failed to read Excel: {e}", "danger")
        return redirect(url_for("home"))

    # Normalize headers and map
    mapped_cols = {}
    for col in df.columns:
        norm = normalize_header(col)
        if norm in HEADER_MAP:
            mapped_cols[col] = HEADER_MAP[norm]

    if "station" not in [HEADER_MAP.get(normalize_header(c), "") for c in df.columns]:
        # If file doesn't include station per-row, use target_station for all rows
        df["__station_fallback__"] = target_station
        mapped_cols["__station_fallback__"] = "station"

    # Build rows
    inserted = 0
    s = Session()
    try:
        for _, row in df.iterrows():
            data = {
                "station": None,
                "meter_name": None,
                "flow_cal_id": None,
                "test_date": None,
                "h2s_ppm": None,
                "meter_type": None,
                "meter_address": None,
                "serial_number": None,
                "tube_serial_number": None,
                "tube_size": None,
                "orifice_plate_size": None,
                "notes": None,
            }

            for orig_col, mapped in mapped_cols.items():
                val = row.get(orig_col, None)
                if mapped == "test_date":
                    data[mapped] = parse_date(val)
                elif mapped == "h2s_ppm":
                    data[mapped] = parse_float(val)
                elif mapped == "station":
                    # ensure station is valid, else fallback
                    st = str(val).strip() if val is not None else target_station
                    data[mapped] = st if st in ALLOWED_STATIONS else target_station
                else:
                    data[mapped] = (None if pd.isna(val) else str(val).strip())

            # Require meter_name minimally
            if not data["meter_name"]:
                continue

            m = Meter(**data)
            s.add(m)
            inserted += 1

        s.commit()
        flash(f"Imported {inserted} rows.", "success")
    except Exception as e:
        s.rollback()
        flash(f"Import failed: {e}", "danger")
    finally:
        s.close()

    return redirect(url_for("list_by_station", station=target_station))


@app.route("/export/<station>.csv")
def export_csv(station: str):
    if station not in ALLOWED_STATIONS:
        flash("Unknown station.", "danger")
        return redirect(url_for("home"))
    s = Session()
    try:
        items = s.query(Meter).filter(Meter.station == station).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Station", "Meter Name", "Flow Cal ID", "Test Date", "H2S PPM",
            "Meter Type", "Meter Address", "Serial Number", "Tube Serial Number",
            "Tube Size", "Orifice Plate Size", "Notes"
        ])
        for m in items:
            writer.writerow([
                m.station,
                m.meter_name,
                m.flow_cal_id or "",
                m.test_date.isoformat() if m.test_date else "",
                m.h2s_ppm if m.h2s_ppm is not None else "",
                m.meter_type or "",
                m.meter_address or "",
                m.serial_number or "",
                m.tube_serial_number or "",
                m.tube_size or "",
                m.orifice_plate_size or "",
                m.notes or "",
            ])
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{station}_meters.csv",
        )
    finally:
        s.close()


# -------------------- Template (single-file Jinja) --------------------
TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Meter Tracker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css"
    rel="stylesheet">
  <style>
    .form-inline input, .form-inline select { margin-right: .5rem; }
    .sticky { position: sticky; top: 0; background: #fff; z-index: 10; }
    textarea { resize: vertical; }
    .small-note { font-size: .875rem; color: #6c757d; }
  </style>
</head>
<body class="bg-light">
<div class="container py-4">
  <div class="d-flex align-items-center justify-content-between mb-3">
    <h3 class="mb-0">Meter Tracker</h3>
    <div>
      <a class="btn btn-outline-secondary me-2" href="{{ url_for('list_by_station', station='Eagleville') }}">Eagleville</a>
      <a class="btn btn-outline-secondary me-2" href="{{ url_for('list_by_station', station='Eagleford') }}">Eagleford</a>
      <a class="btn btn-success" href="{{ url_for('export_csv', station=active_station) }}">Export CSV ({{ active_station }})</a>
    </div>
  </div>

  {% with messages = get_flashed_messages(with_categories=True) %}
    {% if messages %}
      {% for category, msg in messages %}
        <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
          {{ msg }}
          <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        </div>
      {% endfor %}
    {% endif %}
  {% endwith %}

  <div class="card mb-4">
    <div class="card-header">Add Meter ({{ active_station }})</div>
    <div class="card-body">
      <form method="post" action="{{ url_for('add_meter') }}" class="row g-2">
        <input type="hidden" name="station" value="{{ active_station }}">
        <div class="col-md-3">
          <label class="form-label">Meter Name *</label>
          <input name="meter_name" class="form-control" required>
        </div>
        <div class="col-md-3">
          <label class="form-label">Flow Cal ID</label>
          <input name="flow_cal_id" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Test Date</label>
          <input type="date" name="test_date" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">H2S PPM</label>
          <input name="h2s_ppm" class="form-control" inputmode="decimal">
        </div>

        <div class="col-md-3">
          <label class="form-label">Meter Type</label>
          <input name="meter_type" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Meter Address</label>
          <input name="meter_address" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Serial Number</label>
          <input name="serial_number" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Tube Serial Number</label>
          <input name="tube_serial_number" class="form-control">
        </div>

        <div class="col-md-3">
          <label class="form-label">Tube Size</label>
          <input name="tube_size" class="form-control">
        </div>
        <div class="col-md-3">
          <label class="form-label">Orifice Plate Size</label>
          <input name="orifice_plate_size" class="form-control">
        </div>
        <div class="col-md-12">
          <label class="form-label">Notes</label>
          <textarea name="notes" class="form-control" rows="2"></textarea>
        </div>

        <div class="col-12">
          <button class="btn btn-primary">Add</button>
        </div>
      </form>
    </div>
  </div>

  <div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
      <span>Import from Excel to {{ active_station }}</span>
      <span class="small-note">Accepted: .xlsx | Columns auto-mapped (e.g., "Meter Name", "Flow Cal ID", "Test Date", "H2S PPM", etc.)</span>
    </div>
    <div class="card-body">
      <form method="post" action="{{ url_for('import_excel') }}" enctype="multipart/form-data" class="row g-2">
        <div class="col-md-6">
          <input type="file" name="file" accept=".xlsx" class="form-control" required>
        </div>
        <div class="col-md-4">
          <select name="target_station" class="form-select">
            {% for s in stations %}
              <option value="{{ s }}" {% if s == active_station %}selected{% endif %}>{{ s }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="col-md-2">
          <button class="btn btn-secondary w-100">Import</button>
        </div>
      </form>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Meters — {{ active_station }}</div>
    <div class="card-body table-responsive">
      <table class="table table-sm table-striped align-middle">
        <thead class="table-light sticky">
          <tr>
            <th>Meter Name</th>
            <th>Flow Cal ID</th>
            <th>Test Date</th>
            <th>H2S PPM</th>
            <th>Meter Type</th>
            <th>Address</th>
            <th>Serial #</th>
            <th>Tube S/N</th>
            <th>Tube Size</th>
            <th>Orifice Plate</th>
            <th>Notes</th>
            <th style="width: 160px;">Actions</th>
          </tr>
        </thead>
        <tbody>
          {% for m in items %}
            <tr>
              <td>{{ m.meter_name }}</td>
              <td>{{ m.flow_cal_id or "" }}</td>
              <td>{{ m.test_date or "" }}</td>
              <td>{{ m.h2s_ppm if m.h2s_ppm is not none else "" }}</td>
              <td>{{ m.meter_type or "" }}</td>
              <td>{{ m.meter_address or "" }}</td>
              <td>{{ m.serial_number or "" }}</td>
              <td>{{ m.tube_serial_number or "" }}</td>
              <td>{{ m.tube_size or "" }}</td>
              <td>{{ m.orifice_plate_size or "" }}</td>
              <td style="max-width: 240px; white-space: pre-wrap;">{{ m.notes or "" }}</td>
              <td>
                <button class="btn btn-sm btn-outline-primary" data-bs-toggle="collapse" data-bs-target="#edit{{ m.id }}">Edit</button>
                <form method="post" action="{{ url_for('delete_meter', meter_id=m.id) }}" style="display:inline" onsubmit="return confirm('Delete this meter?');">
                  <button class="btn btn-sm btn-outline-danger">Delete</button>
                </form>
              </td>
            </tr>
            <tr class="collapse" id="edit{{ m.id }}">
              <td colspan="12">
                <form method="post" action="{{ url_for('edit_meter', meter_id=m.id) }}" class="row g-2">
                  <div class="col-md-2">
                    <label class="form-label">Station</label>
                    <select name="station" class="form-select">
                      {% for s in stations %}
                        <option value="{{ s }}" {% if s == m.station %}selected{% endif %}>{{ s }}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Meter Name *</label>
                    <input name="meter_name" class="form-control" value="{{ m.meter_name }}" required>
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Flow Cal ID</label>
                    <input name="flow_cal_id" class="form-control" value="{{ m.flow_cal_id or '' }}">
                  </div>
                  <div class="col-md-2">
                    <label class="form-label">Test Date</label>
                    <input type="date" name="test_date" class="form-control" value="{{ m.test_date }}">
                  </div>
                  <div class="col-md-2">
                    <label class="form-label">H2S PPM</label>
                    <input name="h2s_ppm" class="form-control" value="{{ m.h2s_ppm if m.h2s_ppm is not none else '' }}">
                  </div>

                  <div class="col-md-3">
                    <label class="form-label">Meter Type</label>
                    <input name="meter_type" class="form-control" value="{{ m.meter_type or '' }}">
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Meter Address</label>
                    <input name="meter_address" class="form-control" value="{{ m.meter_address or '' }}">
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Serial Number</label>
                    <input name="serial_number" class="form-control" value="{{ m.serial_number or '' }}">
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Tube Serial Number</label>
                    <input name="tube_serial_number" class="form-control" value="{{ m.tube_serial_number or '' }}">
                  </div>

                  <div class="col-md-3">
                    <label class="form-label">Tube Size</label>
                    <input name="tube_size" class="form-control" value="{{ m.tube_size or '' }}">
                  </div>
                  <div class="col-md-3">
                    <label class="form-label">Orifice Plate Size</label>
                    <input name="orifice_plate_size" class="form-control" value="{{ m.orifice_plate_size or '' }}">
                  </div>
                  <div class="col-md-12">
                    <label class="form-label">Notes</label>
                    <textarea name="notes" class="form-control" rows="2">{{ m.notes or '' }}</textarea>
                  </div>

                  <div class="col-12">
                    <button class="btn btn-primary">Save</button>
                  </div>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

      {% if not items %}
        <div class="text-muted">No meters yet for {{ active_station }}. Add some above or import from Excel.</div>
      {% endif %}
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# -------------------- Main --------------------
if __name__ == "__main__":
    # Enable SQLite WAL for better concurrent reads/writes
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass

    app.run(host="127.0.0.1", port=5000, debug=True)
