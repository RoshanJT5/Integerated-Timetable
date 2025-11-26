from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file, session, flash, abort
from models import db, Course, Faculty, Room, Student, TimeSlot, TimetableEntry, User, PeriodConfig, BreakConfig, StudentGroup
from scheduler import TimetableGenerator
from functools import wraps
import csv
import io
from datetime import datetime
import json
import secrets
import math
import os

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

def time_to_minutes(time_str):
    """Convert time string (HH:MM) to minutes since midnight"""
    h, m = map(int, time_str.split(':'))
    return h * 60 + m

def minutes_to_time(minutes):
    """Convert minutes since midnight to time string (HH:MM)"""
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"

def ensure_column(table_name, column_name, ddl):
    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns(table_name)]
    if column_name in columns:
        return
    with db.engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
    print(f"Added missing column {column_name} to {table_name}.")

def hydrate_default_faculty_values():
    updated = False
    for faculty in Faculty.query.all():
        if faculty.min_hours_per_week is None:
            faculty.min_hours_per_week = 4
            updated = True
        if faculty.max_hours_per_week is None:
            faculty.max_hours_per_week = 16
            updated = True
        if not faculty.availability:
            faculty.availability = "{}"
            updated = True
    if updated:
        db.session.commit()

def create_faculty_profile(payload):
    username = payload.get('username', '').strip() or None
    raw_password = payload.get('password', '').strip()
    generated_password = None

    user = None
    if username:
        existing_faculty = Faculty.query.filter_by(username=username).first()
        if existing_faculty:
            raise ValueError('Username already assigned to another faculty profile.')
        user = User.query.filter_by(username=username).first()
        if user:
            user.role = 'teacher'
            user.name = payload['name']
            if raw_password:
                user.set_password(raw_password)
        else:
            password_to_use = raw_password or secrets.token_urlsafe(8)
            generated_password = None if raw_password else password_to_use
            email = payload.get('email') or f'{username}@faculty.local'
            email = email.strip()
            existing_email_user = User.query.filter_by(email=email).first()
            if existing_email_user:
                email = f'{username}+{secrets.token_hex(3)}@faculty.local'
            user = User(username=username, email=email, role='teacher', name=payload['name'])
            user.set_password(password_to_use)
            db.session.add(user)
            db.session.flush()

    availability_payload = payload.get('availability', '{}')
    if isinstance(availability_payload, dict):
        availability_payload = json.dumps(availability_payload)

    expertise_payload = normalize_comma_list(payload.get('expertise', []))

    faculty = Faculty(
        name=payload['name'],
        email=payload.get('email', ''),
        expertise=','.join(expertise_payload),
        availability=availability_payload,
        username=username,
        min_hours_per_week=int(payload.get('min_hours_per_week', 4)),
        max_hours_per_week=int(payload.get('max_hours_per_week', 16)),
        user_id=user.id if user else None
    )
    db.session.add(faculty)
    return faculty, generated_password

def load_dataframe_from_upload(upload_file):
    filename = upload_file.filename.lower()
    if filename.endswith('.csv'):
        return pd.read_csv(upload_file)
    if filename.endswith('.xlsx') or filename.endswith('.xls'):
        return pd.read_excel(upload_file)
    raise ValueError('Unsupported file type. Upload CSV or Excel.')

def parse_int(value, default=0):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default

def normalize_comma_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text_value = str(value).strip()
    return [text_value] if text_value else []


# Navigation flow for guided setup
def get_next_page(current_page):
    """Get the next page URL in the navigation flow for admin guided setup"""
    navigation_map = {
        'courses': '/faculty',
        'faculty': '/rooms',
        'rooms': '/students',
        'students': '/student-groups',
        'student-groups': '/settings',
        'settings': '/timetable',
        'timetable': None  # Last step
    }
    return navigation_map.get(current_page)

def get_progress_steps(current_page):
    """Get list of all steps with current step marked"""
    steps = [
        {'name': 'courses', 'title': 'Courses', 'icon': 'book'},
        {'name': 'faculty', 'title': 'Faculty', 'icon': 'person-badge'},
        {'name': 'rooms', 'title': 'Rooms', 'icon': 'building'},
        {'name': 'students', 'title': 'Students', 'icon': 'people'},
        {'name': 'student-groups', 'title': 'Groups', 'icon': 'people-fill'},
        {'name': 'settings', 'title': 'Settings', 'icon': 'gear'},
        {'name': 'timetable', 'title': 'Timetable', 'icon': 'calendar-week'}
    ]
    
    current_index = next((i for i, s in enumerate(steps) if s['name'] == current_page), -1)
    
    for i, step in enumerate(steps):
        if i < current_index:
            step['status'] = 'completed'
        elif i == current_index:
            step['status'] = 'active'
        else:
            step['status'] = 'pending'
    
    return steps





