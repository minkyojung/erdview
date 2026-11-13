-- pg_erd.sql — PostgreSQL 스키마를 JSON 한 줄로 읽어요. pg_erd.html이 이 JSON을 ERD로 그려요.
-- 읽기만 해요(아무것도 바꾸지 않음). PostgreSQL 12 이상.
--
--   psql "postgresql://user@host:5432/dbname" -XAtf pg_erd.sql > schema.json
--   GUI(DBeaver, DataGrip, pgAdmin 등): 이 파일 전체를 실행하고 결과 칸 erd_json의 값을 복사
--
-- 일부 스키마만 보려면 아래 only_schemas를 '{public,sales}'처럼 바꿔요. 비워 두면 시스템 스키마를 뺀 전부.
-- 빠지는 것: 시스템 스키마, 파티션 자식(부모만 보임), 확장(extension)이 만든 표·뷰.
with opt as (
  select '{}'::text[] as only_schemas
),
rel as (
  select c.oid, n.nspname as schema, c.relname as name, c.relkind
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  cross join opt
  where c.relkind in ('r', 'p', 'v', 'm', 'f')
    and not c.relispartition
    and n.nspname !~ '^pg_' and n.nspname <> 'information_schema'
    and (cardinality(opt.only_schemas) = 0 or n.nspname = any (opt.only_schemas))
    and not exists (select 1 from pg_depend d
                    where d.classid = 'pg_class'::regclass and d.objid = c.oid and d.deptype = 'e')
),
col as (
  select a.attrelid,
         jsonb_agg(jsonb_build_object(
           'name', a.attname,
           'type', format_type(a.atttypid, a.atttypmod),
           'notnull', a.attnotnull,
           'default', case when a.attgenerated = '' then pg_get_expr(ad.adbin, ad.adrelid) end,
           'generated', case a.attgenerated when 's' then 'stored' when 'v' then 'virtual' end,
           'expression', case when a.attgenerated <> '' then pg_get_expr(ad.adbin, ad.adrelid) end,
           'identity', case a.attidentity when 'a' then 'always' when 'd' then 'by default' end,
           'serial', case when exists (select 1 from pg_depend d join pg_class s on s.oid = d.objid and s.relkind = 'S'
                                       where d.classid = 'pg_class'::regclass and d.refobjid = a.attrelid
                                         and d.refobjsubid = a.attnum and d.deptype = 'a') then true end,
           'comment', col_description(a.attrelid, a.attnum)
         ) order by a.attnum) as columns
  from pg_attribute a
  join rel on rel.oid = a.attrelid
  left join pg_attrdef ad on ad.adrelid = a.attrelid and ad.adnum = a.attnum
  where a.attnum > 0 and not a.attisdropped
  group by a.attrelid
),
con as (
  select k.conrelid,
         jsonb_agg(jsonb_build_object(
           'name', k.conname,
           'type', case k.contype when 'p' then 'primary key' when 'u' then 'unique' when 'f' then 'foreign key'
                                  when 'c' then 'check' else 'exclude' end,
           'columns', (select jsonb_agg(a.attname order by u.ord)
                       from unnest(k.conkey) with ordinality as u(attnum, ord)
                       join pg_attribute a on a.attrelid = k.conrelid and a.attnum = u.attnum),
           'references', case when k.contype = 'f' then jsonb_build_object(
                           'schema', fn.nspname,
                           'table', fc.relname,
                           'columns', (select jsonb_agg(a.attname order by u.ord)
                                       from unnest(k.confkey) with ordinality as u(attnum, ord)
                                       join pg_attribute a on a.attrelid = k.confrelid and a.attnum = u.attnum)) end,
           'on_delete', case when k.contype = 'f' then case k.confdeltype when 'a' then 'no action' when 'r' then 'restrict'
                          when 'c' then 'cascade' when 'n' then 'set null' else 'set default' end end,
           'on_update', case when k.contype = 'f' then case k.confupdtype when 'a' then 'no action' when 'r' then 'restrict'
                          when 'c' then 'cascade' when 'n' then 'set null' else 'set default' end end,
           'definition', pg_get_constraintdef(k.oid)
         ) order by k.contype, k.conname) as constraints
  from pg_constraint k
  join rel on rel.oid = k.conrelid
  left join pg_class fc on fc.oid = k.confrelid
  left join pg_namespace fn on fn.oid = fc.relnamespace
  where k.contype in ('p', 'u', 'f', 'c', 'x')
    and k.conparentid = 0
  group by k.conrelid
),
idx as (
  select i.indrelid,
         jsonb_agg(jsonb_build_object(
           'name', ic.relname,
           'unique', i.indisunique,
           'partial', i.indpred is not null,
           'columns', (select jsonb_agg(a.attname order by u.ord)
                       from unnest(i.indkey::int2[]) with ordinality as u(attnum, ord)
                       left join pg_attribute a on a.attrelid = i.indrelid and a.attnum = u.attnum
                       where u.ord <= i.indnkeyatts),
           'definition', pg_get_indexdef(i.indexrelid)
         ) order by ic.relname) as indexes
  from pg_index i
  join rel on rel.oid = i.indrelid
  join pg_class ic on ic.oid = i.indexrelid
  where not exists (select 1 from pg_constraint k
                    where k.conrelid = i.indrelid and k.conindid = i.indexrelid and k.contype in ('p', 'u', 'x'))
  group by i.indrelid
),
uses as (
  select r.ev_class,
         jsonb_agg(distinct jsonb_build_object('schema', n.nspname, 'table', c.relname)) as uses
  from pg_rewrite r
  join rel on rel.oid = r.ev_class and rel.relkind in ('v', 'm')
  join pg_depend d on d.classid = 'pg_rewrite'::regclass and d.objid = r.oid
                  and d.refclassid = 'pg_class'::regclass and d.deptype = 'n'
  join pg_class c on c.oid = d.refobjid and c.oid <> r.ev_class
  join pg_namespace n on n.oid = c.relnamespace
  group by r.ev_class
)
select jsonb_build_object(
  'format', 'pg-erd/1',
  'database', current_database(),
  'server_version', current_setting('server_version'),
  'generated_at', to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  'tables', coalesce(jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
    'schema', rel.schema,
    'name', rel.name,
    'kind', case rel.relkind when 'r' then 'table' when 'p' then 'partitioned table' when 'v' then 'view'
                             when 'm' then 'materialized view' else 'foreign table' end,
    'comment', obj_description(rel.oid, 'pg_class'),
    'partition_key', case when rel.relkind = 'p' then pg_get_partkeydef(rel.oid) end,
    'partitions', case when rel.relkind = 'p' then (select count(*) from pg_inherits h where h.inhparent = rel.oid) end,
    'columns', coalesce(col.columns, '[]'::jsonb),
    'constraints', coalesce(con.constraints, '[]'::jsonb),
    'indexes', coalesce(idx.indexes, '[]'::jsonb),
    'uses', uses.uses
  )) order by rel.schema, rel.name), '[]'::jsonb)
)::text as erd_json
from rel
left join col on col.attrelid = rel.oid
left join con on con.conrelid = rel.oid
left join idx on idx.indrelid = rel.oid
left join uses on uses.ev_class = rel.oid;
