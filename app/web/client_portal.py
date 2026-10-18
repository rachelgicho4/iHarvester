"""Premium, dependency-free client portal for a shared promotion."""
# ruff: noqa: E501

from __future__ import annotations

from html import escape
from typing import Any

from app.utils.time import as_utc, utcnow


def _number(value: Any) -> int:
    return int(value or 0)


def _percent(value: int, total: int) -> int:
    return 0 if total <= 0 else max(0, min(100, round(100 * value / total)))


def _iso(value: Any) -> str | None:
    return as_utc(value).isoformat() if value else None


def _audience_payload(value: dict[str, Any] | None) -> dict[str, int]:
    source = value or {}
    return {
        "target_count": max(0, _number(source.get("target_channels"))),
        "known_count": max(0, _number(source.get("known_audience_channels"))),
        "subscriber_slots": max(0, _number(source.get("subscriber_slots"))),
        "unknown_count": max(0, _number(source.get("unknown_audience_channels"))),
        "public_count": max(0, _number(source.get("public_channels"))),
    }


def _view_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}
    return {
        "total": max(0, _number(source.get("total_views"))),
        "sampled_placements": max(0, _number(source.get("sampled_placements"))),
        "per_placement": max(0, _number(source.get("views_per_placement"))),
        "per_placement_hour": max(0.0, float(source.get("views_per_placement_hour") or 0)),
        "first_sample_at": _iso(source.get("first_sample_at")),
        "last_sample_at": _iso(source.get("last_sample_at")),
    }


def _benchmark_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value or {}
    hours: list[dict[str, Any]] = []
    for item in list(source.get("hourly") or []):
        try:
            hour = int(item.get("hour"))
        except (TypeError, ValueError):
            continue
        if not 0 <= hour <= 23:
            continue
        hours.append(
            {
                "hour": hour,
                "sample_campaigns": max(0, _number(item.get("sample_campaigns"))),
                "views_per_placement_hour": max(0.0, float(item.get("views_per_placement_hour") or 0)),
            }
        )
    return {
        "sampled_campaigns": max(0, _number(source.get("sampled_campaigns"))),
        "sampled_placements": max(0, _number(source.get("sampled_placements"))),
        "average_views_per_placement": max(0, _number(source.get("average_views_per_placement"))),
        "hourly": sorted(hours, key=lambda item: item["hour"]),
    }