def generate_time_slots():
    """Generate time slots based on PeriodConfig and BreakConfig"""
    # Clear existing time slots
    TimeSlot.query.delete()
    
    # Get period configuration
    period_config = PeriodConfig.query.first()
    if not period_config:
        # Use defaults if no config exists
        period_config = PeriodConfig(
            periods_per_day=8,
            period_duration_minutes=60,
            day_start_time='09:00',
            days_of_week='Monday,Tuesday,Wednesday,Thursday,Friday'
        )
        db.session.add(period_config)
        db.session.commit()
    
    # Get break configurations, ordered by after_period
    breaks = BreakConfig.query.order_by(BreakConfig.after_period).all()
    break_map = {br.after_period: br for br in breaks}
    
    days = [d.strip() for d in period_config.days_of_week.split(',')]
    start_minutes = time_to_minutes(period_config.day_start_time)
    period_duration = period_config.period_duration_minutes
    
    current_time = start_minutes
    
    for day in days:
        current_time = start_minutes
        for period_num in range(1, period_config.periods_per_day + 1):
            # Calculate period start and end
            period_start = current_time
            period_end = period_start + period_duration
            
            # Create time slot
            slot = TimeSlot(
                day=day,
                period=period_num,
                start_time=minutes_to_time(period_start),
                end_time=minutes_to_time(period_end)
            )
            db.session.add(slot)
            
            # Move to next period start (after this period ends)
            current_time = period_end
            
            # Check if there's a break after this period
            if period_num in break_map:
                break_config = break_map[period_num]
                current_time += break_config.duration_minutes
    
    db.session.commit()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///timetable.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your-secret-key-change-this-in-production'
db.init_app(app)

# Inject `next_page` into all templates based on a fixed navigation order.
@app.context_processor
def inject_next_page():
    try:
        path = request.path or '/'
    except RuntimeError:
        # No request context; return nothing
        return {'next_page': None}

    # Define the linear navigation order for the Next button
    navigation_order = ['/', '/courses', '/faculty', '/rooms', '/students', '/student-groups', '/settings', '/timetable']

    # Exact match
    if path in navigation_order:
        idx = navigation_order.index(path)
        if idx < len(navigation_order) - 1:
            return {'next_page': navigation_order[idx + 1]}
        return {'next_page': None}

    # Handle subpaths like /courses/add or /faculty/123 by matching prefix
    for i, p in enumerate(navigation_order):
        if p != '/' and path.startswith(p + '/'):
            if i < len(navigation_order) - 1:
                return {'next_page': navigation_order[i + 1]}
            return {'next_page': None}

    return {'next_page': None}

# Initialize database
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        # If there's a schema mismatch, drop and recreate
        print(f"Database schema mismatch detected: {e}")
        print("Dropping and recreating database...")
        db.drop_all()
        db.create_all()
    
    # Create default period config if it doesn't exist
    if PeriodConfig.query.count() == 0:
        period_config = PeriodConfig(
            periods_per_day=8,
            period_duration_minutes=60,
            day_start_time='09:00',
            days_of_week='Monday,Tuesday,Wednesday,Thursday,Friday'
        )
        db.session.add(period_config)
        db.session.commit()
    
    # Create default break configs if they don't exist
    if BreakConfig.query.count() == 0:
        breaks = [
            BreakConfig(break_name='Short Break', after_period=2, duration_minutes=15, order=1),
            BreakConfig(break_name='Lunch Break', after_period=4, duration_minutes=60, order=2),
            BreakConfig(break_name='Short Break', after_period=6, duration_minutes=15, order=3)
        ]
        for br in breaks:
            db.session.add(br)
        db.session.commit()
    
    # Generate time slots based on config if they don't exist
    if TimeSlot.query.count() == 0:
        generate_time_slots()
    
    # Create default admin user if it doesn't exist
    if User.query.filter_by(username='admin').first() is None:
        admin = User(username='admin', email='admin@college.edu', role='admin', name='Administrator')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()

    # Ensure new schema columns exist
    ensure_column('course', 'branch', 'VARCHAR(100)')
    ensure_column('course', 'required_room_tags', 'VARCHAR(255)')
    ensure_column('faculty', 'username', 'VARCHAR(80)')
    ensure_column('faculty', 'min_hours_per_week', 'INTEGER')
    ensure_column('faculty', 'max_hours_per_week', 'INTEGER')
    ensure_column('faculty', 'user_id', 'INTEGER')
    ensure_column('room', 'tags', 'VARCHAR(255)')
    ensure_column('student_group', 'total_students', 'INTEGER')
    ensure_column('student_group', 'batches', 'TEXT')

    hydrate_default_faculty_values()

