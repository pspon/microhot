// MicroHOT scheduler: a Cloudflare Worker that starts the "MicroHOT run" workflow on time.
//
// GitHub's own cron is best-effort and can start runs hours late or skip them, so this Worker
// asks GitHub to run the workflow (workflow_dispatch) at the local times in RUN_HOURS.
// Cloudflare cron triggers are in UTC, so wrangler.toml fires at both the summer and the winter
// UTC hours, and this code keeps only the firings that fall on a run hour in TIMEZONE.

export function localHour(date, timeZone) {
  const hour = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    hour: "2-digit",
    hourCycle: "h23",
  })
    .formatToParts(date)
    .find((p) => p.type === "hour").value;
  return Number(hour);
}

export function isDue(date, env) {
  const hours = String(env.RUN_HOURS || "6,12,18")
    .split(",")
    .map((h) => Number(h.trim()));
  return hours.includes(localHour(date, env.TIMEZONE || "America/Toronto"));
}

async function dispatch(env) {
  if (!env.DISPATCH_TOKEN) throw new Error("DISPATCH_TOKEN is not set");
  const url = `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "microhot-scheduler",
      },
      body: JSON.stringify({ ref: env.REF || "main" }),
    });
    if (res.status === 204) {
      console.log(`Started ${env.WORKFLOW} on ${env.REPO}`);
      return;
    }
    const detail = (await res.text()).slice(0, 300);
    // 401/403/404/422 mean a bad token or setting; retrying will not help.
    if (res.status < 500 && res.status !== 429) {
      throw new Error(`GitHub refused the dispatch (${res.status}): ${detail}`);
    }
    console.log(`Attempt ${attempt} failed (${res.status}): ${detail}`);
    await new Promise((r) => setTimeout(r, 20000 * attempt));
  }
  throw new Error("GitHub did not accept the dispatch after 3 attempts");
}

export default {
  async scheduled(event, env, ctx) {
    const when = new Date(event.scheduledTime);
    if (!isDue(when, env)) return; // the other half of the summer/winter pair
    ctx.waitUntil(dispatch(env));
  },
};
