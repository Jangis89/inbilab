-- ============================================
-- 인비랩 1단계 데이터베이스 설계
-- Supabase SQL Editor에 붙여넣고 Run 하면 됩니다.
-- ============================================

-- 1) 게시글 표
create table if not exists public.posts (
  id            bigint generated always as identity primary key,
  board         text not null default 'community',   -- community(성장기록실) / study(학습자료)
  category      text not null default '자유',         -- 수익 인증 / 구독자 성장 / 후기 / 자유 / 자료
  title         text not null,
  content       text not null,
  author_id     uuid not null references auth.users (id) on delete cascade,
  author_nick   text not null default '회원',
  views         integer not null default 0,
  comment_count integer not null default 0,
  created_at    timestamptz not null default now()
);

-- 2) 댓글 표
create table if not exists public.comments (
  id          bigint generated always as identity primary key,
  post_id     bigint not null references public.posts (id) on delete cascade,
  author_id   uuid not null references auth.users (id) on delete cascade,
  author_nick text not null default '회원',
  content     text not null,
  created_at  timestamptz not null default now()
);

-- 3) 보안 규칙(RLS): 읽기는 누구나, 쓰기는 로그인한 본인만
alter table public.posts enable row level security;
alter table public.comments enable row level security;

drop policy if exists "posts_read"   on public.posts;
drop policy if exists "posts_insert" on public.posts;
drop policy if exists "posts_delete" on public.posts;
drop policy if exists "posts_update_own" on public.posts;
create policy "posts_read"   on public.posts for select using (true);
create policy "posts_insert" on public.posts for insert with check (auth.uid() = author_id);
create policy "posts_update_own" on public.posts for update using (auth.uid() = author_id);
create policy "posts_delete" on public.posts for delete using (auth.uid() = author_id);

drop policy if exists "comments_read"   on public.comments;
drop policy if exists "comments_insert" on public.comments;
drop policy if exists "comments_delete" on public.comments;
create policy "comments_read"   on public.comments for select using (true);
create policy "comments_insert" on public.comments for insert with check (auth.uid() = author_id);
create policy "comments_delete" on public.comments for delete using (auth.uid() = author_id);

-- 4) 조회수 +1 함수 (누구나 호출 가능하되 조회수만 올릴 수 있음)
create or replace function public.increment_views(post_id_input bigint)
returns void
language sql
security definer
set search_path = public
as $$
  update public.posts set views = views + 1 where id = post_id_input;
$$;

-- 5) 댓글 수 자동 갱신
create or replace function public.sync_comment_count()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if (tg_op = 'INSERT') then
    update public.posts set comment_count = comment_count + 1 where id = new.post_id;
  elsif (tg_op = 'DELETE') then
    update public.posts set comment_count = greatest(comment_count - 1, 0) where id = old.post_id;
  end if;
  return null;
end;
$$;

drop trigger if exists trg_comment_count on public.comments;
create trigger trg_comment_count
after insert or delete on public.comments
for each row execute function public.sync_comment_count();
