-- V32 스테이징 영속 대기열 (운영 테이블과 무관, 신규 생성)
create table if not exists wm_v32_queue (
  id bigint generated always as identity primary key,
  project_id uuid not null,
  status text not null default 'queued',
  attempt_token uuid,
  submitted_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  heartbeat_at timestamptz,
  eta_lo_s int, eta_mid_s int, eta_hi_s int,
  result text, error text
);
create unique index if not exists wm_v32_queue_active_uniq
  on wm_v32_queue(project_id) where status in ('queued','running','finishing');
