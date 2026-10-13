"""
Quick outside-in website tracking audit.

Fetches the homepage (plus GTM containers and a few same-site scripts) and detects:
platform/CMS, GTM, Meta Pixel, GA4/Google Ads, TikTok and other pixels, and signs of
server-side tracking. It only sees what a browser can see; real server-to-server
(Conversions API) traffic can't be confirmed from outside.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
MAX_BYTES = 3 * 1024 * 1024
MAX_SCRIPTS = 6

URL_RE = re.compile(
    r"(https?://[^\s<>\"']+)"
    r"|(\bwww\.[^\s<>\"']+)"
    r"|(\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|com\.bd|bd|shop|store|xyz|io|co|info|"
    r"online|site|biz|me|app|dev|tech|in|us|uk|ca|au|top|live|digital|agency|website)\b(?:/[^\s<>\"']*)?)",
    re.IGNORECASE,
)


def find_url(text: str) -> str | None:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;:!?)]}।")
    if not url.lower().startswith("http"):
        url = "https://" + url
    return url


# ---------- safety: never fetch private / internal addresses ----------

class BlockedHost(Exception):
    pass


def _check_host(host: str) -> None:
    if not host:
        raise BlockedHost("no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise BlockedHost(f"domain not found: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BlockedHost(f"blocked address for {host}")


async def _guard(request: httpx.Request):
    await asyncio.to_thread(_check_host, request.url.host)


# ---------- detection rules ----------

PLATFORMS = [
    ("Shopify", [r"cdn\.shopify\.com", r"Shopify\.theme", r"myshopify\.com"]),
    ("WordPress", [r"/wp-content/", r"/wp-includes/", r'name="generator" content="WordPress']),
    ("WooCommerce", [r"woocommerce", r"wc-ajax", r"/wc-blocks"]),
    ("Elementor", [r"elementor"]),
    ("Wix", [r"static\.wixstatic\.com", r"wix-bolt", r"_wixCIDX"]),
    ("Squarespace", [r"static1\.squarespace\.com", r"Squarespace\."]),
    ("Webflow", [r"data-wf-site", r"webflow\.js", r"website-files\.com"]),
    ("Framer", [r"framerusercontent\.com", r"framer\.com/m/"]),
    ("Magento", [r"Magento_", r"mage/cookies", r"/static/version\d+/frontend/"]),
    ("BigCommerce", [r"cdn\d*\.bigcommerce\.com"]),
    ("OpenCart", [r"catalog/view/theme", r"route=common/home"]),
    ("Joomla", [r'content="Joomla', r"/media/jui/"]),
    ("Drupal", [r'content="Drupal', r"/sites/default/files/"]),
    ("Next.js (React)", [r"__NEXT_DATA__", r"/_next/static/"]),
    ("Nuxt (Vue)", [r"__NUXT__", r"/_nuxt/"]),
    ("Gatsby (React)", [r"___gatsby"]),
    ("Angular", [r"ng-version="]),
    ("Laravel", [r"laravel_session", r"XSRF-TOKEN"]),
]

WP_TRACKING_PLUGINS = [
    ("PixelYourSite", r"pixelyoursite|pys-"),
    ("Facebook for WooCommerce", r"facebook-for-woocommerce"),
    ("Meta Pixel for WordPress", r"facebook-for-wordpress|official-facebook-pixel"),
    ("GTM4WP", r"gtm4wp"),
    ("Site Kit by Google", r"google-site-kit|googlesitekit"),
    ("Conversios", r"conversios"),
]

OTHER_PIXELS = [
    ("TikTok Pixel", r"analytics\.tiktok\.com|ttq\.load"),
    ("Microsoft Clarity", r"clarity\.ms"),
    ("Hotjar", r"static\.hotjar\.com|hjid"),
    ("Snapchat Pixel", r"sc-static\.net/scevent|snaptr\("),
    ("LinkedIn Insight", r"snap\.licdn\.com|_linkedin_partner_id"),
    ("Pinterest Tag", r"s\.pinimg\.com/ct|pintrk\("),
]


@dataclass
class AuditResult:
    url: str
    final_url: str = ""
    status: int | None = None
    title: str = ""
    error: str = ""
    https: bool = False
    platforms: list[str] = field(default_factory=list)
    server: str = ""
    gtm_ids: list[str] = field(default_factory=list)
    gtm_custom_domain: list[str] = field(default_factory=list)
    ga4_ids: list[str] = field(default_factory=list)
    ads_ids: list[str] = field(default_factory=list)
    meta_pixel_ids: list[str] = field(default_factory=list)
    meta_pixel_present: bool = False
    meta_via_gtm: bool = False
    fb_domain_verified: bool = False
    server_side_signs: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    other_pixels: list[str] = field(default_factory=list)


def _uniq(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _normalize_js(s: str) -> str:
    return (s.replace("\\x27", "'").replace("\\x22", '"').replace("\\/", "/")
             .replace("\\u0027", "'").replace("\\u0022", '"').replace('\\"', '"'))


PIXEL_INIT_RE = re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"]?(\d{10,20})")
PIXEL_ID_RE = re.compile(r"(?:pixel_?ids?|pixelId)['\"]?\s*[:=,]\s*\[?\s*['\"]?(\d{12,20})", re.I)
GTM_RE = re.compile(r"\bGTM-[A-Z0-9]{4,10}\b")
GA4_RE = re.compile(r"\bG-[A-Z0-9]{6,12}\b")
ADS_RE = re.compile(r"\bAW-\d{6,12}\b")
GTM_SRC_RE = re.compile(r"""(?:src=|\.src\s*=\s*|['"])\s*['"]?((?:https?:)?//[^'"\s>]+?/[^'"\s>]*?\.js\?id=)""", re.I)
TRANSPORT_RE = re.compile(r"(?:transport_url|server_container_url)['\"]?\s*[:,]\s*['\"](https?://[^'\"]+)", re.I)


def _scan_tracking(text: str, r: AuditResult, from_gtm: bool = False):
    t = _normalize_js(text)
    r.gtm_ids += GTM_RE.findall(t)
    r.ga4_ids += GA4_RE.findall(t)
    r.ads_ids += ADS_RE.findall(t)

    pixels = PIXEL_INIT_RE.findall(t) + PIXEL_ID_RE.findall(t)
    has_meta = bool(pixels) or "fbevents.js" in t or "connect.facebook.net" in t or "fbq(" in t
    if has_meta:
        r.meta_pixel_present = True
        if from_gtm:
            r.meta_via_gtm = True
    r.meta_pixel_ids += pixels

    for url in TRANSPORT_RE.findall(t):
        host = urlparse(url).hostname or url
        if "google-analytics.com" not in host and "googletagmanager.com" not in host:
            r.server_side_signs.append(f"GA4 data server container-e jacche ({host})")
    if "stape.io" in t or "stape.net" in t:
        r.server_side_signs.append("Stape (server-side GTM hosting) use hocche")
    if "capig" in t.lower() and "facebook" in t.lower():
        r.server_side_signs.append("Meta Conversions API Gateway-er lokkhon")
    for name, pat in OTHER_PIXELS:
        if re.search(pat, t, re.I):
            r.other_pixels.append(name)


async def run_audit(url: str) -> AuditResult:
    r = AuditResult(url=url)
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=15, follow_redirects=True, max_redirects=5,
            event_hooks={"request": [_guard]},
        ) as http:
            try:
                resp = await http.get(url)
            except httpx.ConnectError:
                if url.startswith("https://"):
                    resp = await http.get("http://" + url[len("https://"):])
                else:
                    raise
            r.status = resp.status_code
            r.final_url = str(resp.url)
            r.https = r.final_url.startswith("https://")
            html = resp.text[:MAX_BYTES]

            server = resp.headers.get("server", "")
            powered = resp.headers.get("x-powered-by", "")
            r.server = ", ".join(x for x in (server, powered) if x)
            if resp.status_code >= 400:
                r.error = f"Site {resp.status_code} response dicche (block ba error)."
                if "cloudflare" in server.lower() and resp.status_code in (403, 503):
                    r.error += " Cloudflare bot protection-er karone hote pare."

            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            r.title = re.sub(r"\s+", " ", m.group(1)).strip()[:100] if m else ""

            cookies = " ".join(resp.headers.get_list("set-cookie"))
            for name, pats in PLATFORMS:
                if any(re.search(p, html + cookies, re.I) for p in pats):
                    r.platforms.append(name)
            if "WooCommerce" in r.platforms and "WordPress" not in r.platforms:
                r.platforms.insert(0, "WordPress")
            for name, pat in WP_TRACKING_PLUGINS:
                if re.search(pat, html, re.I):
                    r.plugins.append(name)
            r.fb_domain_verified = "facebook-domain-verification" in html

            _scan_tracking(html, r)

            # GTM loaded from a custom domain = classic server-side GTM sign
            site_host = urlparse(r.final_url).hostname or ""
            for src in GTM_SRC_RE.findall(_normalize_js(html)):
                host = urlparse(src if src.startswith("http") else "https:" + src).hostname or ""
                if host and "googletagmanager.com" not in host and "google" not in host:
                    r.gtm_custom_domain.append(host)
                    r.server_side_signs.append(f"GTM custom domain theke load hocche ({host})")

            # Look inside GTM containers for pixels and server-side config
            r.gtm_ids = _uniq(r.gtm_ids)
            for gid in r.gtm_ids[:3]:
                base = f"https://{r.gtm_custom_domain[0]}" if r.gtm_custom_domain else "https://www.googletagmanager.com"
                try:
                    g = await http.get(f"{base}/gtm.js?id={gid}")
                    if g.status_code == 200:
                        _scan_tracking(g.text[:MAX_BYTES], r, from_gtm=True)
                except Exception:
                    pass

            # A few same-site scripts (pixels are often bundled in theme/app JS)
            srcs = re.findall(r"<script[^>]+src=['\"]([^'\"]+)['\"]", html, re.I)
            checked = 0
            for src in srcs:
                full = urljoin(r.final_url, src)
                h = urlparse(full).hostname or ""
                if h != site_host or checked >= MAX_SCRIPTS:
                    continue
                if re.search(r"jquery|wp-includes|polyfill|swiper|slick|bootstrap", full, re.I):
                    continue
                checked += 1
                try:
                    s = await http.get(full)
                    if s.status_code == 200 and len(s.content) < MAX_BYTES:
                        _scan_tracking(s.text, r)
                except Exception:
                    pass
    except BlockedHost as exc:
        r.error = f"Ei address check kora jabe na: {exc}"
    except httpx.TimeoutException:
        r.error = "Site load hote onek deri korche (timeout)."
    except Exception as exc:
        r.error = f"Site open kora jayni: {type(exc).__name__}"

    r.gtm_ids = _uniq(r.gtm_ids)
    r.ga4_ids = _uniq(r.ga4_ids)
    r.ads_ids = _uniq(r.ads_ids)
    r.meta_pixel_ids = _uniq(r.meta_pixel_ids)
    r.server_side_signs = _uniq(r.server_side_signs)
    r.other_pixels = _uniq(r.other_pixels)
    r.platforms = _uniq(r.platforms)
    return r


def _yes(ok: bool) -> str:
    return "✅" if ok else "❌"


def format_report(r: AuditResult) -> str:
    domain = urlparse(r.final_url or r.url).hostname or r.url
    lines = [f"🔍 Website Audit: {domain}", "🔒 Internal report — client-ke pathaben na", ""]
    if r.error and not r.status or (r.status and r.status >= 400 and not r.platforms):
        lines.append(f"⚠️ {r.error or 'Site check kora jayni.'}")
        lines.append("Client-er kach theke Pixel/GTM access niye manually check korte hobe.")
        return "\n".join(lines)

    if r.title:
        lines.append(f"Title: {r.title}")
    lines.append(f"Platform: {', '.join(r.platforms) if r.platforms else 'Nishchit na (custom/hidden)'}")
    if r.server:
        lines.append(f"Server: {r.server}")
    lines.append(f"HTTPS: {_yes(r.https)}")
    lines.append("")

    lines.append(f"GTM: {_yes(bool(r.gtm_ids))} {', '.join(r.gtm_ids)}".rstrip())
    if r.meta_pixel_present:
        ids = ", ".join(r.meta_pixel_ids) if r.meta_pixel_ids else "ID pawa jayni"
        how = " (GTM-er maddhome)" if r.meta_via_gtm else ""
        lines.append(f"Meta Pixel: ✅ {ids}{how}")
        if len(r.meta_pixel_ids) > 1:
            lines.append("  ⚠️ Ekadhik Pixel ID — duplicate event-er jhuki")
    else:
        lines.append("Meta Pixel: ❌ Pawa jayni")
    lines.append(f"GA4: {_yes(bool(r.ga4_ids))} {', '.join(r.ga4_ids)}".rstrip())
    if r.ads_ids:
        lines.append(f"Google Ads: ✅ {', '.join(r.ads_ids)}")
    lines.append(f"FB Domain Verification: {_yes(r.fb_domain_verified)}")
    if r.other_pixels:
        lines.append(f"Other: {', '.join(r.other_pixels)}")
    if r.plugins:
        lines.append(f"Tracking Plugin: {', '.join(r.plugins)}")
    lines.append("")

    if r.server_side_signs:
        lines.append("Server-Side Tracking: ✅ Lokkhon pawa geche")
        lines += [f"  • {s}" for s in r.server_side_signs]
    else:
        lines.append("Server-Side Tracking: ❌ Kono lokkhon nei (shudhu browser tracking mone hocche)")
    lines.append("")

    lines.append("💡 Sales angle:")
    lines += [f"  • {a}" for a in sales_angles(r)]
    lines.append("")
    lines.append("ℹ️ Shudhu homepage bairer theke check kora. Server theke CAPI chole kina 100% nishchit "
                 "bola jay na — Events Manager access pele confirm kora jabe.")
    return "\n".join(lines)


def sales_angles(r: AuditResult) -> list[str]:
    a = []
    if not r.meta_pixel_present:
        a.append("Meta Pixel nei — Ads chalale conversion track hocche na. Full Pixel + Server-Side setup pitch korun.")
    elif not r.server_side_signs:
        a.append("Pixel ache kintu server-side nei — iOS/ad-blocker-e data harate pare. Server-Side Tracking + $8 offer pitch korun.")
    else:
        a.append("Server-side-er lokkhon ache — Free Audit diye event quality, deduplication ar errors check korar offer din.")
    if not r.gtm_ids:
        a.append("GTM nei — GTM diye shob tracking ek jaygay manage korar shubidha bolun.")
    if len(r.meta_pixel_ids) > 1:
        a.append("Ekadhik Pixel — duplicate/missing event thakte pare, Audit-er strong karon.")
    if "PixelYourSite" in r.plugins or "Facebook for WooCommerce" in r.plugins:
        a.append("Plugin-based tracking — plugin CAPI thik moto configure kora ache kina check korar offer din.")
    if "Shopify" in r.platforms:
        a.append("Shopify — native Meta app-er CAPI setting ar event match quality check korar offer din.")
    if not r.fb_domain_verified and r.meta_pixel_present:
        a.append("Domain verification meta tag dekha jacche na — Business Manager-e verify kora ache kina dekhte hobe.")
    return a


def audit_context_for_ai(r: AuditResult) -> str:
    """Short factual summary passed to the AI so the client reply uses real findings."""
    if r.error and not r.platforms:
        return f"Website audit could not complete: {r.error}"
    return (
        "Website audit findings (outside-in, homepage only; real, use them, don't exaggerate):\n"
        f"- Platform: {', '.join(r.platforms) or 'unknown'}\n"
        f"- GTM: {'yes ' + ', '.join(r.gtm_ids) if r.gtm_ids else 'not found'}\n"
        f"- Meta Pixel: {'yes' if r.meta_pixel_present else 'not found'}"
        f"{' (multiple IDs: ' + ', '.join(r.meta_pixel_ids) + ')' if len(r.meta_pixel_ids) > 1 else ''}\n"
        f"- GA4: {'yes' if r.ga4_ids else 'not found'}\n"
        f"- Server-side tracking signs: {'; '.join(r.server_side_signs) if r.server_side_signs else 'none found'}\n"
        "Use 1-2 of these findings naturally in the reply to show we already checked their site, "
        "point to the gap, and move to the next step. Server-side (CAPI) can't be fully confirmed "
        "from outside, so phrase it as 'দেখা যাচ্ছে না' rather than a certain claim."
    )