# Authentication decorators
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user or user.role != 'admin':
            flash('Access denied. Admin privileges required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# Authentication Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            session['name'] = user.name
            flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'danger')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')
        name = request.form.get('name')
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email already exists', 'danger')
            return render_template('register.html')
        
        user = User(username=username, email=email, role=role, name=name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))


@app.route('/download-template/<entity>')
@admin_required
def download_template(entity):
    """Generate a CSV or Excel template for courses or faculty and send as attachment.
    Usage: /download-template/courses?format=csv or ?format=xlsx
    """
    fmt = (request.args.get('format') or 'csv').lower()
    if entity not in ('courses', 'faculty'):
        abort(404)

    if entity == 'courses':
        columns = ['code', 'name', 'credits', 'hours_per_week', 'course_type', 'branch', 'required_room_tags']
        filename_base = 'courses_template'
    else:
        columns = ['name', 'username', 'email', 'expertise', 'password', 'min_hours_per_week', 'max_hours_per_week', 'availability']
        filename_base = 'faculty_template'

    if fmt == 'csv':
        # Create CSV in-memory
        output = io.StringIO()
        import csv as _csv
        writer = _csv.writer(output)
        writer.writerow(columns)
        # Add a sample/example row (optional): keep blank
        # writer.writerow(['CS101', 'Intro to CS', '3', '3', 'theory', 'CSE', 'computer'])
        mem = io.BytesIO()
        mem.write(output.getvalue().encode('utf-8'))
        mem.seek(0)
        return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f"{filename_base}.csv")

    elif fmt in ('xls', 'xlsx'):
        # Use pandas to create an Excel file in-memory. Try available engines.
        df = pd.DataFrame(columns=columns)
        mem = io.BytesIO()
        engines_to_try = ['xlsxwriter', 'openpyxl']
        writer_used = None
        for eng in engines_to_try:
            try:
                with pd.ExcelWriter(mem, engine=eng) as writer:
                    df.to_excel(writer, index=False, sheet_name='Template')
                writer_used = eng
                break
            except ModuleNotFoundError:
                # try next engine
                mem.seek(0)
                mem.truncate(0)
                continue

        if not writer_used:
            # Fallback: return CSV if no excel engine is available
            output = io.StringIO()
            import csv as _csv
            writer = _csv.writer(output)
            writer.writerow(columns)
            mem2 = io.BytesIO()
            mem2.write(output.getvalue().encode('utf-8'))
            mem2.seek(0)
            return send_file(mem2, mimetype='text/csv', as_attachment=True, download_name=f"{filename_base}.csv")

        mem.seek(0)
        return send_file(mem, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name=f"{filename_base}.xlsx")

    else:
        # Unsupported format
        return jsonify({'success': False, 'error': 'Unsupported format'}), 400

@app.route('/')
@login_required
def index():
    user = User.query.get(session['user_id'])
    stats = {
        'courses': Course.query.count(),
        'faculty': Faculty.query.count(),
        'rooms': Room.query.count(),
        'students': Student.query.count(),
        'timetable_entries': TimetableEntry.query.count()
    }
    return render_template('index.html', stats=stats, user=user)

# Course Management
@app.route('/courses')
@login_required
def courses():
    user = User.query.get(session['user_id'])
    courses_list = Course.query.all()
    return render_template('courses.html', courses=courses_list, user=user)

@app.route('/courses/add', methods=['POST'])
@admin_required
def add_course():
    data = request.json
    course = Course(
        code=data['code'],
        name=data['name'],
        credits=int(data['credits']),
        course_type=data['type'],
        hours_per_week=int(data['hours_per_week']),
        branch=data.get('branch', '').strip() or None,
        required_room_tags=','.join(tag.strip() for tag in data.get('required_room_tags', '').split(',') if tag.strip())
    )
    db.session.add(course)
    db.session.commit()
    return jsonify({'success': True, 'id': course.id})

