"""Tuition attendance app. Run with: python -m streamlit run app.py"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import csv
import html
import io
import os
from pathlib import Path
import re
import sqlite3
import unicodedata

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("TUITION_DATA_DIR", str(APP_DIR / "data")))
DB_PATH = DATA_DIR / "tuition.db"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STATUSES = ("Present", "Absent")
CSV_COLUMNS = (
    "date", "batch", "start_time", "end_time", "student_id", "student_name",
    "status", "saved_at",
)

STYLE = """
<style>
    .stMainBlockContainer { max-width: 850px; padding-top: 2rem; padding-bottom: 3rem; }
    h1 { font-size: clamp(1.8rem, 5vw, 2.6rem) !important; letter-spacing: -.04em; }
    .intro { color: #64748b; margin: -.6rem 0 1.4rem; }
    .eyebrow { color: #4f46e5; font-size: .78rem; font-weight: 700;
               letter-spacing: .12em; text-transform: uppercase; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
             gap: .65rem; margin: .6rem 0 1.4rem; }
    .stat { border: 1px solid #e2e8f0; border-radius: 14px; padding: .85rem;
            background: #ffffff; color: #172033; }
    .stat strong { display: block; font-size: 1.65rem; line-height: 1.3; }
    .stat span { font-size: .82rem; color: #64748b; }
    .stButton button, .stDownloadButton button,
    [data-testid="stFormSubmitButton"] button { min-height: 48px; border-radius: 12px; }
    [data-testid="stRadio"] [role="radiogroup"] { gap: .65rem; flex-wrap: wrap; }
    [data-testid="stRadio"] label[data-baseweb="radio"] {
        min-height: 44px; padding: .45rem .7rem; border: 1px solid #cbd5e1;
        border-radius: 10px; margin-right: 0;
    }
    [data-testid="stTabs"] button { min-height: 48px; }
    @media (max-width: 600px) {
        .stMainBlockContainer { padding: 1.1rem .8rem 3rem; }
        .stat { padding: .65rem; }
        .stat strong { font-size: 1.45rem; }
        .stat span { font-size: .75rem; }
    }
</style>
"""


@contextmanager
def database():
    """Use a fresh connection per operation, including across browser sessions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_database():
    with database() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                weekdays TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                enrolled_on TEXT NOT NULL,
                UNIQUE (name_key, batch_id)
            );
            CREATE TABLE IF NOT EXISTS attendance (
                date TEXT NOT NULL,
                batch_id INTEGER NOT NULL REFERENCES batches(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                status TEXT NOT NULL CHECK (status IN ('Present', 'Absent')),
                saved_at TEXT NOT NULL,
                PRIMARY KEY (date, batch_id, student_id)
            );
        """)


def clean_name(value, maximum=80):
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not value:
        raise ValueError("Please enter a name.")
    if len(value) > maximum:
        raise ValueError(f"Please keep the name within {maximum} characters.")
    return value


def add_batch(name, start, end, weekdays):
    name = clean_name(name, 60)
    if end <= start:
        raise ValueError("The end time must be later than the start time.")
    if not weekdays or any(day not in WEEKDAYS for day in weekdays):
        raise ValueError("Choose at least one valid weekday.")
    days = ",".join(str(i) for i, day in enumerate(WEEKDAYS) if day in weekdays)
    with database() as conn:
        return conn.execute(
            "INSERT INTO batches (name, name_key, start_time, end_time, weekdays) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, name.casefold(), start.strftime("%H:%M"), end.strftime("%H:%M"), days),
        ).lastrowid


def add_student(name, batch_id, enrolled_on):
    name = clean_name(name)
    with database() as conn:
        return conn.execute(
            "INSERT INTO students (name, name_key, batch_id, enrolled_on) VALUES (?, ?, ?, ?)",
            (name, name.casefold(), batch_id, enrolled_on.isoformat()),
        ).lastrowid


def get_batches():
    with database() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM batches ORDER BY start_time, name_key"
        )]


def get_students(batch_id=None, on_date=None):
    query = """
        SELECT s.*, b.name AS batch, b.start_time, b.end_time
        FROM students s JOIN batches b ON b.id = s.batch_id WHERE 1 = 1
    """
    params = []
    if batch_id is not None:
        query += " AND s.batch_id = ?"
        params.append(batch_id)
    if on_date is not None:
        query += " AND s.enrolled_on <= ?"
        params.append(on_date.isoformat())
    query += " ORDER BY b.start_time, s.name_key, s.id"
    with database() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def get_attendance(batch_id, on_date):
    with database() as conn:
        return dict(conn.execute(
            "SELECT student_id, status FROM attendance WHERE batch_id = ? AND date = ?",
            (batch_id, on_date.isoformat()),
        ).fetchall())


def save_attendance(batch_id, on_date, marks):
    """Upsert selected students atomically; unmarked students are never made absent."""
    if not marks or any(status not in STATUSES for status in marks.values()):
        raise ValueError("Choose Present or Absent for at least one student.")
    with database() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None or str(on_date.weekday()) not in batch["weekdays"].split(","):
            raise ValueError("This batch is not scheduled for the selected date.")
        roster = {row["id"] for row in conn.execute(
            "SELECT id FROM students WHERE batch_id = ? AND enrolled_on <= ?",
            (batch_id, on_date.isoformat()),
        )}
        if not set(marks).issubset(roster):
            raise ValueError("A student is not enrolled in this batch on the selected date.")
        saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.executemany("""
            INSERT INTO attendance (date, batch_id, student_id, status, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (date, batch_id, student_id) DO UPDATE SET
                status = excluded.status, saved_at = excluded.saved_at
        """, [(on_date.isoformat(), batch_id, student_id, status, saved_at)
              for student_id, status in marks.items()])
    return len(marks)


def get_records(start_date, end_date, batch_id=None):
    query = """
        SELECT a.date, b.name AS batch, b.start_time, b.end_time,
               s.id AS student_id, s.name AS student_name, a.status, a.saved_at
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        JOIN batches b ON b.id = a.batch_id
        WHERE a.date BETWEEN ? AND ?
    """
    params = [start_date.isoformat(), end_date.isoformat()]
    if batch_id is not None:
        query += " AND a.batch_id = ?"
        params.append(batch_id)
    query += " ORDER BY a.date, b.start_time, b.name_key, s.name_key, s.id"
    with database() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def records_to_csv(records):
    """UTF-8 BOM supports Excel; quote formula-like text before spreadsheet export."""
    def safe_cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for row in records:
        writer.writerow(safe_cell(row[column]) for column in CSV_COLUMNS)
    return output.getvalue().encode("utf-8-sig")


def time_label(value):
    return time.fromisoformat(value).strftime("%I:%M %p").lstrip("0")


def batch_label(batch):
    return f"{batch['name']} · {time_label(batch['start_time'])} – {time_label(batch['end_time'])}"


def markdown_text(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def show_stats(present, absent, unmarked):
    st.markdown(
        '<div class="stats">'
        f'<div class="stat"><strong>{present}</strong><span>Present</span></div>'
        f'<div class="stat"><strong>{absent}</strong><span>Absent</span></div>'
        f'<div class="stat"><strong>{unmarked}</strong><span>Unmarked</span></div>'
        '</div>', unsafe_allow_html=True,
    )


def set_all_statuses(keys, status):
    for key in keys:
        st.session_state[key] = status


def attendance_page(batches, today):
    selected_date = st.date_input("Attendance date", value=today, max_value=today)
    scheduled = [b for b in batches if str(selected_date.weekday()) in b["weekdays"].split(",")]
    st.caption(f"{selected_date.strftime('%A, %d %B %Y')} · {len(scheduled)} scheduled batches")
    if not batches:
        st.info("Open Students to create your first batch and add students.")
        return
    if not scheduled:
        st.info("There are no batches scheduled for this weekday.")
        return

    rosters = {b["id"]: get_students(b["id"], selected_date) for b in scheduled}
    saved = {b["id"]: get_attendance(b["id"], selected_date) for b in scheduled}
    statuses = [saved[b["id"]].get(s["id"]) for b in scheduled for s in rosters[b["id"]]]
    show_stats(statuses.count("Present"), statuses.count("Absent"), statuses.count(None))
    st.subheader("Daily batches")
    st.caption("Counts show saved records. Save each batch before changing the date or leaving the app.")

    for batch in scheduled:
        batch_id = batch["id"]
        students = rosters[batch_id]
        with st.expander(batch_label(batch), expanded=len(scheduled) == 1):
            if not students:
                st.info("No students are enrolled in this batch on this date.")
                continue
            st.caption(f"{len(students)} students · Unmarked students are skipped when you save.")
            keys = [f"mark_{selected_date.isoformat()}_{batch_id}_{s['id']}" for s in students]
            left, right = st.columns(2)
            left.button("All present", key=f"present_{batch_id}", width="stretch",
                        on_click=set_all_statuses, args=(keys, "Present"))
            right.button("All absent", key=f"absent_{batch_id}", width="stretch",
                         on_click=set_all_statuses, args=(keys, "Absent"))

            with st.form(f"attendance_{selected_date.isoformat()}_{batch_id}"):
                marks = {}
                for student, key in zip(students, keys):
                    previous = saved[batch_id].get(student["id"])
                    status = st.radio(
                        f"**{markdown_text(student['name'])}**",
                        STATUSES, index=STATUSES.index(previous) if previous in STATUSES else None,
                        horizontal=True, key=key, help=f"Student ID: {student['id']}",
                    )
                    if status is not None:
                        marks[student["id"]] = status
                submitted = st.form_submit_button("Save attendance", type="primary", width="stretch")
                if submitted:
                    try:
                        count = save_attendance(batch_id, selected_date, marks)
                    except ValueError as exc:
                        st.warning(str(exc))
                    else:
                        st.session_state["notice"] = (
                            f"Saved {count} attendance records for {batch['name']} on {selected_date}."
                        )
                        st.rerun()


def students_page(batches, today):
    st.subheader("Add a student")
    if batches:
        batch_by_id = {b["id"]: b for b in batches}
        with st.form("add_student", clear_on_submit=True):
            name = st.text_input("Student name", max_chars=80, placeholder="e.g. Aanya Sharma")
            batch_id = st.selectbox("Assign a batch", list(batch_by_id),
                                    format_func=lambda value: batch_label(batch_by_id[value]))
            enrolled_on = st.date_input("Enrollment date", value=today, max_value=today,
                                        help="The student appears in attendance from this date onward.")
            if st.form_submit_button("Add student", type="primary", width="stretch"):
                try:
                    add_student(name, batch_id, enrolled_on)
                except ValueError as exc:
                    st.warning(str(exc))
                except sqlite3.IntegrityError:
                    st.warning("This student name is already in the batch. Use an initial to distinguish namesakes.")
                else:
                    st.session_state["notice"] = f"Added {clean_name(name)} to {batch_by_id[batch_id]['name']}."
                    st.rerun()
    else:
        st.info("Create a batch below, then add your students.")

    with st.expander("Create a batch", expanded=not batches):
        with st.form("add_batch", clear_on_submit=True):
            name = st.text_input("Batch name", max_chars=60, placeholder="e.g. Class 8 Mathematics")
            start = st.time_input("Start time", value=time(16, 0), step=timedelta(minutes=15))
            end = st.time_input("End time", value=time(17, 0), step=timedelta(minutes=15))
            weekdays = st.multiselect("Runs on", WEEKDAYS, default=list(WEEKDAYS))
            if st.form_submit_button("Create batch", type="primary", width="stretch"):
                try:
                    add_batch(name, start, end, weekdays)
                except ValueError as exc:
                    st.warning(str(exc))
                except sqlite3.IntegrityError:
                    st.warning("A batch with this name already exists. Please use a different name.")
                else:
                    st.session_state["notice"] = f"Created {clean_name(name, 60)}. You can now add students."
                    st.rerun()

    students = get_students()
    with st.expander(f"Student directory · {len(students)} students"):
        if not students:
            st.caption("Your students will appear here.")
        else:
            search = st.text_input("Find a student", placeholder="Search by name")
            visible = [s for s in students if search.casefold() in s["name"].casefold()]
            if not visible:
                st.info("No students match your search.")
            for student in visible:
                with st.container(border=True):
                    st.markdown(f"**{markdown_text(student['name'])}**")
                    st.caption(batch_label({**student, "name": student["batch"]}))
                    st.caption(f"Student #{student['id']} · Enrolled {student['enrolled_on']}")


def records_page(batches, today):
    st.subheader("Attendance records")
    with database() as conn:
        earliest = conn.execute("SELECT MIN(date) FROM attendance").fetchone()[0]
    first_date = date.fromisoformat(earliest) if earliest else today.replace(day=1)
    start_date = st.date_input("From", value=first_date, max_value=today, key="export_from")
    end_date = st.date_input("To", value=today, max_value=today, key="export_to")
    batch_by_id = {b["id"]: b for b in batches}
    batch_id = st.selectbox("Filter by batch", [None, *batch_by_id],
                            format_func=lambda value: "All batches" if value is None
                            else batch_label(batch_by_id[value]))
    if start_date > end_date:
        st.warning("The From date must be on or before the To date.")
        return
    records = get_records(start_date, end_date, batch_id)
    if not records:
        st.info("No saved attendance records match these filters.")
        return
    present = sum(r["status"] == "Present" for r in records)
    st.caption(f"{len(records)} saved records · {present} present · {len(records) - present} absent")
    st.download_button(
        "Download attendance CSV", data=records_to_csv(records),
        file_name=f"attendance_{start_date}_{end_date}.csv", mime="text/csv",
        type="primary", width="stretch", on_click="ignore",
    )
    preview = [{"Date": r["date"], "Student": r["student_name"], "Batch": r["batch"],
                "Status": r["status"]} for r in records]
    st.dataframe(preview, hide_index=True, width="stretch")


def main():
    st.set_page_config(page_title="Tuition · Attendance", page_icon="📚", layout="centered")
    st.markdown(STYLE, unsafe_allow_html=True)
    st.markdown('<div class="eyebrow">Your daily teaching companion</div>', unsafe_allow_html=True)
    st.title("Tuition, organized.")
    st.markdown('<p class="intro">Your batches, students, and attendance — together.</p>',
                unsafe_allow_html=True)
    try:
        initialize_database()
        if notice := st.session_state.pop("notice", None):
            st.success(notice)
        batches = get_batches()
        today = date.today()  # Uses the local computer's date and timezone.
        attendance, students, records = st.tabs(["Attendance", "Students", "Records"])
        with attendance:
            attendance_page(batches, today)
        with students:
            students_page(batches, today)
        with records:
            records_page(batches, today)
    except (sqlite3.Error, OSError) as exc:
        st.error("The app could not access its saved data. Check that the data folder is writable, then retry.")
        st.caption(html.escape(str(exc)))


if __name__ == "__main__":
    main()
