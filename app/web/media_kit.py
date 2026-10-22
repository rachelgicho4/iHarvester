"""Public media-kit and browseable network directory for prospective clients."""
# ruff: noqa: E501

from __future__ import annotations

from typing import Any


def _number(value: Any) -> int:
    return max(0, int(value or 0))


def _safe_json(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _placements_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}
    items: list[dict[str, Any]] = []
    for item in list(source.get("items") or []):
        label = str(item.get("label") or "Public channel").strip()
        url = str(item.get("url") or "").strip()
        if not label or not url.startswith("https://t.me/"):
            continue
        audience = item.get("subscriber_slots")
        items.append(
            {
                "label": label[:120],
                "url": url,
                "subscriber_slots": int(audience)
                if isinstance(audience, (int, float)) and not isinstance(audience, bool) and audience >= 0
                else None,
            }
        )
    page_size = max(1, min(60, _number(source.get("page_size")) or 24))
    return {
        "items": items[:page_size],
        "total": max(len(items), _number(source.get("total"))),
        "page": max(0, _number(source.get("page"))),
        "page_size": page_size,
    }


def media_kit_payload(
    audience: dict[str, Any] | None,
    public_placements: dict[str, Any] | None = None,
    *,
    contact_url: str | None = None,
    insights_url: str = "/promote/insights",
    placements_url: str = "/promote/placements",
) -> dict[str, Any]:
    """Build candid public inventory data without selling unreliable view averages."""
    audience = audience or {}
    return {
        "network": {
            "active_placements": _number(audience.get("active_channels")),
            "known_audience_placements": _number(audience.get("known_audience_channels")),
            "subscriber_slots": _number(audience.get("subscriber_slots")),
            "unknown_audience_placements": _number(audience.get("unknown_audience_channels")),
            "public_placements": _number(audience.get("public_channels")),
        },
        "placements": _placements_payload(public_placements),
        "contact_url": contact_url if contact_url and (contact_url.startswith("https://t.me/") or contact_url.startswith("tg://user?id=")) else None,
        "insights_url": insights_url if insights_url.startswith("/promote") else "/promote/insights",
        "placements_url": placements_url if placements_url.startswith("/promote") else "/promote/placements",
    }


def render_media_kit(payload: dict[str, Any]) -> str:
    """Render a premium, transparent public network page."""
    initial = _safe_json(payload)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#080e1a"><title>iHarvester | Campaign distribution</title>