@app.route('/courses/<int:course_id>/delete', methods=['POST'])
@admin_required
def delete_course(course_id):
    course = Course.query.get_or_404(course_id)
    # Remove timetable entries referencing this course first to avoid
    # NOT NULL / FK constraint failures when the course is deleted.
    TimetableEntry.query.filter_by(course_id=course.id).delete(synchronize_session=False)
    db.session.delete(course)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/courses/import', methods=['POST'])
@admin_required
def import_courses():
    upload = request.files.get('file')
    if not upload:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    try:
        df = load_dataframe_from_upload(upload)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    df.columns = [col.strip().lower() for col in df.columns]
    required_columns = {'code', 'name', 'credits', 'hours_per_week'}
    if not required_columns.issubset(set(df.columns)):
        return jsonify({
            'success': False,
            'error': f'Missing columns. Required: {", ".join(sorted(required_columns))}'
        }), 400

    created, updated = 0, 0
    for _, row in df.iterrows():
        row_data = row.to_dict()
        code = str(row_data.get('code', '')).strip()
        if not code:
            continue
        course = Course.query.filter_by(code=code).first()
        course_type = str(row_data.get('course_type', row_data.get('type', 'theory'))).lower()
        course_type = 'practical' if 'prac' in course_type else 'theory'
        branch = str(row_data.get('branch', '')).strip() or None
        tags_raw = row_data.get('required_room_tags') or row_data.get('room_tags') or ''
        tags = ','.join(tag.strip() for tag in str(tags_raw).split(',') if tag.strip())

        payload = {
            'code': code,
            'name': str(row_data.get('name', code)).strip(),
            'credits': parse_int(row_data.get('credits'), 0),
            'course_type': course_type,
            'hours_per_week': parse_int(row_data.get('hours_per_week'), 1),
            'branch': branch,
            'required_room_tags': tags
        }

        if course:
            course.name = payload['name']
            course.credits = payload['credits']
            course.course_type = payload['course_type']
            course.hours_per_week = payload['hours_per_week']
            course.branch = payload['branch']
            course.required_room_tags = payload['required_room_tags']
            updated += 1
        else:
            course = Course(
                code=payload['code'],
                name=payload['name'],
                credits=payload['credits'],
                course_type=payload['course_type'],
                hours_per_week=payload['hours_per_week'],
                branch=payload['branch'],
                required_room_tags=payload['required_room_tags']
            )
            db.session.add(course)
            created += 1

    db.session.commit()
    return jsonify({'success': True, 'created': created, 'updated': updated})

# Faculty Management
@app.route('/faculty')
@login_required
def faculty():
    user = User.query.get(session['user_id'])
    faculty_list = Faculty.query.all()
    courses_list = Course.query.all()
    return render_template('faculty.html', faculty=faculty_list, courses=courses_list, user=user)

@app.route('/faculty/add', methods=['POST'])
@admin_required
def add_faculty():
    data = request.json
    try:
        faculty, generated_password = create_faculty_profile(data)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    db.session.commit()
    response = {'success': True, 'id': faculty.id}
    if generated_password:
        response['generated_password'] = generated_password
    return jsonify(response)

@app.route('/faculty/<int:faculty_id>/delete', methods=['POST'])
@admin_required
def delete_faculty(faculty_id):
    faculty = Faculty.query.get_or_404(faculty_id)
    linked_user = User.query.get(faculty.user_id) if faculty.user_id else None
    # Remove timetable entries referencing this faculty to avoid FK issues
    TimetableEntry.query.filter_by(faculty_id=faculty.id).delete(synchronize_session=False)
    db.session.delete(faculty)
    if linked_user and linked_user.role == 'teacher':
        db.session.delete(linked_user)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/faculty/availability', methods=['POST'])
@login_required
def update_own_availability():
    user = User.query.get(session['user_id'])
    if user.role != 'teacher':
        abort(403)
    faculty = Faculty.query.filter_by(user_id=user.id).first()
    if not faculty:
        return jsonify({'success': False, 'error': 'Profile not linked to faculty record'}), 404
    data = request.json or {}
    availability_payload = data.get('availability', {})
    if isinstance(availability_payload, dict):
        availability_payload = json.dumps(availability_payload)
    faculty.availability = availability_payload
    db.session.commit()
    return jsonify({'success': True})