def _placements_payload(value: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in value or []:
        label = str(item.get("label") or "Public placement").strip()
        url = str(item.get("url") or "").strip()
        if label and url.startswith("https://t.me/"):
            result.append({"label": label[:120], "url": url})
    return result[:60]


def campaign_progress_payload(
    campaign: dict[str, Any],
    totals: dict[str, Any],
    cycle_stats: dict[str, Any],
    metrics: dict[str, Any],
    *,
    live_posts: int,
    joined: int,
    audience: dict[str, Any] | None = None,
    views: dict[str, Any] | None = None,
    benchmarks: dict[str, Any] | None = None,
    public_placements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return client-safe aggregate promotion data.

    The audience total is explicitly a sum of subscriptions across placements,
    never a claim of deduplicated people.  View values are absent until the
    optional MTProto tracker has recorded real Telegram view counters.
    """
    pending = sum(_number(totals.get(status)) for status in ("PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED"))
    sent = _number(totals.get("SENT"))
    failed = _number(totals.get("FAILED_PERMANENT"))
    unknown = _number(totals.get("UNKNOWN_SEND_STATE"))
    cleaned = _number(totals.get("CLEANED"))
    cleanup_failed = _number(totals.get("CLEANUP_FAILED"))
    cancelled = _number(totals.get("CANCELLED"))
    delivery_total = sum(
        _number(totals.get(status))
        for status in ("PENDING", "PROCESSING", "RETRY_WAIT", "PAUSED", "SENT", "FAILED_PERMANENT", "UNKNOWN_SEND_STATE", "CLEANED", "CLEANUP_FAILED", "CANCELLED")
    )
    delivery_complete = delivery_total - pending
    start = campaign.get("start_at_utc")
    end = campaign.get("current_end_at_utc")
    now = as_utc(campaign.get("archived_at")) if campaign.get("archived_at") else utcnow()
    elapsed_percent = 0
    if start and end:
        total_seconds = max(1, int((as_utc(end) - as_utc(start)).total_seconds()))
        elapsed_seconds = max(0, min(int((now - as_utc(start)).total_seconds()), total_seconds))
        elapsed_percent = _percent(elapsed_seconds, total_seconds)
    status = str(campaign.get("status") or "UNKNOWN")
    return {
        "campaign": {
            "name": str(campaign.get("name") or "Promotion"),
            "status": status.lower().replace("_", " ").title(),
            "mode": str(campaign.get("mode") or "STANDARD").lower().replace("_", " ").title(),
            "start_at": _iso(start),
            "end_at": _iso(end),
            "updated_at": _iso(metrics.get("last_updated_at") or campaign.get("updated_at")),
        },
        "delivery": {
            "complete": delivery_complete,
            "total": delivery_total,
            "percent": _percent(delivery_complete, delivery_total),
            "sent": sent,
            "pending": pending,
            "failed": failed,
            "unknown": unknown,
            "cancelled": cancelled,
        },
        "timeline": {"percent": elapsed_percent, "status": status},
        "cycles": {"completed": _number(cycle_stats.get("completed")), "planned": _number(cycle_stats.get("planned"))},
        "cleanup": {"deleted": cleaned, "failed": cleanup_failed, "live_posts": max(0, int(live_posts))},
        "engagement": {"tracked_joins": max(0, int(joined))},
        "audience": _audience_payload(audience),
        "views": _view_payload(views),
        "benchmarks": _benchmark_payload(benchmarks),
        "placements": _placements_payload(public_placements),
    }


def render_client_portal(token: str, payload: dict[str, Any]) -> str:
    """Render a polished single-campaign client experience with live polling."""
    title = escape(str(payload["campaign"]["name"]))
    initial = _safe_json(payload)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#08111f"><title>{title} | iHarvester</title>
<style>
:root{{--ink:#edf4ff;--muted:#a5b2c7;--line:#26384d;--card:rgba(15,29,47,.86);--deep:#07111f;--emerald:#49e4a0;--cyan:#68b8ff;--gold:#f7c96b;--pink:#ff93c7}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at 16% 0,#173b59 0,transparent 34rem),radial-gradient(circle at 100% 18%,#24315e 0,transparent 32rem),var(--deep);color:var(--ink);font:16px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.45}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.34;background-image:linear-gradient(#ffffff08 1px,transparent 1px),linear-gradient(90deg,#ffffff08 1px,transparent 1px);background-size:34px 34px;mask-image:linear-gradient(to bottom,#000,transparent 65%)}}main{{position:relative;max-width:1040px;margin:auto;padding:34px 18px 64px}}.eyebrow{{font-size:12px;letter-spacing:.15em;text-transform:uppercase;color:#91add2;font-weight:800}}h1{{font-size:clamp(32px,7vw,56px);letter-spacing:-.045em;line-height:1.02;margin:9px 0 12px;max-width:850px}}h2{{font-size:19px;letter-spacing:-.02em;margin:0 0 8px}}h3{{font-size:14px;text-transform:uppercase;letter-spacing:.09em;color:#a7b8ce;margin:0 0 8px}}p{{margin:8px 0}}.subtle,.muted,small{{color:var(--muted)}}.hero{{padding:16px 0 22px;display:flex;justify-content:space-between;gap:18px;align-items:flex-start}}.badge{{display:inline-flex;align-items:center;gap:7px;border:1px solid #4be29c55;background:#15422f99;color:#c9ffe3;padding:7px 10px;border-radius:99px;font-size:13px;font-weight:750;white-space:nowrap}}.pulse{{width:8px;height:8px;background:var(--emerald);border-radius:50%;box-shadow:0 0 0 4px #49e4a022}}.layout{{display:grid;grid-template-columns:1.6fr 1fr;gap:16px}}.wide{{grid-column:1/-1}}.card{{background:linear-gradient(145deg,#162b44e8,#101d30e8);border:1px solid var(--line);box-shadow:0 18px 54px #0005;border-radius:22px;padding:21px;backdrop-filter:blur(10px)}}.accent{{border-color:#54e4a050;background:linear-gradient(135deg,#123d37e8,#142b45e8)}}.kpis{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px;margin-top:15px}}.kpi{{border:1px solid #ffffff14;border-radius:15px;padding:13px;background:#07132177}}.kpi b{{display:block;font-size:25px;letter-spacing:-.04em;margin-top:3px}}.kpi small{{font-size:12px}}.track{{height:11px;border-radius:999px;background:#06101d;overflow:hidden;margin:15px 0 7px;border:1px solid #ffffff0d}}.fill{{height:100%;border-radius:inherit;background:linear-gradient(90deg,#2bbd7b,var(--emerald));transition:width .45s ease}}.timefill{{background:linear-gradient(90deg,#e9a94e,var(--gold))}}.line{{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}}.line b{{font-size:15px}}.figure{{font-size:34px;line-height:1;letter-spacing:-.05em;margin:13px 0 5px;font-weight:800}}.mini{{font-size:13px;color:var(--muted)}}.trust{{margin-top:15px;padding:12px 13px;border-radius:13px;background:#0713218a;border:1px solid #ffffff12;font-size:13px;color:#bfcbdb}}.trust strong{{color:#e9f2ff}}.slot-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:15px 0 8px}}.slot{{padding:10px 8px;border:1px solid #ffffff15;border-radius:12px;background:#07132177;text-align:center}}.slot b{{display:block;color:#d6ffe9;font-size:13px}}.slot span{{display:block;color:var(--muted);font-size:11px;margin-top:2px}}.estimate{{margin-top:12px;border-left:3px solid var(--cyan);padding:8px 0 8px 12px;color:#cddcf0;font-size:14px}}label{{display:block;margin:14px 0 6px;font-size:13px;font-weight:750;color:#cad8eb}}input,textarea,select,button{{font:inherit}}input,textarea,select{{width:100%;padding:11px 12px;border-radius:12px;border:1px solid var(--line);background:#071321;color:var(--ink)}}textarea{{min-height:96px;resize:vertical}}button,.button{{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:12px;padding:11px 15px;background:linear-gradient(135deg,#42d995,#7bf0c0);color:#032116;font-weight:850;text-decoration:none;cursor:pointer;margin-top:15px}}button:hover,.button:hover{{filter:brightness(1.08)}}.notice{{display:none;margin-top:12px;padding:12px;border-radius:12px;background:#143a2c;color:#ddffea}}.proof{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:9px;margin-top:14px}}.proof a{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:11px 12px;border-radius:12px;color:#d9eaff;text-decoration:none;border:1px solid #ffffff16;background:#07132188}}.proof a:hover{{border-color:#68b8ff99;background:#0c2036}}.arrow{{color:var(--cyan);font-weight:900}}.quiet{{padding:13px;border:1px dashed #54708f;border-radius:13px;color:var(--muted);font-size:14px}}footer{{margin-top:24px;text-align:center;color:#8294ad;font-size:12px}}@media(max-width:720px){{main{{padding-top:22px}}.hero{{display:block}}.badge{{margin-top:8px}}.layout{{grid-template-columns:1fr}}.wide{{grid-column:auto}}.card{{padding:17px}}}}@media(max-width:380px){{.kpis{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header class="hero"><div><div class="eyebrow">iHarvester / campaign intelligence</div><h1 id="campaign-name">{title}</h1><p class="subtle" id="campaign-state">Live campaign information refreshes automatically.</p></div><div class="badge"><span class="pulse"></span><span id="live-badge">Live updates</span></div></header>
<section class="layout"><article class="card accent wide"><div class="line"><div><h2>Campaign delivery</h2><p class="muted">A clear, live view of delivery and campaign time.</p></div><b id="delivery-label">Loading...</b></div><div class="track"><div id="delivery-bar" class="fill"></div></div><p class="mini" id="delivery-detail"></p><div class="line" style="margin-top:17px"><b id="timeline-label">Campaign time</b><span class="mini" id="timeline-detail"></span></div><div class="track"><div id="timeline-bar" class="fill timefill"></div></div></article>
<article class="card"><h2>Audience exposure</h2><div class="figure" id="audience">-</div><p class="muted">Subscriber slots across this campaign's placements.</p><div class="trust" id="audience-note">Loading the verified audience inventory...</div></article>
<article class="card"><h2>Measured views</h2><div class="figure" id="views">-</div><p class="muted" id="views-label">Live post-view tracking is preparing.</p><div class="trust" id="views-note">Telegram view counters are sampled from live placements.</div></article>
<article class="card wide"><div class="line"><div><h2>Results at a glance</h2><p class="muted">Delivery, visibility and response signals update while you are here.</p></div><span class="eyebrow">Verified delivery</span></div><div class="kpis"><div class="kpi"><small>Delivered</small><b id="sent">-</b></div><div class="kpi"><small>Awaiting / processing</small><b id="pending">-</b></div><div class="kpi"><small>Delivery issues</small><b id="failed">-</b></div><div class="kpi"><small>Live placements</small><b id="live-posts">-</b></div><div class="kpi"><small>Cycles completed</small><b id="cycles">-</b></div><div class="kpi"><small>Tracked joins</small><b id="joins">-</b></div></div><p class="mini" id="updated">Updates while this page is open; no manual refresh is needed.</p></article>
<article class="card wide"><h2>When tends to perform best?</h2><p class="muted">Use measured results from earlier promotions to choose a time for the next run. Estimates remain indicative, not a guarantee.</p><div id="slot-grid" class="slot-grid"></div><label for="estimate-hour">Preferred start hour (your local time)</label><select id="estimate-hour"></select><label for="estimate-hours">How many hours should the ad remain live?</label><input id="estimate-hours" type="number" min="1" max="720" value="24"><div class="estimate" id="estimate-copy">View history will appear here as verified samples are collected.</div></article>
<article class="card wide" id="proof-card"><h2>See public placements</h2><p class="muted">Open any public placement to see the live ad in context. Private placements remain intentionally private.</p><div id="proof-list" class="proof"></div><div id="proof-empty" class="quiet">Public placement links appear here once a live post is available in a public channel.</div></article>
<article class="card wide"><h2>Book your next promotion</h2><p class="muted">Choose a preferred time or request a new promotion. Your campaign manager reviews payment and approves every request before anything is scheduled.</p><form id="request-form"><div class="layout"><div><label>Request type</label><select id="request-type"><option value="RERUN">Run this promotion again</option><option value="NEW">Request a new promotion</option></select></div><div><label>Your name or reference</label><input id="client-name" maxlength="100" required placeholder="For example: Acme Media"></div></div><label>Preferred start time</label><input id="desired-start" type="datetime-local"><small>Leave blank if you want the campaign manager to choose the most suitable slot.</small><label>Notes or material changes</label><textarea id="details" maxlength="3000" placeholder="Timing, duration, changes, or instructions"></textarea><button type="submit">Send request for approval</button></form><div id="notice" class="notice"></div></article></section>
<footer>Private campaign link. Subscriber slots and view totals are reported transparently; they are not a promise of unique people or conversions.</footer>
</main><script>
const token={_safe_json(token)};const progressUrl=`/client/c/${{encodeURIComponent(token)}}/progress`;const requestUrl=`/client/c/${{encodeURIComponent(token)}}/requests`;let current={initial};
const n=v=>Number(v||0).toLocaleString();const when=v=>v?new Date(v).toLocaleString():"Not scheduled";const hour=h=>new Intl.DateTimeFormat([],{{hour:'numeric'}}).format(new Date(2026,0,1,Number(h),0));
function slots(data){{const select=document.getElementById('estimate-hour');const old=select.value;select.innerHTML='';for(let i=0;i<24;i++){{const o=document.createElement('option');o.value=i;o.textContent=hour(i);select.appendChild(o)}}select.value=old||String(new Date().getHours());const measured=(data.benchmarks.hourly||[]).filter(item=>item.sample_campaigns>0&&item.views_per_placement_hour>0).sort((a,b)=>b.views_per_placement_hour-a.views_per_placement_hour).slice(0,3);const box=document.getElementById('slot-grid');box.innerHTML='';if(!measured.length){{box.innerHTML='<div class="quiet" style="grid-column:1/-1">No verified hourly view history yet. The first live tracking samples will build this recommendation.</div>';return}}for(const item of measured){{const card=document.createElement('div');card.className='slot';card.innerHTML=`<b>${{hour(item.hour)}}</b><span>${{n(item.views_per_placement_hour)}} views / placement / hr</span><span>${{n(item.sample_campaigns)}} campaign sample(s)</span>`;box.appendChild(card)}}}}
function estimate(data){{const selected=Number(document.getElementById('estimate-hour').value);const duration=Math.max(1,Number(document.getElementById('estimate-hours').value)||1);const row=(data.benchmarks.hourly||[]).find(item=>Number(item.hour)===selected&&item.sample_campaigns>0&&item.views_per_placement_hour>0);const copy=document.getElementById('estimate-copy');if(!row){{copy.textContent='There is not enough verified view history for this hour yet. You can still request it; your manager will review the best available slot.';return}}const projected=Math.round(row.views_per_placement_hour*duration*Math.max(1,Number(data.audience.target_count)||0));copy.textContent=`Indicative benchmark: about ${{n(row.views_per_placement_hour)}} measured views per placement per hour from ${{n(row.sample_campaigns)}} similar campaign sample(s). At ${{duration}} hour(s), that is roughly ${{n(projected)}} placement views across this campaign inventory, before any manager adjustments. Not a guarantee.`}}
function proof(data){{const list=document.getElementById('proof-list'),empty=document.getElementById('proof-empty');list.innerHTML='';const placements=data.placements||[];empty.style.display=placements.length?'none':'block';for(const item of placements){{const a=document.createElement('a');a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';a.innerHTML=`<span></span><span class="arrow">Open -></span>`;a.firstElementChild.textContent=item.label;list.appendChild(a)}}}}
function paint(data){{current=data;const c=data.campaign,d=data.delivery,t=data.timeline,cy=data.cycles,cl=data.cleanup,e=data.engagement,a=data.audience,v=data.views;document.title=`${{c.name}} | iHarvester`;document.getElementById('campaign-name').textContent=c.name;document.getElementById('campaign-state').textContent=`${{c.status}} | ${{c.mode}} | updates every 10 seconds`;document.getElementById('delivery-bar').style.width=`${{d.percent}}%`;document.getElementById('delivery-label').textContent=d.total?`${{d.percent}}% complete`:'Waiting for the first delivery cycle';document.getElementById('delivery-detail').textContent=`${{n(d.sent)}} delivered | ${{n(d.pending)}} in progress | ${{n(d.failed+d.unknown)}} issue(s)`;document.getElementById('timeline-bar').style.width=`${{t.percent}}%`;document.getElementById('timeline-label').textContent=`Campaign time: ${{t.percent}}%`;document.getElementById('timeline-detail').textContent=`${{when(c.start_at)}} - ${{when(c.end_at)}}`;document.getElementById('sent').textContent=n(d.sent);document.getElementById('pending').textContent=n(d.pending);document.getElementById('failed').textContent=n(d.failed+d.unknown);document.getElementById('live-posts').textContent=n(cl.live_posts);document.getElementById('cycles').textContent=`${{n(cy.completed)}} / ${{n(cy.planned)}}`;document.getElementById('joins').textContent=n(e.tracked_joins);document.getElementById('audience').textContent=a.subscriber_slots?n(a.subscriber_slots):'Building inventory';document.getElementById('audience-note').innerHTML=a.subscriber_slots?`<strong>${{n(a.known_count)}} of ${{n(a.target_count)}} placements</strong> have a current subscriber count. This is a combined subscription audience, not deduplicated people.${{a.unknown_count?` ${{n(a.unknown_count)}} placement(s) are not counted yet.`:''}}`:'The campaign target inventory is still being measured.';document.getElementById('views').textContent=v.sampled_placements?n(v.total):'Tracking starts soon';document.getElementById('views-label').textContent=v.sampled_placements?`${{n(v.sampled_placements)}} measured placement(s) | avg. ${{n(v.per_placement)}} views each`:'Telegram post views will appear after the first verified sample.';document.getElementById('views-note').textContent=v.sampled_placements?`Last sampled ${{when(v.last_sample_at)}}. View totals are post views, not unique people.`:'View tracking reads live post counters without creating extra exposure.';document.getElementById('updated').textContent=`Last campaign data update: ${{when(c.updated_at)}} | refreshes automatically every 10 seconds.`;slots(data);estimate(data);proof(data)}}
async function refresh(){{try{{const r=await fetch(progressUrl,{{cache:'no-store'}});if(!r.ok)throw Error();paint(await r.json());document.getElementById('live-badge').textContent='Live updates'}}catch{{document.getElementById('live-badge').textContent='Reconnecting...';document.getElementById('updated').textContent='Live updates are temporarily unavailable. Retrying automatically...'}}}}
paint(current);setInterval(refresh,10000);document.getElementById('estimate-hour').addEventListener('change',()=>estimate(current));document.getElementById('estimate-hours').addEventListener('input',()=>estimate(current));
document.getElementById('request-form').addEventListener('submit',async event=>{{event.preventDefault();const notice=document.getElementById('notice');notice.style.display='block';notice.textContent='Sending request...';const wanted=document.getElementById('desired-start').value;const body={{request_type:document.getElementById('request-type').value,client_name:document.getElementById('client-name').value,details:document.getElementById('details').value,desired_start:wanted||null,client_timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'}};try{{const r=await fetch(requestUrl,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});const out=await r.json();if(!r.ok)throw Error(out.detail||'Could not send request');notice.textContent=out.material_url?'Request recorded. Send your post materials through Telegram, then tap Done there so it reaches approval.':'Request recorded and waiting for payment and approval.';if(out.material_url){{const a=document.createElement('a');a.className='button';a.href=out.material_url;a.textContent='Send materials in Telegram';notice.appendChild(document.createElement('br'));notice.appendChild(a)}}event.target.reset()}}catch(error){{notice.textContent=error.message||'Could not send request. Please try again.'}}}});
</script></body></html>"""


def _safe_json(value: Any) -> str:
    """A tiny JSON encoder safe to embed inside a script tag."""
    import json

    return json.dumps(value, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
