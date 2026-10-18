"""Public aggregate-only media-kit page for prospective promotion clients."""
# ruff: noqa: E501

from __future__ import annotations

from typing import Any


def _number(value: Any) -> int:
    return max(0, int(value or 0))


def _safe_json(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def media_kit_payload(
    audience: dict[str, Any] | None,
    benchmarks: dict[str, Any] | None,
    *,
    contact_url: str | None = None,
    insights_url: str = "/promote/insights",
) -> dict[str, Any]:
    """Keep the public sales page aggregate-only and candid about coverage."""
    audience = audience or {}
    benchmarks = benchmarks or {}
    hourly: list[dict[str, Any]] = []
    for entry in list(benchmarks.get("hourly") or []):
        try:
            hour = int(entry.get("hour"))
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            hourly.append(
                {
                    "hour": hour,
                    "sample_campaigns": _number(entry.get("sample_campaigns")),
                    "views_per_placement_hour": max(0.0, float(entry.get("views_per_placement_hour") or 0)),
                }
            )
    return {
        "network": {
            "active_placements": _number(audience.get("active_channels")),
            "known_audience_placements": _number(audience.get("known_audience_channels")),
            "subscriber_slots": _number(audience.get("subscriber_slots")),
            "unknown_audience_placements": _number(audience.get("unknown_audience_channels")),
        },
        "benchmarks": {
            "sampled_campaigns": _number(benchmarks.get("sampled_campaigns")),
            "sampled_placements": _number(benchmarks.get("sampled_placements")),
            "average_views_per_placement": _number(benchmarks.get("average_views_per_placement")),
            "hourly": sorted(hourly, key=lambda item: item["hour"]),
        },
        "contact_url": contact_url if contact_url and (contact_url.startswith("https://t.me/") or contact_url.startswith("tg://user?id=")) else None,
        "insights_url": insights_url if insights_url.startswith("/promote") else "/promote/insights",
    }


def render_media_kit(payload: dict[str, Any]) -> str:
    """Render a premium, lightweight prospect page with no network leakage."""
    initial = _safe_json(payload)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#080e1a"><title>iHarvester | Campaign distribution</title>
<style>
:root{{--ink:#f0f6ff;--muted:#9eafc7;--line:#263752;--panel:#102138e8;--base:#080e1a;--green:#51e5aa;--blue:#79b9ff;--gold:#ffd172}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:radial-gradient(circle at 7% 4%,#164163 0,transparent 35rem),radial-gradient(circle at 92% 10%,#29315d 0,transparent 32rem),var(--base);color:var(--ink);font:16px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:linear-gradient(120deg,#fff0 35%,#ffffff09 50%,#fff0 65%);opacity:.35}}main{{max-width:1130px;margin:auto;padding:24px 18px 70px;position:relative}}nav{{display:flex;align-items:center;justify-content:space-between;gap:15px;margin-bottom:56px}}.brand{{font-weight:900;letter-spacing:-.04em;font-size:20px}}.brand i{{font-style:normal;color:var(--green)}}.navlink{{color:#c9daf0;text-decoration:none;font-size:14px;font-weight:700;border:1px solid #ffffff1f;border-radius:99px;padding:8px 13px}}.hero{{display:grid;grid-template-columns:1.4fr .8fr;gap:30px;align-items:center;padding:28px 0 60px}}.eyebrow{{color:#9cc8ff;text-transform:uppercase;letter-spacing:.14em;font-weight:850;font-size:12px}}h1{{font-size:clamp(42px,7vw,78px);line-height:.97;letter-spacing:-.065em;margin:14px 0 18px;max-width:730px}}h1 em{{font-style:normal;color:var(--green)}}h2{{font-size:28px;letter-spacing:-.04em;line-height:1.05;margin:0 0 10px}}h3{{font-size:15px;margin:0 0 5px}}p{{margin:8px 0}}.muted{{color:var(--muted)}}.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:25px}}.button{{display:inline-flex;align-items:center;justify-content:center;border-radius:12px;padding:12px 16px;font-weight:850;text-decoration:none;background:linear-gradient(135deg,#4ce0a5,#88f5c8);color:#052218}}.button.ghost{{background:#172b43;color:#e1edff;border:1px solid #ffffff1a}}.card{{background:linear-gradient(145deg,#183452e8,#0e1e32e8);border:1px solid var(--line);box-shadow:0 22px 60px #0006;border-radius:24px;padding:22px;backdrop-filter:blur(12px)}}.hero-card{{transform:rotate(2deg);border-color:#61eab460}}.hero-card>*{{transform:rotate(-2deg)}}.signal{{display:flex;align-items:center;gap:8px;color:#d4ffe9;font-size:13px;font-weight:800}}.dot{{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px #51e5aa22}}.big-number{{font-size:48px;font-weight:900;letter-spacing:-.06em;line-height:1;margin:20px 0 5px}}.rule{{height:1px;background:#ffffff1a;margin:18px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.metric{{padding:20px;border:1px solid #ffffff15;border-radius:19px;background:#09182a8c}}.metric b{{font-size:28px;display:block;letter-spacing:-.04em;margin-top:5px}}.section{{margin-top:70px}}.section-head{{max-width:720px;margin-bottom:22px}}.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.step{{padding:20px;border:1px solid var(--line);border-radius:18px;background:#0c1a2dd1}}.step span{{display:inline-grid;place-items:center;width:29px;height:29px;border-radius:50%;background:#1a4051;color:#adffe2;font-weight:900;margin-bottom:14px}}.planner{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}label{{display:block;margin:13px 0 6px;font-weight:750;font-size:13px;color:#caddf7}}select,input{{width:100%;padding:12px;border:1px solid var(--line);border-radius:11px;background:#071525;color:var(--ink);font:inherit}}.forecast{{background:linear-gradient(145deg,#123b35,#142442);border-color:#56e7ac55;min-height:245px}}.forecast-value{{font-size:44px;font-weight:900;letter-spacing:-.06em;margin:17px 0 3px}}.slots{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}}.slot{{padding:10px 7px;border-radius:11px;background:#071525a8;border:1px solid #ffffff13;text-align:center}}.slot b{{font-size:13px;display:block;color:#d9ffec}}.slot small{{font-size:11px;color:var(--muted)}}.fine{{color:#9cb0cc;font-size:12px}}.callout{{padding:20px;border-radius:20px;background:linear-gradient(105deg,#173d4a,#232957);border:1px solid #7bceff55;display:flex;justify-content:space-between;gap:20px;align-items:center}}footer{{margin-top:50px;text-align:center;color:#7f91aa;font-size:12px}}@media(max-width:760px){{nav{{margin-bottom:30px}}.hero,.planner{{grid-template-columns:1fr}}.hero-card{{transform:none}}.hero-card>*{{transform:none}}.grid,.steps{{grid-template-columns:1fr}}.section{{margin-top:48px}}.callout{{display:block}}.callout .button{{margin-top:14px}}}}
</style></head><body><main><nav><div class="brand">i<i>Harvester</i></div><a class="navlink" href="#plan">Plan a promotion</a></nav>
<section class="hero"><div><div class="eyebrow">Telegram campaign distribution</div><h1>Put your promotion in front of <em>measured</em> placements.</h1><p class="muted">Campaign planning, verified delivery and live reporting for promotions distributed across an established Telegram network.</p><div class="actions"><a id="hero-contact" class="button" href="#plan">Explore the campaign planner</a><a class="button ghost" href="#how">How it works</a></div></div><aside class="card hero-card"><div class="signal"><span class="dot"></span> Active distribution inventory</div><div id="hero-audience" class="big-number">-</div><p class="muted">subscriber memberships across active placements</p><div class="rule"></div><div class="fine" id="hero-note">Verifying the current network inventory...</div></aside></section>
<section class="grid"><article class="metric"><small class="muted">Active placement inventory</small><b id="active-placements">-</b><p class="fine">Ready campaign destinations, subject to review.</p></article><article class="metric"><small class="muted">Subscriber memberships</small><b id="audience-total">-</b><p class="fine">Subscriptions summed across placements; never presented as unique reach.</p></article><article class="metric"><small class="muted">Average measured post views</small><b id="average-views">Building</b><p class="fine" id="view-sample-note">Live view verification builds this benchmark.</p></article></section>
<section class="section" id="how"><div class="section-head"><div class="eyebrow">A transparent process</div><h2>Designed to make a promotion easy to understand and easy to verify.</h2></div><div class="steps"><article class="step"><span>1</span><h3>Plan around the right moment</h3><p class="muted">Use real campaign view history to compare available time windows before you request a run.</p></article><article class="step"><span>2</span><h3>Receive verified delivery</h3><p class="muted">The campaign manager confirms materials, targeting and payment before any promotion is scheduled.</p></article><article class="step"><span>3</span><h3>Watch it live</h3><p class="muted">A private client link shows delivery, live view totals and inspectable public placements while the promotion runs.</p></article></div></section>
<section class="section" id="plan"><div class="section-head"><div class="eyebrow">Timing intelligence</div><h2>Choose a time window with evidence, not guesswork.</h2><p class="muted">The calculator uses verified post-view samples from prior promotions. It never treats subscriber memberships as guaranteed views.</p></div><div class="planner"><article class="card"><h3>Explore a campaign window</h3><label for="slot">Preferred start hour (your local time)</label><select id="slot"></select><label for="duration">Campaign duration in hours</label><input id="duration" type="number" value="24" min="1" max="720"><div id="top-slots" class="slots"></div><p class="fine">Availability, final targeting and creative suitability are confirmed by the campaign manager.</p></article><article class="card forecast"><div class="eyebrow">Indicative post-view model</div><div id="forecast-number" class="forecast-value">Awaiting samples</div><p id="forecast-copy" class="muted">The first verified view samples will unlock a transparent timing benchmark.</p><div class="rule"></div><p class="fine">This is a planning estimate of placement post views. It is not a promise of unique reach, conversions, sales or engagement.</p></article></div></section>
<section class="section"><article class="callout"><div><div class="eyebrow">Ready when you are</div><h2>Request a tailored promotion plan.</h2><p class="muted">Discuss the creative, preferred time, duration and reporting you need.</p></div><a id="contact" class="button" href="#plan">Contact campaign manager</a></article></section><footer>iHarvester provides aggregate network reporting. Private placement identities remain protected.</footer></main><script>
let data={initial};const insightsUrl=data.insights_url||'/promote/insights';const n=v=>Number(v||0).toLocaleString();const hour=h=>new Intl.DateTimeFormat([],{{hour:'numeric'}}).format(new Date(2026,0,1,Number(h),0));
function topRows(){{return(data.benchmarks.hourly||[]).filter(x=>x.sample_campaigns>0&&x.views_per_placement_hour>0).sort((a,b)=>b.views_per_placement_hour-a.views_per_placement_hour)}}
function render(){{const net=data.network,b=data.benchmarks;document.getElementById('hero-audience').textContent=net.subscriber_slots?n(net.subscriber_slots):'Building';document.getElementById('audience-total').textContent=net.subscriber_slots?n(net.subscriber_slots):'-';document.getElementById('active-placements').textContent=n(net.active_placements);document.getElementById('hero-note').textContent=net.subscriber_slots?`${{n(net.known_audience_placements)}} placements have a current subscriber count. This is a sum of subscriber memberships, not a deduplicated audience or promised reach.`:'Placement inventory values appear as active channels report their current subscriber count.';document.getElementById('average-views').textContent=b.sampled_campaigns?n(b.average_views_per_placement):'Building';document.getElementById('view-sample-note').textContent=b.sampled_campaigns?`Measured across ${{n(b.sampled_campaigns)}} campaign(s) and ${{n(b.sampled_placements)}} placements.`:'Live view verification is being collected; no projected figure is shown yet.';const select=document.getElementById('slot'),old=select.value;select.innerHTML='';for(let h=0;h<24;h++){{const option=document.createElement('option');option.value=h;option.textContent=hour(h);select.appendChild(option)}}select.value=old||String(new Date().getHours());const best=topRows().slice(0,3),slots=document.getElementById('top-slots');slots.innerHTML=best.length?'':'<div class="fine" style="grid-column:1/-1">Hourly recommendations will appear after verified samples are available.</div>';for(const item of best){{const node=document.createElement('div');node.className='slot';node.innerHTML=`<b>${{hour(item.hour)}}</b><small>${{n(item.views_per_placement_hour)}} / placement / hr</small>`;slots.appendChild(node)}}forecast()}}
function forecast(){{const h=Number(document.getElementById('slot').value),hours=Math.max(1,Number(document.getElementById('duration').value)||1),row=topRows().find(x=>Number(x.hour)===h),net=data.network;const number=document.getElementById('forecast-number'),copy=document.getElementById('forecast-copy');if(!row){{number.textContent='Awaiting samples';copy.textContent='There is not enough verified view history for this start hour. Request it anyway and the campaign manager can recommend the strongest available window.';return}}const estimate=Math.round(row.views_per_placement_hour*hours*Math.max(1,net.active_placements));number.textContent=`~${{n(estimate)}} post views`;copy.textContent=`Planning model: ${{n(row.views_per_placement_hour)}} measured post views per placement per hour from ${{n(row.sample_campaigns)}} comparable campaign sample(s), across the current ${{n(net.active_placements)}}-placement inventory for ${{n(hours)}} hour(s).`}}
function contact(){{for(const id of ['contact','hero-contact']){{const a=document.getElementById(id);if(data.contact_url){{a.href=data.contact_url;a.target='_blank';a.rel='noopener noreferrer';a.textContent='Contact campaign manager'}}}}}}render();contact();document.getElementById('slot').addEventListener('change',forecast);document.getElementById('duration').addEventListener('input',forecast);setInterval(async()=>{{try{{const r=await fetch(insightsUrl,{{cache:'no-store'}});if(r.ok){{data=await r.json();render();contact()}}}}catch{{}}}},60000);
</script></body></html>"""
