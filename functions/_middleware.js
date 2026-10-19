// Cloudflare Pages middleware: keeps the whole site (including preview deployments) behind one
// password. Any username works; the password is the SITE_PASSWORD secret on the Pages project.
// If the secret is missing, the site stays closed rather than open.

const encoder = new TextEncoder();

async function sameSecret(a, b) {
  // Compare digests so the check takes the same time whatever the input.
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  const x = new Uint8Array(da);
  const y = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function passwordFrom(request) {
  const header = request.headers.get("Authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) return null;
  try {
    const decoded = new TextDecoder().decode(Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0)));
    const colon = decoded.indexOf(":");
    return colon >= 0 ? decoded.slice(colon + 1) : null;
  } catch {
    return null;
  }
}

export async function onRequest(context) {
  const { request, env, next } = context;
  const expected = env.SITE_PASSWORD;
  if (!expected) {
    return new Response("MicroHOT is closed: set the SITE_PASSWORD secret on the Pages project.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "X-Robots-Tag": "noindex" },
    });
  }
  const given = passwordFrom(request);
  if (given === null || !(await sameSecret(given, expected))) {
    return new Response("Password required.", {
      status: 401,
      headers: {
        "WWW-Authenticate": 'Basic realm="MicroHOT", charset="UTF-8"',
        "Content-Type": "text/plain; charset=utf-8",
        "X-Robots-Tag": "noindex",
      },
    });
  }
  const response = await next();
  const out = new Response(response.body, response);
  out.headers.set("X-Robots-Tag", "noindex, nofollow");
  if (!new URL(request.url).pathname.startsWith("/static/")) {
    // Pages change three times a day; never let a shared cache keep a private copy.
    out.headers.set("Cache-Control", "private, no-cache");
  }
  return out;
}
