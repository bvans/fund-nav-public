const OWNER = "bvans";
const REPO = "fund-nav-public";
const WORKFLOW = "fetch-index-quotes.yml";
const REF = "main";

async function dispatchGitHub(env, scheduledTime, cron) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("Missing GITHUB_TOKEN secret");
  }

  const url =
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "fund-nav-index-scheduler",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: REF,
      inputs: {
        source: "cloudflare-cron",
        scheduled_at: new Date(scheduledTime).toISOString(),
      },
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `GitHub workflow dispatch failed: HTTP ${response.status} ${body}`
    );
  }

  console.log(
    JSON.stringify({
      ok: true,
      cron,
      scheduled_at: new Date(scheduledTime).toISOString(),
      workflow: WORKFLOW,
      ref: REF,
    })
  );
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      dispatchGitHub(env, controller.scheduledTime, controller.cron)
    );
  },

  async fetch() {
    return new Response("fund-nav index scheduler: ok\n", {
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
