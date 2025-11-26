# PlanSphere.AI - MVP

A web-based intelligent timetable generator for NEP 2020 compliant programs (FYUP, B.Ed., M.Ed., ITEP) with role-based access control and flexible period/break configuration.

## Features

- **3-Tier Login System**: Admin, Teacher, and Student roles with different access levels
- **Admin-Configurable Periods & Breaks**: 
  - Set number of periods per day
  - Configure period duration (in minutes)
  - Set day start time
  - Select days of the week
  - Add breaks with custom names and durations
  - Specify which period each break comes after
- **Automatic Time Slot Generation**: Time slots are automatically calculated based on period and break settings
- **One Lecture Per Period Per Class**: Database-enforced constraint ensures only one lecture per period per student group
- **Student Group Management**: Organize students into classes/groups (e.g., "FYUP-A", "B.Ed-1")
- **Course Management**: Add and manage courses with credits, type (theory/practical), and hours per week (Admin only)
- **Faculty Management**: Add faculty with expertise in specific courses (Admin only)
- **Room Management**: Add classrooms and labs with capacity information (Admin only)
- **Student Management**: Add students and their enrolled courses (Admin only)
- **Automatic Timetable Generation**: Conflict-free scheduling algorithm (Admin only)
- **Timetable Viewing**: All users can view the generated timetable with breaks displayed
- **Export**: Export timetable to CSV format (All users)

## Installation

1. Install Python 3.7 or higher
2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Running the Application

1. Start the Flask server:
```bash
python app.py
```

2. Open your browser and navigate to:
```
http://localhost:5000
```

## Usage

### Default Login Credentials
- **Admin**: username: `admin`, password: `admin123`
- You can register new users with different roles (admin, teacher, student)

### For Administrators

1. **Login** as admin (default: username: `admin`, password: `admin123`)

2. **Configure Period & Break Settings** (Settings page):
   - Set number of periods per day (e.g., 8)
   - Set period duration in minutes (e.g., 60)
   - Set day start time (e.g., 09:00)
   - Select days of the week (Monday-Saturday)
   - Add breaks:
     - Break name (e.g., "Lunch Break", "Short Break")
     - After which period (e.g., after Period 4)
     - Duration in minutes (e.g., 60 for lunch, 15 for short break)
   - Time slots are automatically regenerated when settings change

3. **Setup Data**:
   - Add student groups (e.g., "FYUP-A", "B.Ed-1", "M.Ed-1")
   - Add courses (with codes, credits, type, hours per week)
   - Add faculty members (with expertise in course codes)
   - Add rooms (classrooms and labs with capacity)
   - Add students (with enrolled course codes)

4. **Generate Timetable**:
   - Go to the Timetable page
   - Click "Generate Timetable"
   - The system will create a conflict-free schedule
   - Ensures one lecture per period per class
   - Breaks are automatically displayed in the timetable

5. **Export**:
   - Click "Export CSV" to download the timetable

### For Teachers and Students

1. **Login** with your credentials
2. **View Timetable**: Navigate to the Timetable page to see your schedule
3. **Export**: Download the timetable as CSV if needed

**Note**: Only administrators can create, edit, or delete timetables and manage courses, faculty, rooms, and students.

## Data Structure

- **Users**: Username, Email, Password (hashed), Role (admin/teacher/student), Name
- **Courses**: Code, Name, Credits, Type (theory/practical), Hours per week
- **Faculty**: Name, Email, Expertise (course codes), Availability
- **Rooms**: Name, Capacity, Type (classroom/lab), Equipment
- **Students**: Student ID, Name, Enrolled Courses, Student Group
- **Student Groups**: Name, Description (e.g., "FYUP-A", "B.Ed-1")
- **Period Config**: Periods per day, Period duration (minutes), Day start time, Days of week
- **Break Config**: Break name, After period, Duration (minutes), Order
- **Time Slots**: Day, Period, Start time, End time (auto-generated from PeriodConfig and BreakConfig)
- **Timetable Entries**: Course, Faculty, Room, Time Slot, Student Group (with unique constraint ensuring one lecture per period per class)

## Algorithm

The timetable generator uses a simple constraint satisfaction approach:

1. **Time Slot Generation** (based on admin settings):
   - Calculate periods based on PeriodConfig (number, duration, start time)
   - Insert breaks based on BreakConfig (after which period, duration)
   - Generate time slots with calculated start/end times

2. **Course Assignment**:
   - Sort courses by priority (credits, type)
   - For each course:
     - Find suitable faculty with matching expertise
     - Find suitable rooms (classroom for theory, lab for practical)
     - For each student group:
       - Find available time slot where:
         - Faculty is not already assigned
         - Room is not already assigned
         - **Period is not already assigned for that student group** (one lecture per period per class)
       - Create timetable entry with student group

3. **Conflict Prevention**:
   - Faculty schedule tracking (prevents double-booking)
   - Room schedule tracking (prevents room conflicts)
   - Period-class unique constraint (database-enforced: one lecture per period per class)

4. **Validation**:
   - Check if all required hours are assigned
   - Generate warnings for partially assigned courses

## Key Features

- ✅ **One Lecture Per Period Per Class**: Database-enforced unique constraint
- ✅ **Flexible Period Configuration**: Admin can customize periods per day, duration, start time
- ✅ **Custom Break Configuration**: Admin can add breaks with different durations after specific periods
- ✅ **Automatic Time Slot Calculation**: Time slots generated automatically from settings
- ✅ **Role-Based Access**: Admin (full access), Teacher/Student (view-only)
- ✅ **Student Group Management**: Organize classes for proper scheduling
- ✅ **Break Display**: Breaks shown in timetable view

## Limitations (MVP)

- Basic conflict detection (faculty, room, and period-class only)
- Simple assignment algorithm (no advanced optimization)
- No student preference handling
- No faculty availability calendar (uses all time slots)
- No multi-semester planning
- CSV export only (no PDF)
- No manual timetable editing interface

## Future Enhancements

- Advanced ML optimization for better scheduling
- Student preference handling
- Faculty availability calendar
- PDF export with professional formatting
- Multi-semester planning
- Manual timetable editing interface
- Integration APIs with existing Academic Management Systems
- Advanced reporting and analytics
- Room utilization reports
- Conflict resolution suggestions
- Batch student import/export
- Email notifications for timetable changes

## License

This is an MVP prototype for educational purposes.

