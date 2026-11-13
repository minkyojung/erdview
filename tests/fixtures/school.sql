-- The sample schema database.build draws (7 tables, single-column FKs). Used by tests/test_pg_erd.py.
create table students (
  id bigint primary key generated always as identity,
  first_name text not null,
  last_name text not null,
  email text not null unique,
  enrollment_date date not null
);
create table courses (
  id bigint primary key generated always as identity,
  course_name text not null,
  course_code text not null unique,
  description text,
  start_date date not null,
  end_date date not null
);
create table instructors (
  id bigint primary key generated always as identity,
  first_name text not null,
  last_name text not null,
  email text not null unique,
  hire_date date not null
);
create table enrollments (
  id bigint primary key generated always as identity,
  student_id bigint references students (id),
  course_id bigint references courses (id),
  enrollment_date date not null
);
create table course_instructors (
  id bigint primary key generated always as identity,
  course_id bigint references courses (id),
  instructor_id bigint references instructors (id)
);
create table assignments (
  id bigint primary key generated always as identity,
  course_id bigint references courses (id),
  title text not null,
  description text,
  due_date date not null
);
create table submissions (
  id bigint primary key generated always as identity,
  assignment_id bigint references assignments (id),
  student_id bigint references students (id),
  submission_date date not null,
  grade numeric
);
