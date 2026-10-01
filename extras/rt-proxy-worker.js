// （任意）リアルタイム情報の中継用 Cloudflare Worker — 無料枠で動きます。
// 一部のバス会社のリアルタイム情報は、ブラウザから直接読めない設定（CORS）になっています。
// その場合だけ、これを Cloudflare Workers に貼り付けて公開し、web/config.js の RT_PROXY に
// "https://<名前>.<アカウント>.workers.dev/?url=" を設定してください。
const ALLOWED_ORIGIN = "*"; // 例: "https://<GitHubユーザー名>.github.io" にするとより安全

export default {
  async fetch(request) {
    const target = new URL(request.url).searchParams.get("url");
    if (!target || !/^https?:\/\//.test(target)) return new Response("url required", { status: 400 });
    const r = await fetch(target, { cf: { cacheTtl: 15, cacheEverything: true } });
    const h = new Headers({
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      "Content-Type": r.headers.get("Content-Type") || "application/octet-stream",
      "Cache-Control": "max-age=15",
    });
    return new Response(r.body, { status: r.status, headers: h });
  },
};
