import os
import calendar
from pathlib import Path
from datetime import date, datetime, timedelta
from flask import Flask, request, redirect, url_for, render_template_string, flash
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader, ChoiceLoader
from sqlalchemy.orm import joinedload
from sqlalchemy import inspect

# ------------------------
# Database config (Railway-ready)
# ------------------------
DB_URL = os.environ.get("DATABASE_URL")
if DB_URL:
    # Railway/Heroku sometimes provide 'postgres://'
    DB_URI = DB_URL.replace("postgres://", "postgresql://")
else:
    _raw = os.environ.get("INSPECT_DB_PATH", "inspections.db")
    base_dir = Path(__file__).resolve().parent
    db_path = Path(_raw)
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    DB_URI = "sqlite:///" + str(db_path).replace("\\", "/")

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "devkey")
db = SQLAlchemy(app)

# ------------------------
# Helpers & constants
# ------------------------
FREQ_MONTHS = {"Monthly": 1, "Quarterly": 3, "Semiannual": 6, "Annual": 12}
QUICK_REASONS = [
    "—",
    "No flow",
    "Valve closed",
    "Shutdown/maintenance",
    "Access blocked",
    "Unsafe conditions",
    "Customer requested skip",
    "Instrument offline",
    "Other",
]

def parse_ymd(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        y, m, d = [int(x) for x in s.split("-")]
        return date(y, m, d)
    except Exception:
        return None

def add_months(d: date, months: int) -> date:
    m0 = d.month - 1 + months
    y = d.year + (m0 // 12)
    m = (m0 % 12) + 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))

def compute_next(last_test: date, freq: str):
    if not last_test or not freq or freq == "Out of Service":
        return None
    months = FREQ_MONTHS.get(freq)
    return add_months(last_test, months) if months else None

def week_bounds(ref: date | None = None):
    today = ref or date.today()
    start = today - timedelta(days=today.weekday())  # Monday
    end = start + timedelta(days=6)                  # Sunday
    return start, end

# ------------------------
# Models
# ------------------------
class Field(db.Model):
    __tablename__ = "fields"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    location = db.Column(db.String(200))
    batteries = db.relationship("Battery", backref="field", cascade="all, delete-orphan")

class Battery(db.Model):
    __tablename__ = "batteries"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    notes = db.Column(db.String(500))
    field_id = db.Column(db.Integer, db.ForeignKey("fields.id"), nullable=False)
    meters = db.relationship("Meter", backref="battery", cascade="all, delete-orphan")

class Meter(db.Model):
    __tablename__ = "meters"
    id = db.Column(db.Integer, primary_key=True)
    meter_name = db.Column(db.String(160), nullable=False)
    flow_cal_id = db.Column(db.String(120))
    purchaser_name = db.Column(db.String(160))       # optional
    purchaser_meter_id = db.Column(db.String(120))   # optional
    meter_type = db.Column(db.String(120))
    meter_address = db.Column(db.String(200))
    serial_number = db.Column(db.String(120))
    tube_serial_number = db.Column(db.String(120))
    tube_size = db.Column(db.String(80))
    orifice_plate_size = db.Column(db.String(80))
    h2s_ppm = db.Column(db.String(40))
    notes = db.Column(db.String(1000))
    last_test_date = db.Column(db.Date)
    next_inspection = db.Column(db.Date)
    frequency = db.Column(db.String(40))  # "", Monthly, Quarterly, Semiannual, Annual, Out of Service
    battery_id = db.Column(db.Integer, db.ForeignKey("batteries.id"), nullable=False)
    history = db.relationship(
        "MeterHistory",
        backref="meter",
        cascade="all, delete-orphan",
        order_by="desc(MeterHistory.event_date)",
    )

class MeterHistory(db.Model):
    __tablename__ = "meter_history"
    id = db.Column(db.Integer, primary_key=True)
    meter_id = db.Column(db.Integer, db.ForeignKey("meters.id"), nullable=False, index=True)
    event_date = db.Column(db.Date, nullable=False)
    h2s_ppm = db.Column(db.String(40))
    notes = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_via = db.Column(db.String(40))  # 'manual' | 'mark_tested' | 'edit'

# ------------------------
# DB bootstrap + tiny migrations
# ------------------------
with app.app_context():
    db.create_all()
    # default fields
    for fname in ["Eagleville", "Eagleford"]:
        if not Field.query.filter_by(name=fname).first():
            db.session.add(Field(name=fname))
    db.session.commit()

    # lightweight column adders for older DBs
    insp = inspect(db.engine)
    cols = {c["name"] for c in insp.get_columns("meters")}
    with db.engine.begin() as conn:
        if "last_test_date" not in cols:
            conn.exec_driver_sql("ALTER TABLE meters ADD COLUMN last_test_date DATE")
        if "purchaser_name" not in cols:
            conn.exec_driver_sql("ALTER TABLE meters ADD COLUMN purchaser_name VARCHAR(160)")
        if "purchaser_meter_id" not in cols:
            conn.exec_driver_sql("ALTER TABLE meters ADD COLUMN purchaser_meter_id VARCHAR(120)")

