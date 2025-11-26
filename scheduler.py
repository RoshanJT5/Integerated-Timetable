import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

import pulp

from models import (
    Course,
    Faculty,
    Room,
    TimeSlot,
    TimetableEntry,
    StudentGroup,
)


@dataclass(frozen=True)
class Session:
    """Represents an atomic lecture hour that must be scheduled."""

    id: int
    course_id: int
    course_code: str
    course_type: str
    student_group: str
    is_lab: bool


class TimetableGenerator:
    """
    Enhanced Hybrid scheduler with 9 advanced constraints:
    
    1. Automatic faculty workload management (min/max bounds)
    2. Priority lab allocation for courses requiring labs
    3. Faculty availability-based prioritization
    4. Per-day faculty timetable generation based on expertise & workload
    5. Prevention of consecutive lectures of same subject
    6. Preferred timeslot assignment for senior faculty
    7. Automatic daily lecture & lab tracking per faculty
    8. Multi-course faculty scheduling with cross-course coordination
    9. Overwork detection & alerts (40+ hours/week warning)
    """

    def __init__(self, db_session, random_seed: int | None = None, config: dict = None):
        self.db = db_session
        self.random = random.Random(random_seed or random.randint(1, 999_999))
        
        # Enhanced configuration options
        self.config = config or {}
        self.overwork_threshold = self.config.get('overwork_threshold', 40)  # hours/week
        self.senior_faculty_preference = self.config.get('senior_faculty_preference', True)
        self.consecutive_penalty_weight = self.config.get('consecutive_penalty', 20)
        self.lab_priority_weight = self.config.get('lab_priority', 50)

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def generate(self):
        context = self._load_context()
        if not context["courses"]:
            return {"success": False, "error": "No courses found. Please add courses first."}
        if not context["faculty"]:
            return {"success": False, "error": "No faculty found. Please add faculty first."}
        if not context["rooms"]:
            return {"success": False, "error": "No rooms found. Please add rooms first."}
        if not context["time_slots"]:
            return {"success": False, "error": "No time slots found. Please configure time slots."}

        # Constraint 1: Validate workload bounds
        bound_report = self._run_bound_analyzer(context)
        if not bound_report["feasible"]:
            return {
                "success": False,
                "error": "Bound analysis failed – please review constraints.",
                "warnings": bound_report["warnings"],
            }

        # Constraint 2 & 3: ILP with lab priority and availability focus
        ilp_result = self._solve_with_ilp(context)
        warnings = bound_report["warnings"] + ilp_result.get("warnings", [])
        if not ilp_result["success"]:
            return {"success": False, "error": ilp_result["error"], "warnings": warnings}

        # Constraints 4-8: GA refinement with enhanced constraints
        ga_result = self._refine_with_genetic_algorithm(
            context,
            ilp_result["assignments"],
            ilp_result.get("session_candidates", {}),
        )
        warnings.extend(ga_result.get("warnings", []))
        final_assignments = ga_result.get("assignments", ilp_result["assignments"])

        # Constraint 9: Overwork detection
        overwork_warnings = self._detect_overwork(final_assignments, context)
        warnings.extend(overwork_warnings)

        # Constraint 7: Generate per-faculty daily schedules
        faculty_schedules = self._generate_faculty_schedules(final_assignments, context)
        
        entries_created = self._persist_assignments(final_assignments, context)
        
        return {
            "success": True,
            "entries_created": entries_created,
            "warnings": warnings,
            "faculty_schedules": faculty_schedules,
            "overwork_alerts": [w for w in warnings if "overwork" in w.lower()]
        }

    # --------------------------------------------------------------------- #
    # Context Preparation
    # --------------------------------------------------------------------- #
    def _load_context(self):
        courses = Course.query.all()
        faculty = Faculty.query.all()
        rooms = Room.query.all()
        time_slots = TimeSlot.query.order_by(TimeSlot.day, TimeSlot.period).all()
        student_groups = StudentGroup.query.all()

        if not student_groups:
            default_group = StudentGroup(name="Default", description="Auto-generated group")
            self.db.session.add(default_group)
            self.db.session.commit()
            student_groups = [default_group]

        slot_by_id = {slot.id: slot for slot in time_slots}
        slots_by_day: Dict[str, List[TimeSlot]] = defaultdict(list)
        for slot in time_slots:
            slots_by_day[slot.day].append(slot)
        for day in slots_by_day:
            slots_by_day[day].sort(key=lambda s: s.period)

        faculty_availability = self._build_faculty_availability_map(faculty, slot_by_id)
        faculty_expertise = self._build_faculty_expertise_map(faculty)
        
        # Enhanced: Track faculty seniority (for constraint 6)
        faculty_seniority = self._estimate_faculty_seniority(faculty)

        sessions = self._build_sessions(courses, student_groups)
        room_capabilities = self._build_room_capabilities(rooms)

        return {
            "courses": courses,
            "course_by_id": {course.id: course for course in courses},
            "faculty": faculty,
            "faculty_by_id": {f.id: f for f in faculty},
            "rooms": rooms,
            "time_slots": time_slots,
            "slot_by_id": slot_by_id,
            "slots_by_day": slots_by_day,
            "student_groups": student_groups,
            "sessions": sessions,
            "faculty_availability": faculty_availability,
            "faculty_expertise": faculty_expertise,
            "faculty_seniority": faculty_seniority,
            "room_capabilities": room_capabilities,
        }

    def _estimate_faculty_seniority(self, faculty_list: List[Faculty]) -> Dict[int, float]:
        """
        Constraint 6: Estimate faculty seniority based on workload patterns.
        Higher max_hours_per_week typically indicates senior faculty.
        Returns score 0-1 where 1 is most senior.
        """
        seniority = {}
        if not faculty_list:
            return seniority
        
        # Use max_hours as proxy for seniority (senior faculty often have higher max)
        max_hours_values = [f.max_hours_per_week or 16 for f in faculty_list]
        max_val = max(max_hours_values) if max_hours_values else 16
        min_val = min(max_hours_values) if max_hours_values else 4
        
        for faculty in faculty_list:
            max_h = faculty.max_hours_per_week or 16
            if max_val == min_val:
                seniority[faculty.id] = 0.5
            else:
                # Normalize: higher max_hours = higher seniority
                seniority[faculty.id] = (max_h - min_val) / (max_val - min_val)
        
        return seniority

    def _build_faculty_availability_map(self, faculty_list: List[Faculty], slot_by_id: Dict[int, TimeSlot]):
        """Constraint 3: Build availability map with preference scoring"""
        availability = {}
        for faculty in faculty_list:
            if not faculty.availability:
                availability[faculty.id] = set(slot_by_id.keys())
                continue

            try:
                availability_json = json.loads(faculty.availability)
            except json.JSONDecodeError:
                availability[faculty.id] = set(slot_by_id.keys())
                continue

            allowed_slots: Set[int] = set()
            for day, periods in availability_json.items():
                if not isinstance(periods, (list, tuple)):
                    periods = periods.get("periods", [])
                normalized_periods = set()
                for period in periods:
                    if isinstance(period, dict) and "period" in period:
                        normalized_periods.add(int(period["period"]))
                    else:
                        try:
                            normalized_periods.add(int(period))
                        except (TypeError, ValueError):
                            continue

                for slot in slot_by_id.values():
                    if slot.day.lower() == day.lower() and slot.period in normalized_periods:
                        allowed_slots.add(slot.id)

            availability[faculty.id] = allowed_slots if allowed_slots else set(slot_by_id.keys())

        return availability

    def _build_faculty_expertise_map(self, faculty_list: List[Faculty]):
        """Constraint 4 & 8: Build expertise map for multi-course handling"""
        expertise_map: Dict[int, Set[str]] = {}
        for faculty in faculty_list:
            if faculty.expertise:
                items = [code.strip().lower() for code in faculty.expertise.split(",") if code.strip()]
                expertise_map[faculty.id] = set(items)
            else:
                expertise_map[faculty.id] = set()
        return expertise_map

    def _build_room_capabilities(self, rooms: List[Room]):
        """Constraint 2: Build room capabilities with lab tagging"""
        capability_map: Dict[int, Set[str]] = {}
        for room in rooms:
            tags = set()
            if room.tags:
                tags.update(tag.strip().lower() for tag in room.tags.split(",") if tag.strip())
            if room.room_type == "lab":
                tags.add("lab")
            capability_map[room.id] = tags
        return capability_map

    def _eligible_groups_for_course(self, course: Course, groups: List[StudentGroup]):
        if not course.branch:
            return groups
        branch = course.branch.lower()
        eligible = [
            group for group in groups
            if branch in group.name.lower() or (group.description and branch in group.description.lower())
        ]
        return eligible or groups

    def _build_sessions(self, courses: List[Course], groups: List[StudentGroup]):
        sessions = []
        session_id = 1
        for course in courses:
            eligible_groups = self._eligible_groups_for_course(course, groups)
            for group in eligible_groups:
                is_lab = course.course_type == "practical"
                for _ in range(course.hours_per_week):
                    sessions.append(
                        Session(
                            id=session_id,
                            course_id=course.id,
                            course_code=course.code.lower(),
                            course_type=course.course_type,
                            student_group=group.name,
                            is_lab=is_lab,
                        )
                    )
                    session_id += 1
        return sessions

    # --------------------------------------------------------------------- #
    # Bound Analyzer (Constraint 1)
    # --------------------------------------------------------------------- #
    def _run_bound_analyzer(self, context):
        """Enhanced bound analysis with workload validation"""
        warnings = []
        total_session_hours = len(context["sessions"])
        
        # Constraint 1: Validate faculty workload bounds
        total_faculty_capacity = sum(f.max_hours_per_week for f in context["faculty"])
        total_faculty_minimum = sum(f.min_hours_per_week for f in context["faculty"])
        
        if total_session_hours > total_faculty_capacity:
            warnings.append(
                f"⚠️ Workload Issue: Total sessions ({total_session_hours}h) exceed faculty capacity ({total_faculty_capacity}h)"
            )
            return {"feasible": False, "warnings": warnings}
        
        if total_session_hours < total_faculty_minimum:
            warnings.append(
                f"ℹ️ Under-utilization: Total sessions ({total_session_hours}h) below faculty minimum ({total_faculty_minimum}h)"
            )

        # Constraint 2: Validate lab availability
        lab_sessions = [s for s in context["sessions"] if s.is_lab]
        lab_rooms = [r for r in context["rooms"] if r.room_type == "lab"]
        lab_capacity = len(lab_rooms) * len(context["time_slots"])
        
        if len(lab_sessions) > lab_capacity:
            warnings.append(
                f"⚠️ Lab Shortage: {len(lab_sessions)} lab sessions need {len(lab_rooms)} labs × {len(context['time_slots'])} slots"
            )
            return {"feasible": False, "warnings": warnings}

        # Constraint 3: Validate faculty availability coverage
        for faculty in context["faculty"]:
            available_slots = len(context["faculty_availability"].get(faculty.id, set()))
            if available_slots < faculty.min_hours_per_week:
                warnings.append(
                    f"⚠️ {faculty.name} has only {available_slots} available slots but requires {faculty.min_hours_per_week} hours minimum"
                )

        # Enhanced check: ensure each faculty has enough possible session assignments
        # (considering expertise, eligible rooms and availability) to meet their minimum hours
        sessions = context.get("sessions", [])
        for faculty in context["faculty"]:
            possible_session_count = 0
            avail_slots = context["faculty_availability"].get(faculty.id, set())
            for session in sessions:
                course = context["course_by_id"].get(session.course_id)
                if not course:
                    continue
                # Check faculty expertise eligibility
                eligible_faculty = self._faculty_for_course(course, [faculty], context.get("faculty_expertise", {}))
                if not eligible_faculty:
                    continue

                # Check for at least one eligible room for this course
                eligible_rooms = self._rooms_for_course(course, context.get("rooms", []), context.get("room_capabilities", {}))
                if not eligible_rooms:
                    continue

                # Check if there exists at least one timeslot where faculty is available
                if any(slot.id in avail_slots for slot in context.get("time_slots", [])):
                    possible_session_count += 1

            if possible_session_count < (faculty.min_hours_per_week or 0):
                warnings.append(
                    f"⚠️ Feasibility Issue: {faculty.name} can teach at most {possible_session_count} sessions but requires {faculty.min_hours_per_week} minimum"
                )
                # Do not fail here: report as a warning so that soft-minima in the ILP
                # can be used to find a best-effort solution. Returning infeasible would
                # prevent the ILP from running even when we allow shortfalls.

        return {"feasible": True, "warnings": warnings}

    # --------------------------------------------------------------------- #
    # ILP Solver (Constraints 1-3)
    # --------------------------------------------------------------------- #
    def _solve_with_ilp(self, context):
        """Enhanced ILP with lab priority and availability focus"""
        warnings = []
        problem = pulp.LpProblem("Timetable", pulp.LpMinimize)
        
        # Build candidates for each session
        session_candidates = {}
        decision_vars = {}
        
        for session in context["sessions"]:
            course = context["course_by_id"][session.course_id]
            eligible_faculty = self._faculty_for_course(course, context["faculty"], context["faculty_expertise"])
            eligible_rooms = self._rooms_for_course(course, context["rooms"], context["room_capabilities"])
            
            if not eligible_faculty:
                warnings.append(f"⚠️ No faculty available for course {course.code}")
                continue
            if not eligible_rooms:
                warnings.append(f"⚠️ No suitable rooms for course {course.code}")
                continue
            
            candidates = []
            for faculty in eligible_faculty:
                # Constraint 3: Only consider available timeslots
                available_slots = context["faculty_availability"].get(faculty.id, set())
                
                for room in eligible_rooms:
                    for slot in context["time_slots"]:
                        # Skip if faculty not available
                        if slot.id not in available_slots:
                            continue
                        
                        var_name = f"s{session.id}_f{faculty.id}_r{room.id}_t{slot.id}"
                        var = pulp.LpVariable(var_name, cat="Binary")
                        decision_vars[var_name] = var
                        
                        # Constraint 2 & 6: Calculate priority score
                        priority_score = 0
                        if session.is_lab:
                            priority_score += self.lab_priority_weight
                        
                        # Constraint 6: Prefer early/preferred slots for senior faculty
                        if self.senior_faculty_preference:
                            seniority = context["faculty_seniority"].get(faculty.id, 0.5)
                            if seniority > 0.7 and slot.period <= 3:  # Morning slots for senior
                                priority_score -= 10
                        
                        candidates.append({
                            "var": var,
                            "faculty_id": faculty.id,
                            "room_id": room.id,
                            "slot_id": slot.id,
                            "group": session.student_group,
                            "course_id": course.id,
                            "course_code": session.course_code,
                            "is_lab": session.is_lab,
                            "priority": priority_score
                        })
            
            if not candidates:
                warnings.append(f"⚠️ No valid candidates for session {session.id} of {course.code}")
                continue
            
            session_candidates[session.id] = candidates
            
            # Constraint: Each session assigned exactly once
            # If `maximize_fill` config is set, allow session to be unassigned (<=1)
            if self.config.get('maximize_fill', False):
                problem += pulp.lpSum(c["var"] for c in candidates) <= 1, f"session_{session.id}_opt"
            else:
                problem += pulp.lpSum(c["var"] for c in candidates) == 1, f"session_{session.id}"
        
        # Constraint: No faculty/room/group conflicts per timeslot
        faculty_slot_usage = defaultdict(list)
        room_slot_usage = defaultdict(list)
        group_slot_usage = defaultdict(list)
        
        for candidates in session_candidates.values():
            for candidate in candidates:
                faculty_slot_usage[(candidate["faculty_id"], candidate["slot_id"])].append(candidate["var"])
                room_slot_usage[(candidate["room_id"], candidate["slot_id"])].append(candidate["var"])
                group_slot_usage[(candidate["group"], candidate["slot_id"])].append(candidate["var"])
        
        for key, vars_list in faculty_slot_usage.items():
            problem += pulp.lpSum(vars_list) <= 1, f"faculty_{key[0]}_slot_{key[1]}"
        for key, vars_list in room_slot_usage.items():
            problem += pulp.lpSum(vars_list) <= 1, f"room_{key[0]}_slot_{key[1]}"
        for key, vars_list in group_slot_usage.items():
            problem += pulp.lpSum(vars_list) <= 1, f"group_{key[0]}_slot_{key[1]}"
        
        # Constraint 1: Faculty workload bounds
        faculty_hours = defaultdict(list)
        for candidates in session_candidates.values():
            for candidate in candidates:
                faculty_hours[candidate["faculty_id"]].append(candidate["var"])
        
        for faculty in context["faculty"]:
            if faculty.id in faculty_hours:
                total = pulp.lpSum(faculty_hours[faculty.id])
                # Make minimum-hours a soft constraint using a non-negative slack variable
                slack_name = f"slack_faculty_{faculty.id}"
                slack_var = pulp.LpVariable(slack_name, lowBound=0, cat="Continuous")
                problem += total + slack_var >= faculty.min_hours_per_week, f"faculty_{faculty.id}_min_soft"
                # Keep maximum as a hard constraint
                problem += total <= faculty.max_hours_per_week, f"faculty_{faculty.id}_max"
                # Store slack var on the faculty object for objective construction
                faculty._min_slack_var = slack_var
        
        # Constraint 2: At least one lab per student group
        for group in context["student_groups"]:
            lab_vars = []
            for candidates in session_candidates.values():
                for candidate in candidates:
                    if candidate["is_lab"] and candidate["group"] == group.name:
                        lab_vars.append(candidate["var"])
            if lab_vars:
                problem += pulp.lpSum(lab_vars) >= 1, f"group_{group.name}_min_lab"
        
        # Objective: Penalize minimum-hours shortfall (slack) heavily, plus priority scores
        objective_terms = []
        slack_penalty = self.config.get('min_violation_penalty', 1000)
        for faculty in context["faculty"]:
            if hasattr(faculty, '_min_slack_var'):
                # Penalize any slack (hours shortfall) to prefer meeting minima when possible
                objective_terms.append(slack_penalty * faculty._min_slack_var)
        
        # Add priority scores to objective
        for candidates in session_candidates.values():
            for candidate in candidates:
                objective_terms.append(candidate["priority"] * candidate["var"])

        # If maximizing fill is enabled, add a negative reward for assigning any candidate
        # so that the minimization objective will try to assign as many sessions as possible
        if self.config.get('maximize_fill', False):
            assign_reward = -self.config.get('assign_reward', 50)
            for candidates in session_candidates.values():
                for candidate in candidates:
                    objective_terms.append(assign_reward * candidate["var"]) 
        
        problem += pulp.lpSum(objective_terms)
        
        # Solve
        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=60)
        status = problem.solve(solver)
        
        if status != pulp.LpStatusOptimal:
            return {
                "success": False,
                "error": f"ILP solver failed with status: {pulp.LpStatus[status]}",
                "warnings": warnings
            }
        
        # Extract assignments
        assignments = []
        for session_id, candidates in session_candidates.items():
            for candidate in candidates:
                if pulp.value(candidate["var"]) > 0.5:
                    assignments.append({
                        "session_id": session_id,
                        "faculty_id": candidate["faculty_id"],
                        "room_id": candidate["room_id"],
                        "slot_id": candidate["slot_id"],
                        "group": candidate["group"],
                        "course_id": candidate["course_id"],
                        "course_code": candidate["course_code"],
                        "is_lab": candidate["is_lab"],
                    })
        
        return {
            "success": True,
            "assignments": assignments,
            "warnings": warnings,
            "session_candidates": session_candidates,
        }

    def _faculty_for_course(self, course: Course, faculty_list: List[Faculty], expertise_map):
        """Constraint 4 & 8: Select faculty based on expertise"""
        course_code = course.code.lower()
        eligible = []
        for faculty in faculty_list:
            expertise = expertise_map.get(faculty.id, set())
            if not expertise or course_code in expertise:
                eligible.append(faculty)
        return eligible

    def _rooms_for_course(self, course: Course, rooms: List[Room], room_tags):
        """Constraint 2: Select rooms with required lab capabilities"""
        is_lab = course.course_type == "practical"
        required_tags = set()
        if course.required_room_tags:
            required_tags.update(tag.strip().lower() for tag in course.required_room_tags.split(",") if tag.strip())
        if is_lab:
            required_tags.add("lab")

        eligible = []
        for room in rooms:
            if is_lab and room.room_type != "lab":
                continue
            if not is_lab and room.room_type != "classroom":
                continue
            tags = room_tags.get(room.id, set())
            if required_tags and not required_tags.issubset(tags):
                continue
            eligible.append(room)
        return eligible

    # --------------------------------------------------------------------- #
    # Genetic Optimizer (Constraints 5-8)
    # --------------------------------------------------------------------- #
    def _refine_with_genetic_algorithm(self, context, base_assignments, session_candidates):
        """Enhanced GA with consecutive lecture prevention and multi-course handling"""
        if not base_assignments:
            return {"warnings": ["GA skipped – no ILP assignments to refine."]}

        population_size = 10
        generations = 15
        candidates_by_session = self._index_assignment_candidates(session_candidates)

        def clone(assignments):
            return [assignment.copy() for assignment in assignments]

        population = [clone(base_assignments)]
        while len(population) < population_size:
            mutated = self._mutate_assignment(population[0], candidates_by_session)
            population.append(mutated)

        for _ in range(generations):
            scored = sorted(
                [(self._fitness(individual, context), individual) for individual in population],
                key=lambda item: item[0],
            )
            population = [ind for _, ind in scored[:population_size // 2]]
            while len(population) < population_size:
                parents = self.random.sample(population[: max(1, len(population) // 2)], k=min(2, len(population)))
                child = self._crossover_assignments(*parents)
                child = self._mutate_assignment(child, candidates_by_session)
                population.append(child)

        best = min(population, key=lambda individual: self._fitness(individual, context))
        return {"assignments": best, "warnings": []}

    def _index_assignment_candidates(self, session_candidates):
        index = defaultdict(list)
        for session_id, candidates in session_candidates.items():
            for candidate in candidates:
                index[session_id].append({
                    "session_id": session_id,
                    "faculty_id": candidate["faculty_id"],
                    "room_id": candidate["room_id"],
                    "slot_id": candidate["slot_id"],
                    "group": candidate["group"],
                    "course_id": candidate["course_id"],
                    "course_code": candidate["course_code"],
                    "is_lab": candidate["is_lab"],
                })
        return index

    def _mutate_assignment(self, assignments, candidates_by_session):
        mutated = [assignment.copy() for assignment in assignments]
        if not mutated:
            return mutated
        target = self.random.choice(mutated)
        session_candidates = candidates_by_session.get(target["session_id"], [])
        if not session_candidates:
            return mutated
        replacement = self.random.choice(session_candidates)
        target.update(replacement)
        return mutated

    def _crossover_assignments(self, parent_a, parent_b=None):
        if parent_b is None:
            return [assignment.copy() for assignment in parent_a]
        child = []
        for a, b in zip(parent_a, parent_b):
            child.append(a.copy() if self.random.random() < 0.5 else b.copy())
        return child

    def _fitness(self, assignments, context):
        """Enhanced fitness with all constraint penalties"""
        penalty = 0
        faculty_hours = defaultdict(int)
        faculty_daily_hours = defaultdict(lambda: defaultdict(int))
        group_day_labs = defaultdict(int)
        slot_lookup = context["slot_by_id"]

        faculty_conflicts = set()
        room_conflicts = set()
        group_conflicts = set()

        for assignment in assignments:
            faculty_hours[assignment["faculty_id"]] += 1
            slot = slot_lookup[assignment["slot_id"]]
            faculty_daily_hours[assignment["faculty_id"]][slot.day] += 1
            
            if assignment["is_lab"]:
                group_day_labs[(assignment["group"], slot.day)] += 1

            faculty_key = (assignment["faculty_id"], slot.id)
            room_key = (assignment["room_id"], slot.id)
            group_key = (assignment["group"], slot.id)
            if faculty_key in faculty_conflicts or room_key in room_conflicts or group_key in group_conflicts:
                penalty += 100
            faculty_conflicts.add(faculty_key)
            room_conflicts.add(room_key)
            group_conflicts.add(group_key)

        # Constraint 1: Workload bounds penalty
        for faculty in context["faculty"]:
            hours = faculty_hours.get(faculty.id, 0)
            if hours < faculty.min_hours_per_week:
                penalty += (faculty.min_hours_per_week - hours) * 15
            if hours > faculty.max_hours_per_week:
                penalty += (hours - faculty.max_hours_per_week) * 15

        # Constraint 2: Lab requirement penalty
        for group in context["student_groups"]:
            labs = sum(group_day_labs.get((group.name, day), 0) for day in context["slots_by_day"].keys())
            if labs == 0:
                penalty += 30

        # Constraint 5: Consecutive lecture penalty (major enhancement)
        penalty += self._consecutive_penalty(assignments, context) * self.consecutive_penalty_weight

        # Constraint 7: Daily balance penalty
        for faculty_id, daily_hours in faculty_daily_hours.items():
            for day, hours in daily_hours.items():
                if hours > 6:  # More than 6 hours in a day
                    penalty += (hours - 6) * 5

        return penalty

    def _consecutive_penalty(self, assignments, context):
        """Constraint 5: Heavily penalize consecutive lectures of same subject"""
        penalty = 0
        slot_lookup = context["slot_by_id"]
        grouped = defaultdict(list)
        
        for assignment in assignments:
            slot = slot_lookup[assignment["slot_id"]]
            key = (assignment["group"], assignment["course_code"], slot.day)
            grouped[key].append(slot)

        for slots in grouped.values():
            slots.sort(key=lambda s: s.period)
            for first, second in zip(slots, slots[1:]):
                if second.period == first.period + 1:
                    penalty += 10  # Heavy penalty for consecutive same subject
        
        return penalty

    # --------------------------------------------------------------------- #
    # Enhanced Features (Constraints 7, 9)
    # --------------------------------------------------------------------- #
    def _generate_faculty_schedules(self, assignments, context):
        """Constraint 7: Generate detailed per-faculty daily schedules"""
        schedules = defaultdict(lambda: defaultdict(list))
        slot_lookup = context["slot_by_id"]
        course_lookup = context["course_by_id"]
        
        for assignment in assignments:
            faculty_id = assignment["faculty_id"]
            slot = slot_lookup[assignment["slot_id"]]
            course = course_lookup[assignment["course_id"]]
            
            schedules[faculty_id][slot.day].append({
                "period": slot.period,
                "time": f"{slot.start_time}-{slot.end_time}",
                "course": course.code,
                "group": assignment["group"],
                "is_lab": assignment["is_lab"],
                "room_id": assignment["room_id"]
            })
        
        # Sort by period for each day
        for faculty_id in schedules:
            for day in schedules[faculty_id]:
                schedules[faculty_id][day].sort(key=lambda x: x["period"])
        
        return dict(schedules)

    def _detect_overwork(self, assignments, context):
        """Constraint 9: Detect and alert on faculty overwork (40+ hours)"""
        warnings = []
        faculty_hours = defaultdict(int)
        faculty_lookup = context["faculty_by_id"]
        
        for assignment in assignments:
            faculty_hours[assignment["faculty_id"]] += 1
        
        for faculty_id, hours in faculty_hours.items():
            if hours >= self.overwork_threshold:
                faculty = faculty_lookup.get(faculty_id)
                if faculty:
                    warnings.append(
                        f"🚨 OVERWORK ALERT: {faculty.name} assigned {hours} hours/week (threshold: {self.overwork_threshold}h) - Review workload!"
                    )
        
        return warnings

    # --------------------------------------------------------------------- #
    # Persistence
    # --------------------------------------------------------------------- #
    def _persist_assignments(self, assignments, context):
        entries_created = 0
        for assignment in assignments:
            entry = TimetableEntry(
                course_id=assignment["course_id"],
                faculty_id=assignment["faculty_id"],
                room_id=assignment["room_id"],
                time_slot_id=assignment["slot_id"],
                student_group=assignment["group"],
            )
            self.db.session.add(entry)
            entries_created += 1

        self.db.session.commit()
        return entries_created