<style>
:root{{--ink:#f0f6ff;--muted:#9eafc7;--line:#263752;--panel:#102138e8;--base:#080e1a;--green:#51e5aa;--blue:#79b9ff}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:radial-gradient(circle at 7% 4%,#164163 0,transparent 35rem),radial-gradient(circle at 92% 10%,#29315d 0,transparent 32rem),var(--base);color:var(--ink);font:16px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(120deg,#fff0 35%,#ffffff09 50%,#fff0 65%);opacity:.35}}main{{max-width:1130px;margin:auto;padding:24px 18px 70px;position:relative}}nav{{display:flex;align-items:center;justify-content:space-between;gap:15px;margin-bottom:56px}}.brand{{font-weight:900;letter-spacing:-.04em;font-size:20px}}.brand i{{font-style:normal;color:var(--green)}}.navlink{{color:#c9daf0;text-decoration:none;font-size:14px;font-weight:700;border:1px solid #ffffff1f;border-radius:99px;padding:8px 13px}}.hero{{display:grid;grid-template-columns:1.4fr .8fr;gap:30px;align-items:center;padding:28px 0 60px}}.eyebrow{{color:#9cc8ff;text-transform:uppercase;letter-spacing:.14em;font-weight:850;font-size:12px}}h1{{font-size:clamp(42px,7vw,78px);line-height:.97;letter-spacing:-.065em;margin:14px 0 18px;max-width:730px}}h1 em{{font-style:normal;color:var(--green)}}h2{{font-size:28px;letter-spacing:-.04em;line-height:1.05;margin:0 0 10px}}h3{{font-size:15px;margin:0 0 5px}}p{{margin:8px 0}}.muted{{color:var(--muted)}}.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:25px}}.button{{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:12px;padding:12px 16px;font-weight:850;text-decoration:none;background:linear-gradient(135deg,#4ce0a5,#88f5c8);color:#052218;cursor:pointer}}.button.ghost,.button.secondary{{background:#172b43;color:#e1edff;border:1px solid #ffffff1a}}button:disabled{{opacity:.45;cursor:not-allowed}}.card{{background:linear-gradient(145deg,#183452e8,#0e1e32e8);border:1px solid var(--line);box-shadow:0 22px 60px #0006;border-radius:24px;padding:22px;backdrop-filter:blur(12px)}}.hero-card{{transform:rotate(2deg);border-color:#61eab460}}.hero-card>*{{transform:rotate(-2deg)}}.signal{{display:flex;align-items:center;gap:8px;color:#d4ffe9;font-size:13px;font-weight:800}}.dot{{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px #51e5aa22}}.big-number{{font-size:48px;font-weight:900;letter-spacing:-.06em;line-height:1;margin:20px 0 5px}}.rule{{height:1px;background:#ffffff1a;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.metric{{padding:20px;border:1px solid #ffffff15;border-radius:19px;background:#09182a8c}}.metric b{{font-size:28px;display:block;letter-spacing:-.04em;margin-top:5px}}.section{{margin-top:70px}}.section-head{{max-width:720px;margin-bottom:22px}}.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.step{{padding:20px;border:1px solid var(--line);border-radius:18px;background:#0c1a2dd1}}.step span{{display:inline-grid;place-items:center;width:29px;height:29px;border-radius:50%;background:#1a4051;color:#adffe2;font-weight:900;margin-bottom:14px}}.directory{{display:grid;grid-template-columns:repeat(auto-fill,minmax(238px,1fr));gap:11px;margin-top:18px}}.placement{{display:flex;min-height:112px;flex-direction:column;justify-content:space-between;gap:13px;padding:14px;border-radius:16px;text-decoration:none;color:var(--ink);background:#071525a8;border:1px solid #ffffff16}}.placement:hover{{border-color:#6ac3ff99;background:#0c2036}}.placement b{{font-size:15px;line-height:1.25}}.placement small{{color:var(--muted)}}.placement .open{{color:var(--blue);font-size:12px;font-weight:850}}.notice{{padding:15px;border:1px dashed #54708f;border-radius:14px;color:var(--muted)}}.pager{{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-top:16px;flex-wrap:wrap}}.fine{{color:#9cb0cc;font-size:12px}}.callout{{padding:20px;border-radius:20px;background:linear-gradient(105deg,#173d4a,#232957);border:1px solid #7bceff55;display:flex;justify-content:space-between;gap:20px;align-items:center}}footer{{margin-top:50px;text-align:center;color:#7f91aa;font-size:12px}}@media(max-width:760px){{nav{{margin-bottom:30px}}.hero{{grid-template-columns:1fr}}.hero-card{{transform:none}}.hero-card>*{{transform:none}}.grid,.steps{{grid-template-columns:1fr}}.section{{margin-top:48px}}.callout{{display:block}}.callout .button{{margin-top:14px}}}}
</style></head><body><main><nav><div class="brand">i<i>Harvester</i></div><a class="navlink" href="#network">Browse the network</a></nav>
<section class="hero"><div><div class="eyebrow">Telegram campaign distribution</div><h1>Put your promotion in front of a <em>live</em> network.</h1><p class="muted">Professionally managed Telegram distribution with verified delivery, transparent inventory and a private campaign dashboard.</p><div class="actions"><a id="hero-contact" class="button" href="#contact">Plan a promotion</a><a class="button ghost" href="#network">Browse live channels</a></div></div><aside class="card hero-card"><div class="signal"><span class="dot"></span> Active distribution inventory</div><div id="hero-audience" class="big-number">-</div><p class="muted">subscriber memberships across active placements</p><div class="rule"></div><div class="fine" id="hero-note">Verifying the current network inventory...</div></aside></section>
<section class="grid"><article class="metric"><small class="muted">Active placement inventory</small><b id="active-placements">-</b><p class="fine">Ready campaign destinations, subject to review.</p></article><article class="metric"><small class="muted">Subscriber memberships</small><b id="audience-total">-</b><p class="fine">Subscriptions summed across placements; never presented as unique reach.</p></article><article class="metric"><small class="muted">Public channels to explore</small><b id="public-placements">-</b><p class="fine">Browse the public part of the active network below.</p></article></section>
<section class="section" id="how"><div class="section-head"><div class="eyebrow">A transparent process</div><h2>Designed to make a promotion easy to understand and easy to verify.</h2></div><div class="steps"><article class="step"><span>1</span><h3>Browse the public network</h3><p class="muted">Explore active public channels and understand the scale of the available inventory.</p></article><article class="step"><span>2</span><h3>Plan with a manager</h3><p class="muted">The campaign manager confirms creative, timing, suitability and payment before anything is scheduled.</p></article><article class="step"><span>3</span><h3>Follow delivery live</h3><p class="muted">A private client link shows delivery progress, live highlights and public placements while the promotion runs.</p></article></div></section>
<section class="section" id="network"><div class="section-head"><div class="eyebrow">Explore the network</div><h2>Live public channel browse</h2><p class="muted">Channels are ordered by their current subscriber count. Private placements contribute to campaign inventory but remain intentionally private.</p></div><article class="card"><div id="directory" class="directory"></div><div id="directory-empty" class="notice">Public channels will appear here as the network inventory is refreshed.</div><div class="pager"><button id="directory-prev" class="button secondary" type="button">Previous</button><span class="fine" id="directory-summary"></span><button id="directory-next" class="button secondary" type="button">More channels</button></div></article></section>
<section class="section" id="contact"><article class="callout"><div><div class="eyebrow">Ready when you are</div><h2>Request a tailored promotion plan.</h2><p class="muted">Discuss the creative, preferred time, duration and reporting you need.</p></div><a id="contact-link" class="button" href="#contact">Contact campaign manager</a></article></section><footer>iHarvester reports aggregate network inventory. Subscriber memberships are not a promise of unique people, reach, conversions or sales.</footer></main><script>
let data={initial};const n=v=>Number(v||0).toLocaleString();const directory=document.getElementById('directory');
function renderInventory(){{const net=data.network;document.getElementById('hero-audience').textContent=net.subscriber_slots?n(net.subscriber_slots):'Building';document.getElementById('audience-total').textContent=net.subscriber_slots?n(net.subscriber_slots):'-';document.getElementById('active-placements').textContent=n(net.active_placements);document.getElementById('public-placements').textContent=n(net.public_placements);document.getElementById('hero-note').textContent=net.subscriber_slots?`${{n(net.known_audience_placements)}} placements have a current subscriber count. This is a sum of subscriber memberships, not a deduplicated audience or promised reach.`:'Placement inventory values appear as active channels report their current subscriber count.'}}
function renderDirectory(value){{const p=value||{{items:[]}},items=p.items||[];directory.innerHTML='';document.getElementById('directory-empty').style.display=items.length?'none':'block';for(const item of items){{const a=document.createElement('a');a.className='placement';a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';const title=document.createElement('b');title.textContent=item.label;const audience=document.createElement('small');audience.textContent=item.subscriber_slots===null?'Subscriber count not available':`${{n(item.subscriber_slots)}} subscriber memberships`;const open=document.createElement('span');open.className='open';open.textContent='Open channel →';a.append(title,audience,open);directory.appendChild(a)}}const from=p.total?Number(p.page||0)*Number(p.page_size||24)+1:0,to=Math.min(Number(p.total||0),(Number(p.page||0)+1)*Number(p.page_size||24));document.getElementById('directory-summary').textContent=p.total?`Showing ${{n(from)}}–${{n(to)}} of ${{n(p.total)}} public channels`:'No public channels available yet';document.getElementById('directory-prev').disabled=!p.page;document.getElementById('directory-next').disabled=!p.total||((Number(p.page||0)+1)*Number(p.page_size||24)>=Number(p.total||0))}}
async function loadDirectory(page){{try{{const r=await fetch(`${{data.placements_url}}?page=${{Math.max(0,page)}}`,{{cache:'no-store'}});if(!r.ok)throw Error();data={{...data,placements:await r.json()}};renderDirectory(data.placements)}}catch{{document.getElementById('directory-summary').textContent='Could not load more channels. Please try again.'}}}}
function contact(){{for(const id of ['contact-link','hero-contact']){{const a=document.getElementById(id);if(data.contact_url){{a.href=data.contact_url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='Contact campaign manager'}}}}}}
function render(){{renderInventory();renderDirectory(data.placements);contact()}}render();document.getElementById('directory-prev').addEventListener('click',()=>loadDirectory(Number(data.placements?.page||0)-1));document.getElementById('directory-next').addEventListener('click',()=>loadDirectory(Number(data.placements?.page||0)+1));setInterval(async()=>{{try{{const r=await fetch(data.insights_url,{{cache:'no-store'}});if(r.ok){{const previous=data.placements;data=await r.json();data.placements=previous;renderInventory();contact()}}}}catch{{}}}},60000);
</script></body></html>"""