# ------------------------
# Templates
# ------------------------
BASE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Inspection Scheduler</title>
   <style>
    :root{
      --bg:#f7f8fb;
      --text:#0f172a;
      --muted:#64748b;
      --card:#ffffff;
      --border:#e5e7eb;
      --accent:#2563eb;
      --accent-600:#1e40af;
      --warn:#b91c1c;
    }
    *{box-sizing:border-box}
    body{
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background:var(--bg); color:var(--text);
      margin:24px;
    }
    a{ text-decoration:none; color:var(--accent); }
    a:hover{ text-decoration:underline; }
    .topnav a{ margin-right:14px; font-weight:600; }

    .card{
      background:var(--card);
      border:1px solid var(--border);
      border-radius:14px;
      padding:16px 18px;
      box-shadow: 0 1px 2px rgba(0,0,0,.03);
      margin:14px 0;
    }

    h1{ font-size:28px; margin: 6px 0 14px; }
    h2{ font-size:20px; margin: 6px 0 10px; }
    h3{ font-size:16px; margin: 4px 0 8px; }

    label{ display:block; font-weight:600; color:var(--text); margin-bottom:6px;}
    input, select, textarea{
      width:100%; padding:9px 10px; border-radius:10px;
      border:1px solid var(--border); background:#fff; color:var(--text);
      outline:none;
    }
    input:focus, select:focus, textarea:focus{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(37,99,235,.15);
    }
    textarea{ resize:vertical; }

    .btn{
      display:inline-block; padding:9px 14px; border-radius:10px; border:1px solid var(--border);
      background:#fff; color:var(--text); cursor:pointer; font-weight:600;
    }
    .btn:hover{ background:#f4f6fb; }
    .btn.primary{ background:var(--accent); border-color:var(--accent); color:#fff; }
    .btn.primary:hover{ background:var(--accent-600); border-color:var(--accent-600); }
    .btn.danger{ background:#fef2f2; border-color:#fecaca; color:var(--warn); }

    /* Dashboard tables */
    table{ width:100%; border-collapse:collapse; }
    thead th{
      text-align:left; font-size:12px; letter-spacing:.02em; color:var(--muted);
      border-bottom:1px solid var(--border); padding:8px 6px;
      position:sticky; top:0; background:#fff;
    }
    td{ padding:10px 6px; border-top:1px solid var(--border); vertical-align:top; }
    tr:hover td{ background:#fafbff; }

    .pill{ display:inline-block; padding:2px 8px; border-radius:999px; border:1px solid var(--border); font-size:12px; color:var(--muted); }
    .pill-oos{ background:#fff1f2; border-color:#fecaca; color:#b91c1c; }

    .muted{ color:var(--muted); }
    .row{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }

    /* Aligned “sheet” for Add Meter row */
    .sheet{ table-layout:fixed; }
    .sheet td, .sheet th{ padding:8px 6px; }
    .sheet input, .sheet select{ width:100%; }
    .sheet .tight{ width: 120px; }

    /* Notes row spans full width but looks connected */
    .notes-wrap{
      border:1px solid var(--border);
      border-top:none;
      padding:10px; border-radius:0 0 12px 12px; background:#fff;
    }
    .notes-wrap textarea{ width:100%; }
    .note{ font-size:12px; color:var(--muted); margin-top:4px; }
    .tiny{ font-size:12px; }
  </style>
</head>
<body>
  <div class="topnav">
    <a href="{{ url_for('home') }}">Home</a>
    <a href="{{ url_for('list_fields') }}">Fields</a>
    <a href="{{ url_for('due') }}">Due This Week</a>
  </div>
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <ul>
      {% for m in messages %}<li>{{ m }}</li>{% endfor %}
      </ul>
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}
</body>
</html>
"""

HOME = """
{% extends "base.html" %}
{% block content %}
  <h1>Inspection Scheduler</h1>

  <div class="card">
    <h2>Tests Due</h2>
    <div class="muted">Week {{ week_start }} – {{ week_end }}</div>

    {% if overdue %}
      <h3 style="margin-top:10px;">Overdue</h3>
      <table>
        <thead><tr><th>Field</th><th>Battery</th><th>Meter</th><th>Next Inspection</th><th>Frequency</th><th>Update</th></tr></thead>
        <tbody>
          {% for m in overdue %}
          <tr>
            <td>{{ m.battery.field.name }}</td>
            <td>{{ m.battery.name }}</td>
            <td>{{ m.meter_name }}</td>
            <td>{{ m.next_inspection }}</td>
            <td>{{ m.frequency or '—' }}</td>
            <td>
              <form class="inline" method="post" action="{{ url_for('mark_tested_today', meter_id=m.id) }}?back=home" onsubmit="return confirm('Mark as tested today?');">
                <input name="new_h2s" placeholder="H2S" value="{{ m.h2s_ppm or '' }}" style="width:70px; margin-right:6px;">
                <select name="reason" style="width:170px; margin-right:6px;">
                  {% for r in quick_reasons %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
                </select>
                <input name="note" placeholder="note (optional)" style="width:180px; margin-right:6px;">
                <button class="btn">Mark Tested Today</button>
              </form>
              <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% endif %}

    <h3 style="margin-top:10px;">Due This Week</h3>
    {% if due_week %}
      <table>
        <thead><tr><th>Field</th><th>Battery</th><th>Meter</th><th>Next Inspection</th><th>Frequency</th><th>Update</th></tr></thead>
        <tbody>
          {% for m in due_week %}
          <tr>
            <td>{{ m.battery.field.name }}</td>
            <td>{{ m.battery.name }}</td>
            <td>{{ m.meter_name }}</td>
            <td>{{ m.next_inspection }}</td>
            <td>{{ m.frequency or '—' }}</td>
            <td>
              <form class="inline" method="post" action="{{ url_for('mark_tested_today', meter_id=m.id) }}?back=home" onsubmit="return confirm('Mark as tested today?');">
                <input name="new_h2s" placeholder="H2S" value="{{ m.h2s_ppm or '' }}" style="width:70px; margin-right:6px;">
                <select name="reason" style="width:170px; margin-right:6px;">
                  {% for r in quick_reasons %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
                </select>
                <input name="note" placeholder="note (optional)" style="width:180px; margin-right:6px;">
                <button class="btn">Mark Tested Today</button>
              </form>
              <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <div class="muted">No tests due this week.</div>
    {% endif %}
    <p style="margin-top:8px;"><a class="btn" href="{{ url_for('due') }}">Open full Due This Week</a></p>
  </div>

  <h2 style="margin-top:28px;">Dashboard — Meters by Battery</h2>
  {% for f in fields %}
    <div class="card">
      <h3>{{ f.name }}</h3>
      {% if f.batteries %}
        {% for b in f.batteries %}
          <div class="card" style="margin:12px 0; padding:12px;">
            <div class="row" style="justify-content:space-between;">
              <div><strong>{{ b.name }}</strong></div>
              <div class="muted">{{ b.meters|length }} meter(s)</div>
            </div>

            {% if b.meters %}
              <table>
                <thead>
                  <tr>
                    <th>Meter Name</th>
                    <th>Flow Cal ID</th>
                    <th>Purchaser</th>
                    <th>Purchaser Meter ID</th>
                    <th>Frequency</th>
                    <th>Last Test</th>
                    <th>Next Inspection</th>
                    <th>H2S (PPM)</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {% for m in b.meters|sort(attribute='meter_name') %}
                  <tr>
                    <td>{{ m.meter_name }}</td>
                    <td>{{ m.flow_cal_id or '—' }}</td>
                    <td>{{ m.purchaser_name or '—' }}</td>
                    <td>{{ m.purchaser_meter_id or '—' }}</td>
                    <td>
                      {% if (m.frequency or '') == 'Out of Service' %}
                        <span class="pill pill-oos">Out of Service</span>
                      {% else %}
                        {{ m.frequency or '—' }}
                      {% endif %}
                    </td>
                    <td>{{ m.last_test_date or '—' }}</td>
                    <td>{{ m.next_inspection or '—' }}</td>
                    <td>{{ m.h2s_ppm or '—' }}</td>
                    <td class="row">
                      <a class="btn" href="{{ url_for('edit_meter', meter_id=m.id) }}">Edit</a>
                      <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
                    </td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            {% else %}
              <div class="muted">No meters yet.</div>
            {% endif %}
            <p style="margin-top:10px;">
              <a class="btn" href="{{ url_for('battery_detail', battery_id=b.id) }}">Open {{ b.name }}</a>
            </p>
          </div>
        {% endfor %}
      {% else %}
        <div class="muted">No batteries in this field yet.</div>
      {% endif %}
    </div>
  {% endfor %}
{% endblock %}
"""

DUE_PAGE = """
{% extends "base.html" %}
{% block content %}
  <h1>Due This Week</h1>
  <div class="muted">Week {{ week_start }} – {{ week_end }}</div>

  {% if overdue %}
    <div class="card">
      <h2>Overdue</h2>
      <table>
        <thead><tr><th>Field</th><th>Battery</th><th>Meter</th><th>Next Inspection</th><th>Frequency</th><th>Update</th></tr></thead>
        <tbody>
          {% for m in overdue %}
          <tr>
            <td>{{ m.battery.field.name }}</td>
            <td>{{ m.battery.name }}</td>
            <td>{{ m.meter_name }}</td>
            <td>{{ m.next_inspection }}</td>
            <td>{{ m.frequency or '—' }}</td>
            <td>
              <form class="inline" method="post" action="{{ url_for('mark_tested_today', meter_id=m.id) }}?back=due" onsubmit="return confirm('Mark as tested today?');">
                <input name="new_h2s" placeholder="H2S" value="{{ m.h2s_ppm or '' }}" style="width:70px; margin-right:6px;">
                <select name="reason" style="width:170px; margin-right:6px;">
                  {% for r in quick_reasons %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
                </select>
                <input name="note" placeholder="note (optional)" style="width:180px; margin-right:6px;">
                <button class="btn">Mark Tested Today</button>
              </form>
              <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  {% endif %}

  <div class="card">
    <h2>Due This Week</h2>
    {% if due_week %}
      <table>
        <thead><tr><th>Field</th><th>Battery</th><th>Meter</th><th>Next Inspection</th><th>Frequency</th><th>Update</th></tr></thead>
        <tbody>
          {% for m in due_week %}
          <tr>
            <td>{{ m.battery.field.name }}</td>
            <td>{{ m.battery.name }}</td>
            <td>{{ m.meter_name }}</td>
            <td>{{ m.next_inspection }}</td>
            <td>{{ m.frequency or '—' }}</td>
            <td>
              <form class="inline" method="post" action="{{ url_for('mark_tested_today', meter_id=m.id) }}?back=due" onsubmit="return confirm('Mark as tested today?');">
                <input name="new_h2s" placeholder="H2S" value="{{ m.h2s_ppm or '' }}" style="width:70px; margin-right:6px;">
                <select name="reason" style="width:170px; margin-right:6px;">
                  {% for r in quick_reasons %}<option value="{{ r }}">{{ r }}</option>{% endfor %}
                </select>
                <input name="note" placeholder="note (optional)" style="width:180px; margin-right:6px;">
                <button class="btn">Mark Tested Today</button>
              </form>
              <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <div class="muted">No tests due this week.</div>
    {% endif %}
  </div>
{% endblock %}
"""

FIELDS = """
{% extends "base.html" %}
{% block content %}
  <h1>Fields</h1>
  <div class="card">
    <h3>Add Field</h3>
    <form method="post" action="{{ url_for('add_field') }}">
      <label>Name</label><input name="name" required />
      <label>Location</label><input name="location" />
      <p><button class="btn">Save</button></p>
    </form>
  </div>
  <h2>All Fields</h2>
  <ul>
  {% for f in fields %}
    <li>
      <a href="{{ url_for('list_batteries', field_id=f.id) }}">{{ f.name }}</a>
      {% if f.location %}<span class="muted"> — {{ f.location }}</span>{% endif %}
      &nbsp; <a class="btn" href="{{ url_for('edit_field', field_id=f.id) }}">Edit</a>
    </li>
  {% endfor %}
  </ul>
{% endblock %}
"""

FIELD_EDIT = """
{% extends "base.html" %}
{% block content %}
  <h1>Edit Field</h1>
  <form method="post">
    <label>Name</label><input name="name" value="{{ field.name }}" required />
    <label>Location</label><input name="location" value="{{ field.location or '' }}" />
    <p class="row">
      <button class="btn">Save Changes</button>
      <a class="btn" href="{{ url_for('list_batteries', field_id=field.id) }}">Back</a>
    </p>
  </form>
{% endblock %}
"""

BATTERIES = """
{% extends "base.html" %}
{% block content %}
  <h1>{{ field.name }} — Central Tank Batteries</h1>
  <p><a href="{{ url_for('list_fields') }}">← back to Fields</a></p>

  <div class="card">
    <h3>Add Battery</h3>
    <form method="post" action="{{ url_for('add_battery') }}">
      <label>Battery Name</label><input name="name" required placeholder="CTB-101" />
      <label>Assign to Field</label>
      <select name="field_id" required>
        {% for f in all_fields %}
          <option value="{{ f.id }}" {% if f.id==field.id %}selected{% endif %}>{{ f.name }}</option>
        {% endfor %}
      </select>
      <label>Notes</label><textarea name="notes" rows="2"></textarea>
      <p><button class="btn">Add Battery</button></p>
    </form>
  </div>

  <h2>Existing Batteries</h2>
  <div class="grid">
  {% for b in batteries %}
    <div class="card">
      <h3>{{ b.name }}</h3>
      <div class="muted">Field: {{ b.field.name }}</div>
      {% if b.notes %}<div class="muted">{{ b.notes }}</div>{% endif %}
      <p class="row">
        <a class="btn" href="{{ url_for('battery_detail', battery_id=b.id) }}">Open Meters</a>
        <a class="btn" href="{{ url_for('edit_battery', battery_id=b.id) }}">Edit</a>
        <form class="inline" method="post" action="{{ url_for('delete_battery', battery_id=b.id) }}" onsubmit="return confirm('Delete this Battery and ALL its meters?');">
          <button class="btn danger">Delete</button>
        </form>
      </p>
    </div>
  {% else %}
    <p>No batteries in this field yet.</p>
  {% endfor %}
  </div>
{% endblock %}
"""

BATTERY_EDIT = """
{% extends "base.html" %}
{% block content %}
  <h1>Edit Battery</h1>
  <form method="post">
    <label>Name</label><input name="name" value="{{ battery.name }}" required />
    <label>Assign to Field</label>
    <select name="field_id" required>
      {% for f in all_fields %}
        <option value="{{ f.id }}" {% if f.id==battery.field_id %}selected{% endif %}>{{ f.name }}</option>
      {% endfor %}
    </select>
    <label>Notes</label><textarea name="notes" rows="2">{{ battery.notes or '' }}</textarea>
    <p class="row">
      <button class="btn">Save Changes</button>
      <a class="btn" href="{{ url_for('list_batteries', field_id=battery.field_id) }}">Back</a>
    </p>
  </form>
{% endblock %}
"""

BATTERY_DETAIL = """
{% extends "base.html" %}
{% block content %}
  <h1>{{ battery.name }} — Meters</h1>
  <p>In Field: <b><a href="{{ url_for('list_batteries', field_id=battery.field.id) }}">{{ battery.field.name }}</a></b></p>

  <div class="card">
    <h3>Add Meter to {{ battery.name }}</h3>
    <form method="post" action="{{ url_for('add_meter', battery_id=battery.id) }}">
      <table class="sheet">
        <colgroup>
          <col><col class="tight"><col><col>
          <col><col><col><col>
          <col class="tight"><col class="tight">
          <col class="tight"><col class="tight">
        </colgroup>
        <thead>
          <tr>
            <th>Meter Name</th>
            <th>Flow Cal ID</th>
            <th>Purchaser Name</th>
            <th>Purchaser Meter ID</th>
            <th>Meter Type</th>
            <th>Meter Address</th>
            <th>Device S/N</th>
            <th>Tube S/N</th>
            <th>Tube Size</th>
            <th>Orifice/Plate Size</th>
            <th>H2S (PPM)</th>
            <th>Frequency</th>
            <th>Last Test</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><input name="meter_name" required></td>
            <td><input name="flow_cal_id"></td>
            <td><input name="purchaser_name"></td>
            <td><input name="purchaser_meter_id"></td>
            <td><input name="meter_type"></td>
            <td><input name="meter_address"></td>
            <td><input name="serial_number"></td>
            <td><input name="tube_serial_number"></td>
            <td><input name="tube_size"></td>
            <td><input name="orifice_plate_size"></td>
            <td><input name="h2s_ppm"></td>
            <td>
              <select name="frequency">
                <option value="">—</option>
                <option>Monthly</option>
                <option>Quarterly</option>
                <option>Semiannual</option>
                <option>Annual</option>
                <option>Out of Service</option>
              </select>
            </td>
            <td><input name="last_test_date" placeholder="YYYY-MM-DD"></td>
          </tr>
        </tbody>
      </table>

      <div class="notes-wrap">
        <label class="tiny"><input type="checkbox" name="hist_add"> Also add this to history</label>
        <div class="note">Next Inspection is auto-calculated from <b>Last Test</b> + <b>Frequency</b>.</div>
        <label style="margin-top:10px;">Notes</label>
        <textarea name="notes" rows="2"></textarea>
        <p style="margin-top:10px;"><button class="btn primary">Add Meter</button></p>
      </div>
    </form>
  </div>

  <h2>Meters in {{ battery.name }}</h2>
  <div class="grid">
    {% for m in meters %}
      <div class="card">
        <h3>{{ m.meter_name }}</h3>
        <div class="muted">Flow Cal ID: {{ m.flow_cal_id or '—' }}</div>
        {% if m.purchaser_name or m.purchaser_meter_id %}
          <div class="muted">Purchaser: {{ m.purchaser_name or '—' }} | Purchaser Meter ID: {{ m.purchaser_meter_id or '—' }}</div>
        {% endif %}
        <div class="muted">Type: {{ m.meter_type or '—' }} | Addr: {{ m.meter_address or '—' }}</div>
        <div class="muted">Tube: {{ m.tube_size or '—' }} | Plate: {{ m.orifice_plate_size or '—' }} | H2S: {{ m.h2s_ppm or '—' }}</div>
        <div class="muted">Last Test: {{ m.last_test_date or '—' }} | Next Insp: {{ m.next_inspection or '—' }}</div>
        {% if m.frequency %}
          {% if m.frequency == 'Out of Service' %}
            <div><span class="pill pill-oos">Out of Service</span></div>
          {% else %}
            <div class="muted">Frequency: {{ m.frequency }}</div>
          {% endif %}
        {% endif %}
        {% if m.notes %}<p>{{ m.notes }}</p>{% endif %}
        <p class="row">
          <a class="btn" href="{{ url_for('edit_meter', meter_id=m.id) }}">Edit</a>
          <a class="btn" href="{{ url_for('meter_history', meter_id=m.id) }}">History</a>
          <form class="inline" method="post" action="{{ url_for('delete_meter', meter_id=m.id) }}" onsubmit="return confirm('Delete this Meter?');">
            <button class="btn danger">Delete</button>
          </form>
        </p>
      </div>
    {% else %}
      <p class="muted">No meters yet.</p>
    {% endfor %}
  </div>
{% endblock %}
"""


METER_EDIT = """
{% extends "base.html" %}
{% block content %}
  <h1>Edit Meter</h1>
  <form method="post">
    <div class="grid">
      <div><label>Meter Name</label><input name="meter_name" value="{{ m.meter_name }}" required /></div>
      <div><label>Flow Cal ID</label><input name="flow_cal_id" value="{{ m.flow_cal_id or '' }}" /></div>
      <div><label>Purchaser Name (optional)</label><input name="purchaser_name" value="{{ m.purchaser_name or '' }}" /></div>
      <div><label>Purchaser Meter ID (optional)</label><input name="purchaser_meter_id" value="{{ m.purchaser_meter_id or '' }}" /></div>
      <div><label>Meter Type</label><input name="meter_type" value="{{ m.meter_type or '' }}" /></div>
      <div><label>Meter Address</label><input name="meter_address" value="{{ m.meter_address or '' }}" /></div>
      <div><label>Device S/N</label><input name="serial_number" value="{{ m.serial_number or '' }}" /></div>
      <div><label>Tube S/N</label><input name="tube_serial_number" value="{{ m.tube_serial_number or '' }}" /></div>
      <div><label>Tube Size</label><input name="tube_size" value="{{ m.tube_size or '' }}" /></div>
      <div><label>Orifice/Plate Size</label><input name="orifice_plate_size" value="{{ m.orifice_plate_size or '' }}" /></div>
      <div><label>H2S (PPM)</label><input name="h2s_ppm" value="{{ m.h2s_ppm or '' }}" /></div>
      <div>
        <label>Inspection Frequency</label>
        <select name="frequency">
          {% for opt in ["","Monthly","Quarterly","Semiannual","Annual","Out of Service"] %}
            <option value="{{ opt }}" {% if (m.frequency or '')==opt %}selected{% endif %}>{{ opt or '—' }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>Last Test (YYYY-MM-DD)</label><input name="last_test_date" value="{{ m.last_test_date or '' }}" /></div>
    </div>
    <label class="tiny"><input type="checkbox" name="hist_add" /> Also add this to history on save</label>
    <div class="note">Next Inspection is auto-calculated from Last Test + Frequency (after save).</div>
    <label>Notes</label><textarea name="notes" rows="2">{{ m.notes or '' }}</textarea>
    <p class="row">
      <button class="btn">Save Changes</button>
      <a class="btn" href="{{ url_for('battery_detail', battery_id=m.battery_id) }}">Back</a>
    </p>
  </form>
{% endblock %}
"""

HISTORY_PAGE = """
{% extends "base.html" %}
{% block content %}
  <h1>History — {{ meter.meter_name }}</h1>
  <p>Battery: <a href="{{ url_for('battery_detail', battery_id=meter.battery_id) }}">{{ meter.battery.name }}</a> · Field: {{ meter.battery.field.name }}</p>

  <div class="card">
    <h3>Add History Entry</h3>
    <form method="post" action="{{ url_for('add_history', meter_id=meter.id) }}">
      <div class="grid">
        <div><label>Event Date (YYYY-MM-DD)</label><input name="event_date" placeholder="{{ today }}" required /></div>
        <div><label>H2S (PPM)</label><input name="h2s_ppm" value="{{ meter.h2s_ppm or '' }}" /></div>
      </div>
      <label>Notes</label><textarea name="notes" rows="2"></textarea>
      <p class="row">
        <button class="btn">Add</button>
        <a class="btn" href="{{ url_for('edit_meter', meter_id=meter.id) }}">Edit Meter</a>
      </p>
      <p class="tiny">Tip: adding a history entry does not change Next Inspection; use “Mark Tested Today” or change Last Test on the Edit page if you want to roll the schedule.</p>
    </form>
  </div>

  <div class="card">
    <h3>Entries</h3>
    {% if history %}
      <table>
        <thead><tr><th>Date</th><th>H2S (PPM)</th><th>Notes</th><th>Logged</th><th></th></tr></thead>
        <tbody>
          {% for h in history %}
            <tr>
              <td>{{ h.event_date }}</td>
              <td>{{ h.h2s_ppm or '—' }}</td>
              <td>{{ h.notes or '—' }}</td>
              <td class="tiny">{{ h.created_at.strftime("%Y-%m-%d %H:%M") }} ({{ h.created_via or 'manual' }})</td>
              <td>
                <form class="inline" method="post" action="{{ url_for('delete_history', hist_id=h.id) }}" onsubmit="return confirm('Delete this history entry?');">
                  <button class="btn danger">Delete</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <div class="muted">No history yet.</div>
    {% endif %}
  </div>
{% endblock %}
"""

# register base template
app.jinja_loader = ChoiceLoader([app.jinja_loader, DictLoader({"base.html": BASE})])

# ------------------------
# Routes
# ------------------------
@app.route("/")
def home():
    fields = Field.query.options(
        joinedload(Field.batteries).joinedload(Battery.meters)
    ).order_by(Field.name).all()

    week_start, week_end = week_bounds()
    due_q = (Meter.query
             .options(joinedload(Meter.battery).joinedload(Battery.field))
             .filter(Meter.frequency.isnot(None),
                     Meter.frequency != "Out of Service",
                     Meter.next_inspection.isnot(None),
                     Meter.next_inspection <= week_end))
    due_all = sorted(due_q.all(),
        key=lambda m: (m.battery.field.name, m.battery.name, m.next_inspection, m.meter_name))
    overdue = [m for m in due_all if m.next_inspection < week_start]
    due_week = [m for m in due_all if week_start <= m.next_inspection <= week_end]

    return render_template_string(HOME,
        fields=fields, week_start=week_start, week_end=week_end,
        overdue=overdue, due_week=due_week, quick_reasons=QUICK_REASONS)

@app.route("/due")
def due():
    week_start, week_end = week_bounds()
    due_q = (Meter.query
             .options(joinedload(Meter.battery).joinedload(Battery.field))
             .filter(Meter.frequency.isnot(None),
                     Meter.frequency != "Out of Service",
                     Meter.next_inspection.isnot(None),
                     Meter.next_inspection <= week_end))
    due_all = sorted(due_q.all(),
        key=lambda m: (m.battery.field.name, m.battery.name, m.next_inspection, m.meter_name))
    overdue = [m for m in due_all if m.next_inspection < week_start]
    due_week = [m for m in due_all if week_start <= m.next_inspection <= week_end]
    return render_template_string(DUE_PAGE,
        overdue=overdue, due_week=due_week, week_start=week_start, week_end=week_end,
        quick_reasons=QUICK_REASONS)

# ---- History ----
@app.route("/meters/<int:meter_id>/history")
def meter_history(meter_id):
    m = Meter.query.options(joinedload(Meter.battery).joinedload(Battery.field)).get_or_404(meter_id)
    history = MeterHistory.query.filter_by(meter_id=m.id).order_by(MeterHistory.event_date.desc(), MeterHistory.id.desc()).all()
    return render_template_string(HISTORY_PAGE, meter=m, history=history, today=date.today())

@app.route("/meters/<int:meter_id>/history/add", methods=["POST"])
def add_history(meter_id):
    m = Meter.query.get_or_404(meter_id)
    ev = parse_ymd(request.form.get("event_date"))
    if not ev:
        flash("Event date is required (YYYY-MM-DD).")
        return redirect(url_for("meter_history", meter_id=m.id))
    h = MeterHistory(
        meter_id=m.id,
        event_date=ev,
        h2s_ppm=(request.form.get("h2s_ppm") or None),
        notes=(request.form.get("notes") or None),
        created_via="manual",
    )
    db.session.add(h)
    db.session.commit()
    flash("History entry added.")
    return redirect(url_for("meter_history", meter_id=m.id))

@app.route("/history/<int:hist_id>/delete", methods=["POST"])
def delete_history(hist_id):
    h = MeterHistory.query.get_or_404(hist_id)
    meter_id = h.meter_id
    db.session.delete(h)
    db.session.commit()
    flash("History entry deleted.")
    return redirect(url_for("meter_history", meter_id=meter_id))

# ---- Fields ----
@app.route("/fields")
def list_fields():
    fields = Field.query.order_by(Field.name).all()
    return render_template_string(FIELDS, fields=fields)

@app.route("/fields/add", methods=["POST"])
def add_field():
    name = request.form.get("name","").strip()
    if not name:
        flash("Field name is required.")
        return redirect(url_for("list_fields"))
    location = request.form.get("location","").strip() or None
    if Field.query.filter_by(name=name).first():
        flash("Field already exists.")
        return redirect(url_for("list_fields"))
    db.session.add(Field(name=name, location=location))
    db.session.commit()
    flash(f"Field '{name}' added.")
    return redirect(url_for("list_fields"))

@app.route("/fields/<int:field_id>/edit", methods=["GET","POST"])
def edit_field(field_id):
    field = Field.query.get_or_404(field_id)
    if request.method == "POST":
        field.name = request.form.get("name","").strip() or field.name
        field.location = request.form.get("location","").strip() or None
        db.session.commit()
        flash("Field updated.")
        return redirect(url_for("list_batteries", field_id=field.id))
    return render_template_string(FIELD_EDIT, field=field)

# ---- Batteries ----
@app.route("/fields/<int:field_id>/batteries")
def list_batteries(field_id):
    field = Field.query.get_or_404(field_id)
    batteries = Battery.query.filter_by(field_id=field.id).order_by(Battery.name).all()
    all_fields = Field.query.order_by(Field.name).all()
    return render_template_string(BATTERIES, field=field, batteries=batteries, all_fields=all_fields)

@app.route("/batteries/add", methods=["POST"])
def add_battery():
    name = request.form.get("name","").strip()
    field_id = request.form.get("field_id")
    if not name or not field_id:
        flash("Battery name and Field are required.")
        return redirect(url_for("list_fields"))
    notes = request.form.get("notes","").strip() or None
    field = Field.query.get_or_404(int(field_id))
    b = Battery(name=name, notes=notes, field_id=field.id)
    db.session.add(b)
    db.session.commit()
    flash(f"Battery '{name}' added to {field.name}.")
    return redirect(url_for("list_batteries", field_id=field.id))

@app.route("/batteries/<int:battery_id>/edit", methods=["GET","POST"])
def edit_battery(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    if request.method == "POST":
        battery.name = request.form.get("name","").strip() or battery.name
        new_field_id = int(request.form.get("field_id"))
        battery.field_id = new_field_id
        battery.notes = request.form.get("notes","").strip() or None
        db.session.commit()
        flash("Battery updated.")
        return redirect(url_for("list_batteries", field_id=new_field_id))
    all_fields = Field.query.order_by(Field.name).all()
    return render_template_string(BATTERY_EDIT, battery=battery, all_fields=all_fields)

@app.route("/batteries/<int:battery_id>/delete", methods=["POST"])
def delete_battery(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    field_id = battery.field_id
    db.session.delete(battery)  # cascades to meters
    db.session.commit()
    flash("Battery deleted.")
    return redirect(url_for("list_batteries", field_id=field_id))

@app.route("/batteries/<int:battery_id>")
def battery_detail(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    meters = Meter.query.filter_by(battery_id=battery.id).order_by(Meter.meter_name).all()
    return render_template_string(BATTERY_DETAIL, battery=battery, meters=meters)

# ---- Meters ----
@app.route("/batteries/<int:battery_id>/meters/add", methods=["POST"])
def add_meter(battery_id):
    battery = Battery.query.get_or_404(battery_id)
    f = request.form
    last = parse_ymd(f.get("last_test_date"))
    freq = f.get("frequency") or None
    next_calc = compute_next(last, freq)

    m = Meter(
        meter_name=f.get("meter_name","").strip(),
        flow_cal_id=f.get("flow_cal_id") or None,
        purchaser_name=f.get("purchaser_name") or None,
        purchaser_meter_id=f.get("purchaser_meter_id") or None,
        meter_type=f.get("meter_type") or None,
        meter_address=f.get("meter_address") or None,
        serial_number=f.get("serial_number") or None,
        tube_serial_number=f.get("tube_serial_number") or None,
        tube_size=f.get("tube_size") or None,
        orifice_plate_size=f.get("orifice_plate_size") or None,
        h2s_ppm=f.get("h2s_ppm") or None,
        notes=f.get("notes") or None,
        frequency=freq,
        last_test_date=last,
        next_inspection=next_calc,
        battery_id=battery.id,
    )
    if not m.meter_name:
        flash("Meter Name is required.")
        return redirect(url_for("battery_detail", battery_id=battery.id))
    db.session.add(m)
    db.session.commit()

    if "hist_add" in f and last:
        db.session.add(MeterHistory(meter_id=m.id, event_date=last, h2s_ppm=m.h2s_ppm, notes=m.notes, created_via="manual"))
        db.session.commit()

    flash(f"Meter '{m.meter_name}' added to {battery.name}.")
    return redirect(url_for("battery_detail", battery_id=battery.id))

@app.route("/meters/<int:meter_id>/edit", methods=["GET","POST"])
def edit_meter(meter_id):
    m = Meter.query.get_or_404(meter_id)
    if request.method == "POST":
        f = request.form
        m.meter_name = f.get("meter_name","").strip() or m.meter_name
        m.flow_cal_id = f.get("flow_cal_id") or None
        m.purchaser_name = f.get("purchaser_name") or None
        m.purchaser_meter_id = f.get("purchaser_meter_id") or None
        m.meter_type = f.get("meter_type") or None
        m.meter_address = f.get("meter_address") or None
        m.serial_number = f.get("serial_number") or None
        m.tube_serial_number = f.get("tube_serial_number") or None
        m.tube_size = f.get("tube_size") or None
        m.orifice_plate_size = f.get("orifice_plate_size") or None
        m.h2s_ppm = f.get("h2s_ppm") or None
        m.frequency = f.get("frequency") or None
        new_last = parse_ymd(f.get("last_test_date"))
        m.last_test_date = new_last
        m.next_inspection = compute_next(m.last_test_date, m.frequency)
        m.notes = f.get("notes") or None
        db.session.commit()

        if "hist_add" in f and new_last:
            db.session.add(MeterHistory(meter_id=m.id, event_date=new_last, h2s_ppm=m.h2s_ppm, notes=m.notes, created_via="edit"))
            db.session.commit()

        flash("Meter updated.")
        return redirect(url_for("battery_detail", battery_id=m.battery_id))
    return render_template_string(METER_EDIT, m=m)

@app.route("/meters/<int:meter_id>/delete", methods=["POST"])
def delete_meter(meter_id):
    m = Meter.query.get_or_404(meter_id)
    battery_id = m.battery_id
    db.session.delete(m)
    db.session.commit()
    flash("Meter deleted.")
    return redirect(url_for("battery_detail", battery_id=battery_id))

@app.route("/meters/<int:meter_id>/mark_tested", methods=["POST"])
def mark_tested_today(meter_id):
    """Set last_test_date=today, optionally update H2S, roll next_inspection, and log history with quick reason."""
    m = Meter.query.get_or_404(meter_id)
    today = date.today()

    new_h2s = (request.form.get("new_h2s") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    note_in = (request.form.get("note") or "").strip()

    if new_h2s != "":
        m.h2s_ppm = new_h2s

    m.last_test_date = today
    m.next_inspection = compute_next(today, m.frequency)

    parts = []
    if reason and reason != "—":
        parts.append(reason)
    if note_in:
        parts.append(note_in)
    if not parts:
        parts.append("Marked tested" + ("" if new_h2s != "" else " (no H2S sample)"))
    hist_note = " — ".join(parts)

    hist_h2s = new_h2s if new_h2s != "" else None

    db.session.add(MeterHistory(
        meter_id=m.id,
        event_date=today,
        h2s_ppm=hist_h2s,
        notes=hist_note,
        created_via="mark_tested",
    ))
    db.session.commit()

    flash(f"Marked '{m.meter_name}' as tested on {today}.")
    back = request.args.get("back")
    return redirect(url_for("due" if back == "due" else "home"))

# ------------------------
# Jinja base registration (inline base.html)
# ------------------------
app.jinja_loader = ChoiceLoader([app.jinja_loader, DictLoader({"base.html": BASE})])

# ------------------------
# Run
# ------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting on port {port} — DB: {DB_URI}")
    app.run(host="0.0.0.0", port=port, debug=False)