@app.route('/faculty/import', methods=['POST'])
@admin_required
def import_faculty():
    upload = request.files.get('file')
    if not upload:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    try:
        df = load_dataframe_from_upload(upload)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    df.columns = [col.strip().lower() for col in df.columns]
    required = {'name', 'username'}
    if not required.issubset(set(df.columns)):
        return jsonify({'success': False, 'error': f'Missing columns: {", ".join(sorted(required))}'}), 400

    created = 0
    updated = 0
    for _, row in df.iterrows():
        row_data = row.to_dict()
        name = str(row_data.get('name', '')).strip()
        if not name:
            continue
        username = str(row_data.get('username', '')).strip()
        email = str(row_data.get('email', '')).strip()
        expertise = normalize_comma_list(row_data.get('expertise', ''))
        min_hours = parse_int(row_data.get('min_hours_per_week'), 4)
        max_hours = parse_int(row_data.get('max_hours_per_week'), 16)

        payload = {
            'name': name,
            'email': email,
            'expertise': expertise,
            'username': username,
            'password': str(row_data.get('password', '')).strip(),
            'min_hours_per_week': min_hours,
            'max_hours_per_week': max_hours,
            'availability': row_data.get('availability', '{}')
        }

        faculty = Faculty.query.filter_by(username=username).first()
        if faculty:
            faculty.name = name
            faculty.email = email
            faculty.expertise = ','.join(expertise)
            faculty.min_hours_per_week = min_hours
            faculty.max_hours_per_week = max_hours
            updated += 1
            continue

        create_faculty_profile(payload)
        created += 1

    db.session.commit()
    return jsonify({'success': True, 'created': created, 'updated': updated})

# Room Management
@app.route('/rooms')
@login_required
def rooms():
    user = User.query.get(session['user_id'])
    rooms_list = Room.query.all()
    return render_template('rooms.html', rooms=rooms_list, user=user)

@app.route('/rooms/add', methods=['POST'])
@admin_required
def add_room():
    data = request.json or {}

    # Validate name
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Room name is required.'}), 400

    # Prevent duplicate room names with a friendly error
    existing = Room.query.filter_by(name=name).first()
    if existing:
        return jsonify({'success': False, 'error': f'A room named "{name}" already exists.'}), 400

    # Parse capacity safely
    try:
        capacity = int(data.get('capacity')) if data.get('capacity') not in (None, '') else 0
    except (TypeError, ValueError):
        capacity = 0

    room = Room(
        name=name,
        capacity=capacity,
        room_type=data.get('type', ''),
        equipment=data.get('equipment', ''),
        tags=','.join(tag.strip() for tag in data.get('tags', '').split(',') if tag.strip())
    )
    db.session.add(room)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'A room named "{name}" already exists.'}), 400

    return jsonify({'success': True, 'id': room.id})

@app.route('/rooms/<int:room_id>/delete', methods=['POST'])
@admin_required
def delete_room(room_id):
    room = Room.query.get_or_404(room_id)
    db.session.delete(room)
    db.session.commit()
    return jsonify({'success': True})

# Student Management
@app.route('/students')
@login_required
def students():
    user = User.query.get(session['user_id'])
    students_list = Student.query.all()
    courses_list = Course.query.all()
    return render_template('students.html', students=students_list, courses=courses_list, user=user)

@app.route('/students/add', methods=['POST'])
@admin_required
def add_student():
    data = request.json
    student = Student(
        name=data['name'],
        student_id=data['student_id'],
        enrolled_courses=','.join(data.get('courses', []))
    )
    db.session.add(student)
    db.session.commit()
    return jsonify({'success': True, 'id': student.id})

@app.route('/students/<int:student_id>/delete', methods=['POST'])
@admin_required
def delete_student(student_id):
    student = Student.query.get_or_404(student_id)
    db.session.delete(student)
    db.session.commit()
    return jsonify({'success': True})

# Student Group Management
@app.route('/student-groups')
@admin_required
def student_groups():
    user = User.query.get(session['user_id'])
    raw_groups = StudentGroup.query.all()
    groups = []
    for g in raw_groups:
        batches = []
        if g.batches:
            try:
                parsed = json.loads(g.batches)
                if isinstance(parsed, list):
                    batches = parsed
            except Exception:
                batches = []
        groups.append({
            'id': g.id,
            'name': g.name,
            'description': g.description,
            'total_students': g.total_students,
            'batches': batches
        })
    return render_template('student_groups.html', groups=groups, user=user)

