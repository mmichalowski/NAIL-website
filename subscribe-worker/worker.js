/**
 * NAIL Digest — Subscribe Worker
 * ================================
 * Handles double opt-in subscribe / confirm / unsubscribe, and serves the
 * confirmed-subscriber list to the ni-biweekly.yml GitHub Action so it can
 * email out each new issue.
 *
 * Endpoints:
 *   POST /subscribe    { email, hp }   — start double opt-in (hp = honeypot)
 *   GET  /confirm       ?token=        — confirm a pending subscription
 *   GET  /unsubscribe   ?token=        — unsubscribe
 *   GET  /subscribers                  — confirmed list, Bearer-auth only (GitHub Action)
 *
 * Bindings expected (see wrangler.toml / Cloudflare dashboard):
 *   SUBSCRIBERS         — KV namespace
 *   RESEND_API_KEY      — secret, used to send the confirmation email
 *   SUBSCRIBER_API_KEY  — secret, shared with the GitHub Action for /subscribers auth
 */

const ALLOWED_ORIGINS = [
  "https://www.nailcollab.org",
  "https://nailcollab.org",
];

function corsHeaders(origin) {
  const allow = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function isValidEmail(email) {
  return typeof email === "string" &&
         email.length <= 254 &&
         /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

async function sendConfirmEmail(env, to, confirmUrl) {
  const html = `
  <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px;">
    <h2 style="color:#111C26;">Confirm your subscription</h2>
    <p style="color:#5E6B76;line-height:1.6;">
      One more step — click below to start receiving NAIL Digest every other Monday.
    </p>
    <a href="${confirmUrl}"
       style="display:inline-block;background:#E8C46A;color:#111C26;
              font-weight:600;text-decoration:none;padding:12px 24px;border-radius:99px;margin-top:12px;">
      Confirm subscription
    </a>
    <p style="color:#5E6B76;font-size:12px;margin-top:24px;">
      Didn't request this? Ignore this email — you won't be subscribed unless you click the link above.
    </p>
  </div>`;

  const resp = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: "NAIL Digest <hello@digest.nailcollab.org>",
      reply_to: "ainurse@nailcollab.org",
      to: [to],
      subject: "Confirm your NAIL Digest subscription",
      html,
    }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`Resend error ${resp.status}: ${body}`);
  }
}

function brandedPage(title, message) {
  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${title} · NAIL Digest</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500&family=Instrument+Sans:wght@400;600&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Instrument Sans',sans-serif;background:#1D2B3A;color:#fff;min-height:100vh;
     display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;}
