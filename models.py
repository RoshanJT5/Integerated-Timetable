from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # admin, teacher, student
    name = db.Column(db.String(100), nullable=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def __repr__(self):
        return f'<User {self.username} ({self.role})>'

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    credits = db.Column(db.Integer, nullable=False)
    course_type = db.Column(db.String(20), nullable=False)  # theory or practical
    hours_per_week = db.Column(db.Integer, nullable=False)
    branch = db.Column(db.String(100))  # optional branch identifier
    required_room_tags = db.Column(db.String(255))  # comma separated tags (e.g., computer-lab,physics-lab)
    
    def __repr__(self):
        return f'<Course {self.code}>'

class Faculty(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100))
    expertise = db.Column(db.Text)  # comma-separated course codes
    availability = db.Column(db.Text)  # JSON string for availability
    username = db.Column(db.String(80), unique=True)  # matches teacher login username
    min_hours_per_week = db.Column(db.Integer, nullable=False, default=4)
    max_hours_per_week = db.Column(db.Integer, nullable=False, default=16)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    
    user = db.relationship('User', backref='faculty_profile', uselist=False)
    
    def __repr__(self):
        return f'<Faculty {self.name}>'

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    room_type = db.Column(db.String(20), nullable=False)  # classroom or lab
    equipment = db.Column(db.Text)
    tags = db.Column(db.String(255))  # comma-separated specializations (e.g., computer, electronics)
    
    def __repr__(self):
        return f'<Room {self.name}>'

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    student_id = db.Column(db.String(50), unique=True, nullable=False)
    enrolled_courses = db.Column(db.Text)  # comma-separated course codes
    student_group = db.Column(db.String(50))  # class/group name (e.g., "FYUP-A", "B.Ed-1")
    
    def __repr__(self):
        return f'<Student {self.student_id}>'

class StudentGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)  # e.g., "FYUP-A", "B.Ed-1"
    description = db.Column(db.String(200))
    total_students = db.Column(db.Integer)
    batches = db.Column(db.Text)  # JSON string: list of {"batch_name":..., "students":...}
    
    def __repr__(self):
        return f'<StudentGroup {self.name}>'

class PeriodConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    periods_per_day = db.Column(db.Integer, nullable=False, default=8)
    period_duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    day_start_time = db.Column(db.String(10), nullable=False, default='09:00')
    days_of_week = db.Column(db.Text, nullable=False, default='Monday,Tuesday,Wednesday,Thursday,Friday')  # comma-separated
    
    def __repr__(self):
        return f'<PeriodConfig {self.periods_per_day} periods, {self.period_duration_minutes} min>'

class BreakConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    break_name = db.Column(db.String(50), nullable=False)  # e.g., "Lunch Break", "Short Break"
    after_period = db.Column(db.Integer, nullable=False)  # which period this break comes after
    duration_minutes = db.Column(db.Integer, nullable=False)
    order = db.Column(db.Integer, nullable=False)  # order of breaks in the day
    
    def __repr__(self):
        return f'<BreakConfig {self.break_name} after P{self.after_period}, {self.duration_minutes} min>'

class TimeSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(20), nullable=False)
    period = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.String(10), nullable=False)
    end_time = db.Column(db.String(10), nullable=False)
    
    def __repr__(self):
        return f'<TimeSlot {self.day} P{self.period}>'

class TimetableEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey('faculty.id'), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    time_slot_id = db.Column(db.Integer, db.ForeignKey('time_slot.id'), nullable=False)
    student_group = db.Column(db.String(50), nullable=False)  # class/group name - required now
    
    course = db.relationship('Course', backref='timetable_entries')
    faculty = db.relationship('Faculty', backref='timetable_entries')
    room = db.relationship('Room', backref='timetable_entries')
    time_slot = db.relationship('TimeSlot', backref='timetable_entries')
    
    # Ensure one lecture per period per class
    __table_args__ = (db.UniqueConstraint('time_slot_id', 'student_group', name='unique_period_class'),)
    
    def __repr__(self):
        return f'<TimetableEntry {self.course_id}-{self.faculty_id}-{self.room_id}-{self.student_group}>'


