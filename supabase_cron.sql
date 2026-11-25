-- Run in the Munim project's Supabase SQL Editor after supabase_schema.sql.
-- Store the repo-scoped GitHub Actions token in Vault as munim_github_dispatch_token.
begin;

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

create schema if not exists munim_internal;
revoke all on schema munim_internal from public;

create or replace function munim_internal.dispatch_digest(task text)
returns bigint language plpgsql set search_path = '' as $$
declare
  dispatch_token text;
begin
  if task is null or task not in ('morning', 'evening', 'check') then
    raise exception 'Unsupported Munim task';
  end if;

  if task <> 'check' and exists (
    select 1 from public.digests
    where slot = task
      and day = (now() at time zone 'Asia/Kolkata')::date
      and delivery_status in ('sending', 'sent')
  ) then
    return null;
  end if;

  select decrypted_secret into dispatch_token
  from vault.decrypted_secrets where name = 'munim_github_dispatch_token';
  if nullif(btrim(dispatch_token), '') is null then
    raise exception 'Add munim_github_dispatch_token to Supabase Vault before dispatching Munim';
  end if;

  return net.http_post(
    url := 'https://api.github.com/repos/05Atharva/Munim/actions/workflows/digests.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || btrim(dispatch_token),
      'Accept', 'application/vnd.github+json',
      'Content-Type', 'application/json',
      'User-Agent', 'munim-supabase-cron'
    ),
    body := jsonb_build_object(
      'ref', 'main', 'inputs', jsonb_build_object('task', task, 'scheduled_delivery', true)
    ),
    timeout_milliseconds := 10000
  );
end;
$$;

revoke all on function munim_internal.dispatch_digest(text) from public, anon, authenticated, service_role;

do $$
declare
  zone text := coalesce(current_setting('cron.timezone', true), 'GMT');
  morning text;
  evening text;
begin
  if zone in ('GMT', 'UTC', 'Etc/UTC', 'Etc/GMT') then
    morning := '45 4 * * *';
    evening := '45 12 * * *';
  elsif zone = 'Asia/Kolkata' then
    morning := '15 10 * * *';
    evening := '15 18 * * *';
  else
    raise exception 'Unsupported pg_cron timezone % for Munim schedule', zone;
  end if;

  perform cron.schedule('munim-morning-dispatch', morning, $job$select munim_internal.dispatch_digest('morning')$job$);
  perform cron.schedule('munim-evening-dispatch', evening, $job$select munim_internal.dispatch_digest('evening')$job$);
end;
$$;

select jobname, schedule, active from cron.job
where jobname in ('munim-morning-dispatch', 'munim-evening-dispatch')
order by jobname;

commit;