@app.route('/student-groups/add', methods=['POST'])
@admin_required
def add_student_group():
    data = request.json
    # Validate name
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Class name is required.'}), 400

    # Prevent duplicate names (return friendly error instead of raising DB exception)
    existing = StudentGroup.query.filter_by(name=name).first()
    if existing:
        return jsonify({'success': False, 'error': f'A class named "{name}" already exists.'}), 400
    batches = data.get('batches')
    # Ensure batches is stored as JSON string if provided as list/dict
    if isinstance(batches, (list, dict)):
        batches_json = json.dumps(batches)
    else:
        batches_json = batches or None

    total_students = None
    try:
        total_students = int(data.get('total_students')) if data.get('total_students') not in (None, '') else None
    except (TypeError, ValueError):
        total_students = None

    group = StudentGroup(
        name=name,
        description=data.get('description', ''),
        total_students=total_students,
        batches=batches_json
    )
    db.session.add(group)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'A class named "{name}" already exists.'}), 400
    return jsonify({'success': True, 'id': group.id})

@app.route('/student-groups/<int:group_id>/delete', methods=['POST'])
@admin_required
def delete_student_group(group_id):
    group = StudentGroup.query.get_or_404(group_id)
    db.session.delete(group)
    db.session.commit()
    return jsonify({'success': True})

# Timetable Generation
@app.route('/timetable')
@login_required
def timetable():
    user = User.query.get(session['user_id'])
    entries_query = TimetableEntry.query
    faculty_profile = None
    if user.role == 'teacher':
        faculty_profile = Faculty.query.filter_by(user_id=user.id).first()
        if faculty_profile:
            entries_query = entries_query.filter_by(faculty_id=faculty_profile.id)
        else:
            entries_query = entries_query.filter_by(faculty_id=-1)
    slots = TimeSlot.query.all()
    slots_dict = {s.id: s for s in slots}
    valid_slot_ids = set(slots_dict.keys())

    # Filter entries to only include those with valid time_slot_id
    entries = [e for e in entries_query.all() if e.time_slot_id in valid_slot_ids]

    courses_dict = {c.id: c for c in Course.query.all()}
    faculty_dict = {f.id: f for f in Faculty.query.all()}
    rooms_dict = {r.id: r for r in Room.query.all()}
    
    # Get break configurations
    breaks = BreakConfig.query.order_by(BreakConfig.after_period).all()
    break_map = {br.after_period: br for br in breaks}
    
    # Organize by day and period (one lecture per period per class is enforced by unique constraint)
    timetable_data = {}
    for entry in entries:
        slot = slots_dict[entry.time_slot_id]
        key = (slot.day, slot.period)
        if key not in timetable_data:
            timetable_data[key] = []
        timetable_data[key].append({
            'course': courses_dict[entry.course_id],
            'faculty': faculty_dict[entry.faculty_id],
            'room': rooms_dict[entry.room_id],
            'slot': slot,
            'student_group': entry.student_group
        })
    
    # Get days from period config or default
    period_config = PeriodConfig.query.first()
    if period_config:
        days = [d.strip() for d in period_config.days_of_week.split(',')]
    else:
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    
    periods = sorted(set(s.period for s in TimeSlot.query.all()))
    
    teacher_availability = {}
    if faculty_profile and faculty_profile.availability:
        try:
            teacher_availability = json.loads(faculty_profile.availability)
        except json.JSONDecodeError:
            teacher_availability = {}

    # Provide data needed for manual assignments UI (serialize to plain dicts)
    raw_student_groups = StudentGroup.query.all()
    student_groups_list = []
    for g in raw_student_groups:
        batches = []
        if g.batches:
            try:
                parsed = json.loads(g.batches)
                if isinstance(parsed, list):
                    batches = parsed
            except Exception:
                batches = []
        student_groups_list.append({
            'id': g.id,
            'name': g.name,
            'description': g.description,
            'total_students': g.total_students,
            'batches': batches
        })

    courses_list = []
    for c in Course.query.all():
        courses_list.append({
            'id': c.id,
            'code': c.code,
            'name': c.name,
            'credits': c.credits,
            'hours_per_week': c.hours_per_week,
            'course_type': c.course_type
        })

    faculty_list = []
    for f in Faculty.query.all():
        faculty_list.append({
            'id': f.id,
            'name': f.name,
            'email': f.email,
            'expertise': f.expertise
        })

    rooms_list = []
    for r in Room.query.all():
        rooms_list.append({
            'id': r.id,
            'name': r.name,
            'capacity': r.capacity,
            'room_type': r.room_type,
            'tags': r.tags
        })

    return render_template('timetable.html', 
                         timetable_data=timetable_data,
                         days=days,
                         periods=periods,
                         break_map=break_map,
                         user=user,
                         teacher_availability=teacher_availability,
                         student_groups=student_groups_list,
                         courses=courses_list,
                         faculty=faculty_list,
                         rooms=rooms_list)