h1{font-family:'Fraunces',serif;font-weight:500;font-size:clamp(26px,4vw,38px);margin-bottom:14px;color:#fff;}
p{color:rgba(255,255,255,.7);font-size:15px;line-height:1.7;max-width:420px;margin:0 auto 24px;}
a{display:inline-flex;background:#E8C46A;color:#111C26;font-weight:600;font-size:14px;
  padding:12px 24px;border-radius:99px;text-decoration:none;}
</style></head>
<body><div>
  <h1>${title}</h1>
  <p>${message}</p>
  <a href="https://www.nailcollab.org/ni-biweekly/">Back to NAIL Digest</a>
</div></body></html>`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(origin);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // ── POST /subscribe ──────────────────────────────────────────────────
    if (url.pathname === "/subscribe" && request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response(JSON.stringify({ error: "Invalid request." }), { status: 400, headers: cors });
      }
      const { email, hp } = body;

      // Honeypot: real visitors never fill this hidden field in. Bots do.
      // Return a fake success so bots don't learn to look for a different signal.
      if (hp) {
        return new Response(JSON.stringify({ ok: true }), { headers: cors });
      }
      if (!isValidEmail(email)) {
        return new Response(JSON.stringify({ error: "Please enter a valid email address." }), { status: 400, headers: cors });
      }

      const normalized = email.trim().toLowerCase();
      const key = `sub:${normalized}`;
      const existingRaw = await env.SUBSCRIBERS.get(key);
      if (existingRaw) {
        const existing = JSON.parse(existingRaw);
        if (existing.status === "confirmed") {
          return new Response(JSON.stringify({ ok: true, already: true }), { headers: cors });
        }
        // status is "pending" or "unsubscribed" — fall through and re-send confirmation
      }

      const confirmToken = crypto.randomUUID();
      const unsubToken = crypto.randomUUID();
      const record = {
        email: normalized,
        status: "pending",
        confirm_token: confirmToken,
        unsub_token: unsubToken,
        subscribed_at: new Date().toISOString(),
        confirmed_at: null,
      };
      await env.SUBSCRIBERS.put(key, JSON.stringify(record));
      await env.SUBSCRIBERS.put(`token:${confirmToken}`, normalized);
      await env.SUBSCRIBERS.put(`token:${unsubToken}`, normalized);

      const confirmUrl = `${url.origin}/confirm?token=${confirmToken}`;
      try {
        await sendConfirmEmail(env, normalized, confirmUrl);
      } catch (e) {
        return new Response(
          JSON.stringify({ error: "Could not send confirmation email. Try again shortly." }),
          { status: 502, headers: cors }
        );
      }

      return new Response(JSON.stringify({ ok: true }), { headers: cors });
    }

    // ── GET /confirm ────────────────────────────────────────────────────
    if (url.pathname === "/confirm" && request.method === "GET") {
      const token = url.searchParams.get("token") || "";
      const email = await env.SUBSCRIBERS.get(`token:${token}`);
      if (!email) {
        return new Response(brandedPage("Link expired", "This confirmation link is invalid or has already been used."),
          { status: 404, headers: { "Content-Type": "text/html" } });
      }
      const key = `sub:${email}`;
      const raw = await env.SUBSCRIBERS.get(key);
      if (!raw) {
        return new Response(brandedPage("Not found", "We couldn't find that subscription."),
          { status: 404, headers: { "Content-Type": "text/html" } });
      }
      const rec = JSON.parse(raw);
      rec.status = "confirmed";
      rec.confirmed_at = new Date().toISOString();
      await env.SUBSCRIBERS.put(key, JSON.stringify(rec));
      return new Response(
        brandedPage("You're subscribed", "You'll get an email every other Monday when a new NAIL Digest issue goes live."),
        { headers: { "Content-Type": "text/html" } }
      );
    }

    // ── GET /unsubscribe ────────────────────────────────────────────────
    if (url.pathname === "/unsubscribe" && request.method === "GET") {
      const token = url.searchParams.get("token") || "";
      const email = await env.SUBSCRIBERS.get(`token:${token}`);
      if (!email) {
        return new Response(brandedPage("Link expired", "This unsubscribe link is invalid or has already been used."),
          { status: 404, headers: { "Content-Type": "text/html" } });
      }
      const key = `sub:${email}`;
      const raw = await env.SUBSCRIBERS.get(key);
      if (raw) {
        const rec = JSON.parse(raw);
        rec.status = "unsubscribed";
        await env.SUBSCRIBERS.put(key, JSON.stringify(rec));
      }
      return new Response(
        brandedPage("You're unsubscribed", "You won't receive further NAIL Digest emails. You can resubscribe any time."),
        { headers: { "Content-Type": "text/html" } }
      );
    }

    // ── GET /subscribers (GitHub Action only) ──────────────────────────────
    if (url.pathname === "/subscribers" && request.method === "GET") {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.SUBSCRIBER_API_KEY}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      const confirmed = [];
      let cursor;
      do {
        const page = await env.SUBSCRIBERS.list({ prefix: "sub:", cursor });
        for (const k of page.keys) {
          const raw = await env.SUBSCRIBERS.get(k.name);
          if (!raw) continue;
          const rec = JSON.parse(raw);
          if (rec.status === "confirmed") {
            confirmed.push({ email: rec.email, unsub_token: rec.unsub_token });
          }
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);

      return new Response(JSON.stringify(confirmed), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("Not found", { status: 404, headers: cors });
  },
};