@app.route('/timetable/entries')
@login_required
def timetable_entries():
    # Return entries for a given day to prefill manual assignment UI
    day = request.args.get('day')
    if not day:
        return jsonify({'entries': []})

    slots = TimeSlot.query.filter_by(day=day).all()
    slot_map = {s.id: s for s in slots}
    entries = TimetableEntry.query.filter(TimetableEntry.time_slot_id.in_(list(slot_map.keys()))).all()
    result = []
    for e in entries:
        s = slot_map.get(e.time_slot_id)
        if not s:
            continue
        result.append({
            'period': s.period,
            'student_group': e.student_group,
            'course_id': e.course_id,
            'faculty_id': e.faculty_id,
            'room_id': e.room_id
        })
    return jsonify({'entries': result})

@app.route('/timetable/generate', methods=['POST'])
@admin_required
def generate_timetable():
    # Clear existing timetable
    TimetableEntry.query.delete()
    db.session.commit()
    
    # Generate new timetable
    generator = TimetableGenerator(db)
    result = generator.generate()
    
    if result['success']:
        return jsonify({
            'success': True,
            'message': f'Timetable generated successfully! {result["entries_created"]} entries created.',
            'warnings': result.get('warnings', [])
        })
    else:
        return jsonify({
            'success': False,
            'message': result.get('error', 'Failed to generate timetable'),
            'warnings': result.get('warnings', [])
        })


@app.route('/timetable/manual-save', methods=['POST'])
@admin_required
def manual_save_timetable():
    """Save manual assignments posted from the admin UI.
    Expected JSON payload:
    { "day": "Monday", "assignments": [ {"period":1, "group":"CSE-A", "course_id":1, "faculty_id":2, "room_id":3}, ... ] }
    """
    payload = request.get_json() or {}
    day = payload.get('day')
    assignments = payload.get('assignments', [])

    if not day:
        return jsonify({'success': False, 'error': 'Day is required.'}), 400

    errors = []
    processed = 0

    for a in assignments:
        try:
            period = int(a.get('period'))
        except Exception:
            continue

        group_name = a.get('group')
        if not group_name:
            continue

        # Find timeslot
        slot = TimeSlot.query.filter_by(day=day, period=period).first()
        if not slot:
            errors.append(f'No timeslot for {day} P{period}')
            continue

        course_id = a.get('course_id')
        faculty_id = a.get('faculty_id')
        room_id = a.get('room_id')

        # Basic conflict checks: faculty or room already assigned at this timeslot to another group
        if faculty_id:
            conflict = TimetableEntry.query.filter(TimetableEntry.time_slot_id == slot.id, TimetableEntry.faculty_id == faculty_id, TimetableEntry.student_group != group_name).first()
            if conflict:
                errors.append(f'Faculty id {faculty_id} is already assigned at {day} P{period} to {conflict.student_group}')
                continue

        if room_id:
            conflict = TimetableEntry.query.filter(TimetableEntry.time_slot_id == slot.id, TimetableEntry.room_id == room_id, TimetableEntry.student_group != group_name).first()
            if conflict:
                errors.append(f'Room id {room_id} is already used at {day} P{period} by {conflict.student_group}')
                continue

        # Upsert TimetableEntry for this slot + group
        entry = TimetableEntry.query.filter_by(time_slot_id=slot.id, student_group=group_name).first()
        if course_id in (None, '', 0):
            # Delete existing entry if any
            if entry:
                db.session.delete(entry)
            processed += 1
            continue

        if not entry:
            entry = TimetableEntry(time_slot_id=slot.id, student_group=group_name)
            db.session.add(entry)

        entry.course_id = int(course_id) if course_id not in (None, '') else None
        entry.faculty_id = int(faculty_id) if faculty_id not in (None, '') else None
        entry.room_id = int(room_id) if room_id not in (None, '') else None
        processed += 1

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Database integrity error: ' + str(e)}), 500

    result = {'success': True, 'processed': processed}
    if errors:
        result['warnings'] = errors
    return jsonify(result)

@app.route('/timetable/clear', methods=['POST'])
@admin_required
def clear_timetable():
    TimetableEntry.query.delete()
    db.session.commit()
    return jsonify({'success': True})

# Export
# Settings Management
@app.route('/settings')
@admin_required
def settings():
    user = User.query.get(session['user_id'])
    period_config = PeriodConfig.query.first()
    breaks = BreakConfig.query.order_by(BreakConfig.after_period).all()
    days_list = [d.strip() for d in period_config.days_of_week.split(',')] if period_config else []
    return render_template('settings.html', period_config=period_config, breaks=breaks, days_list=days_list, user=user)

@app.route('/settings/period', methods=['POST'])
@admin_required
def update_period_config():
    data = request.json
    period_config = PeriodConfig.query.first()
    
    if not period_config:
        period_config = PeriodConfig()
        db.session.add(period_config)
    
    period_config.periods_per_day = int(data['periods_per_day'])
    period_config.period_duration_minutes = int(data['period_duration_minutes'])
    period_config.day_start_time = data['day_start_time']
    period_config.days_of_week = ','.join(data.get('days_of_week', []))
    
    db.session.commit()
    
    # Regenerate time slots
    generate_time_slots()
    
    return jsonify({'success': True, 'message': 'Period configuration updated and time slots regenerated.'})

@app.route('/settings/break/add', methods=['POST'])
@admin_required
def add_break():
    data = request.json
    break_config = BreakConfig(
        break_name=data['break_name'],
        after_period=int(data['after_period']),
        duration_minutes=int(data['duration_minutes']),
        order=int(data.get('order', 1))
    )
    db.session.add(break_config)
    db.session.commit()
    
    # Regenerate time slots
    generate_time_slots()
    
    return jsonify({'success': True, 'id': break_config.id})

@app.route('/settings/break/<int:break_id>/update', methods=['POST'])
@admin_required
def update_break(break_id):
    data = request.json
    break_config = BreakConfig.query.get_or_404(break_id)
    
    break_config.break_name = data['break_name']
    break_config.after_period = int(data['after_period'])
    break_config.duration_minutes = int(data['duration_minutes'])
    break_config.order = int(data.get('order', break_config.order))
    
    db.session.commit()
    
    # Regenerate time slots
    generate_time_slots()
    
    return jsonify({'success': True})

@app.route('/settings/break/<int:break_id>/delete', methods=['POST'])
@admin_required
def delete_break(break_id):
    break_config = BreakConfig.query.get_or_404(break_id)
    db.session.delete(break_config)
    db.session.commit()
    
    # Regenerate time slots
    generate_time_slots()
    
    return jsonify({'success': True})

@app.route('/timetable/export')
@login_required
def export_timetable():
    slots = TimeSlot.query.all()
    slots_dict = {s.id: s for s in slots}
    valid_slot_ids = set(slots_dict.keys())

    # Filter entries to only include those with valid time_slot_id
    entries = [e for e in TimetableEntry.query.all() if e.time_slot_id in valid_slot_ids]

    courses_dict = {c.id: c for c in Course.query.all()}
    faculty_dict = {f.id: f for f in Faculty.query.all()}
    rooms_dict = {r.id: r for r in Room.query.all()}
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Day', 'Period', 'Start Time', 'End Time', 'Course Code', 'Course Name', 'Faculty', 'Room'])
    
    for entry in entries:
        slot = slots_dict[entry.time_slot_id]
        course = courses_dict[entry.course_id]
        faculty = faculty_dict[entry.faculty_id]
        room = rooms_dict[entry.room_id]
        writer.writerow([
            slot.day,
            slot.period,
            slot.start_time,
            slot.end_time,
            course.code,
            course.name,
            faculty.name,
            room.name
        ])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'timetable_{datetime.now().strftime("%Y%m%d")}.csv'
    )

if __name__ == '__main__':
    # Allow overriding port via environment variable `PORT` (default: 5001)
    port = int(os.environ.get('PORT', '5001'))
    app.run(debug=True, port=port)


