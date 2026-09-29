"""ORCH cross-border e-commerce demo pages (v0.18.0, i18n v0.18.1/v0.18.2).

Registers the seven client-facing modules on the existing Operator
Console app. All data is SAMPLE data; every AI draft is a pending ORCH
task until a named human approves it in the Approval Inbox.
"""

from __future__ import annotations

from datetime import datetime
import secrets
import time

from flask import Blueprint, abort, redirect, request, session

import commerce_demo
import commerce_import
from commerce_demo import DemoError


bp = Blueprint("commerce", __name__)

_HOOKS = {}

DRAFT_MIN_INTERVAL_SECONDS = 3

MODULE_ROUTES = {
    "sales_hub": "/sales",
    "content_studio": "/content",
    "knowledge_base": "/knowledge",
    "lead_desk": "/leads",
    "campaign_engine": "/campaigns",
    "market_dashboard": "/market",
    "data_import": "/import",
}


def register(
    app,
    *,
    render_page,
    get_csrf_token,
    get_locale,
    ui_strings,
    format_short_time=None,
):
    _HOOKS.update(
        render_page=render_page,
        get_csrf_token=get_csrf_token,
        get_locale=get_locale,
        ui_strings=ui_strings,
        format_short_time=format_short_time,
    )
    app.add_template_filter(local_short_time, "local_time")
    app.register_blueprint(bp)


def local_short_time(value):
    """UTC ISO timestamps (``*_utc`` fields) shown in the box-local zone."""
    formatter = _HOOKS.get("format_short_time") or (lambda item: item)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return formatter(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return formatter(parsed.isoformat(timespec="seconds"))


def _t():
    return _HOOKS["ui_strings"](_HOOKS["get_locale"]())


def require_csrf():
    expected = _HOOKS["get_csrf_token"]()
    submitted = request.form.get("csrf_token", "")
    if (
        not submitted
        or len(submitted) != len(expected)
        or not secrets.compare_digest(expected, submitted)
    ):
        abort(400)


def _flash(kind, code, **extra):
    session["demo_flash"] = {"kind": kind, "code": code, **extra}


def _pop_flash():
    return session.pop("demo_flash", None)


def _remember_operator(value):
    value = (value or "").strip()
    if commerce_demo.OPERATOR_PATTERN.fullmatch(value):
        session["demo_operator"] = value


def _page(title_key, active, body, **context):
    t = _t()
    context.setdefault("flash", _pop_flash())
    context.setdefault("operator_name", session.get("demo_operator", ""))
    context.setdefault("meta", commerce_demo.load_sample_data()["_meta"])
    context.setdefault("ai_mode", commerce_demo.ai_mode())
    context.setdefault("imp", _import_banner())
    return _HOOKS["render_page"](
        t[title_key], active, COMMON_HEAD + body, **context
    )


def _import_banner():
    """Banner facts when imported store data is in use (v0.19.0)."""
    state = commerce_import.active_import()
    if not state:
        return None
    counts = state.get("counts") or {}
    return {
        "products": counts.get("products", 0),
        "orders": counts.get("orders", 0),
        "traffic": counts.get("traffic", 0),
        "time": local_short_time(state.get("imported_at_utc")),
    }


def _active_skus(data):
    """Imported products (sku dict shape) when active, else sample SKUs."""
    return commerce_import.imported_skus() or data["skus"]


COMMON_HEAD = """
  {% if imp %}
  <div class="sample-banner imported-banner" data-imported-banner>
    <strong>{{ t.imp_badge }}</strong>
    <span>{{ t.imp_banner.format(products=imp.products, orders=imp.orders, traffic=imp.traffic, time=imp.time) }}</span>
    <span class="sample-meta"><a href="/import">{{ t.nav_import }}</a></span>
  </div>
  {% else %}
  <div class="sample-banner" data-sample-banner>
    <strong>{{ t.demo_sample_badge }}</strong>
    <span>{{ t.demo_sample_banner }}</span>
    <span class="sample-meta">{{ meta.brand }} · {{ meta.target_market }} · {{ meta.version }}</span>
  </div>
  {% endif %}
  {% if flash %}
    <div class="demo-flash demo-flash-{{ flash.kind }}" role="status">
      {{ t.get('demo_msg_' ~ flash.code, t.get('demo_err_' ~ flash.code, t.demo_err_generic)) }}
      {% if flash.task_id %}
        · <a href="/inbox#{{ flash.task_id }}">{{ flash.task_id|short_id(14, 6) }}</a>
      {% endif %}
    </div>
  {% endif %}
"""

MODULE_HEAD = """
  <div class="module-hero">
    <div>
      <p class="chat-eyebrow">{{ t.demo_eyebrow }}</p>
      <h2>{{ heading }}</h2>
      <p class="subtitle">{{ role }}</p>
    </div>
    <div class="module-ai">
      <span class="metric-label">{{ t.demo_ai_assist }}</span>
      <span>{{ ai }}</span>
      <span class="ai-mode-chip">{{ t.demo_ai_mode }}: {{ ai_mode }}</span>
    </div>
  </div>
  <p class="demo-positioning">{{ t.demo_positioning }}</p>
"""

OPERATOR_FIELDS = """
  <label class="demo-field">
    <span>{{ t.demo_operator }}</span>
    <input type="text" name="operator" required minlength="2" maxlength="40"
           value="{{ operator_name }}" placeholder="{{ t.demo_operator_ph }}">
  </label>
"""

LANGUAGE_FIELD = """
  <label class="demo-field">
    <span>{{ t.demo_language }}</span>
    <select name="language">
      <option value="zh-Hant" {% if locale.startswith('zh') %}selected{% endif %}>{{ t.demo_lang_zh_hant }}</option>
      <option value="en" {% if not locale.startswith('zh') %}selected{% endif %}>{{ t.demo_lang_en }}</option>
    </select>
  </label>
"""

DRAFT_SUBMIT = """
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button type="submit">{{ t.demo_draft_button }}</button>
  <p class="composer-help">{{ t.demo_draft_help }}</p>
"""

def sample_note(what_key):
    """Per-section label: still SAMPLE data while an import is in use."""
    return (
        "{% if imp %}<p class=\"sample-note\" data-sample-note>"
        "{{ t.imp_sample_section.format(what=t." + what_key + ") }}</p>{% endif %}"
    )


DRAFT_LIST = """
  <div class="section">
    <h3>{{ t.demo_recent_drafts }}</h3>
    {% if drafts %}
      <div class="table-wrap"><table>
        <tr><th>{{ t.th_task }}</th><th>{{ t.th_status }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_risk }}</th><th>{{ t.demo_th_created }}</th></tr>
        {% for d in drafts %}
          <tr>
            <td><a href="/inbox#{{ d.id }}">{{ d.title }}</a></td>
            <td><span class="badge {{ d.status }}">{{ d.status|status_label }}</span></td>
            <td>v{{ d.version }}</td>
            <td>{% for r in d.risk_tags %}<span class="risk-tag risk-{{ r }}">{{ t.get('risk_' ~ r, r) }}</span>{% endfor %}</td>
            <td>{{ d.created_by }} · {{ d.created_at_utc|local_time }}</td>
          </tr>
        {% endfor %}
      </table></div>
    {% else %}
      <div class="empty">{{ t.demo_no_drafts }}</div>
    {% endif %}
  </div>
"""


def _drafts_for(*kinds):
    return [d for d in commerce_demo.draft_views(locale=_HOOKS["get_locale"]()) if d["kind"] in kinds][:10]


def _module_context(key):
    t = _t()
    return {
        "heading": t[f"mod_{key}_title"],
        "role": t[f"mod_{key}_role"],
        "ai": t[f"mod_{key}_ai"],
    }


# ---------------------------------------------------------------------------
# Module pages
# ---------------------------------------------------------------------------

@bp.get("/sales")
def sales_hub():
    data = commerce_demo.load_sample_data()
    skus = commerce_demo.sku_index(data)
    active_skus = _active_skus(data)
    leads = [
        {**lead, "sku_obj": skus[lead["sku"]]} for lead in data["order_leads"]
    ]
    open_leads = [lead for lead in leads if lead["stage"] not in {"won", "lost"}]
    body = MODULE_HEAD + """
  <div class="grid">
    <div class="card"><span class="metric-label">{{ t.demo_metric_skus }}</span><span class="metric-value">{{ skus|length }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_metric_inquiries }}</span><span class="metric-value">{{ inquiries|length }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_metric_open_leads }}</span><span class="metric-value">{{ open_leads|length }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_metric_pipeline }}</span><span class="metric-value">HK${{ '{:,}'.format(pipeline) }}</span></div>
  </div>

  <div class="section">
    <h3>{{ t.demo_sales_draft_title }}</h3>
    """ + sample_note("imp_what_leads") + """
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="sales_next_step">
      <label class="demo-field"><span>{{ t.demo_lead }}</span>
        <select name="lead_id">
          {% for l in leads %}<option value="{{ l.id }}">{{ l.id }} · {{ l.sku }} · {{ t.get('stage_' ~ l.stage, l.stage) }}</option>{% endfor %}
        </select>
      </label>
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>

  <div class="section">
    <h3>{{ t.demo_order_leads }}</h3>
    """ + sample_note("imp_what_leads") + """
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_id }}</th><th>{{ t.demo_th_sku }}</th><th>{{ t.demo_th_qty }}</th><th>{{ t.demo_th_stage }}</th><th>{{ t.demo_th_value }}</th><th>{{ t.demo_th_inquiry }}</th></tr>
      {% for l in leads %}
        <tr>
          <td>{{ l.id }}</td>
          <td>{{ l.sku }} · {{ l.sku_obj.name_zh if locale.startswith('zh') else l.sku_obj.name_en }}</td>
          <td>{{ l.qty }}</td>
          <td><span class="badge todo">{{ t.get('stage_' ~ l.stage, l.stage) }}</span></td>
          <td>HK${{ '{:,}'.format(l.est_value_hkd) }}</td>
          <td><a href="/leads#{{ l.inquiry_id }}">{{ l.inquiry_id }}</a></td>
        </tr>
      {% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{% if imp %}{{ t.imp_products_from_import }}{% else %}{{ t.demo_products }}{% endif %} ({{ skus|length }})</h3>
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_sku }}</th><th>{{ t.demo_th_name }}</th><th>{{ t.demo_th_category }}</th><th>{{ t.demo_th_price }}</th><th>{{ t.demo_th_stock }}</th></tr>
      {% for s in skus %}
        <tr>
          <td>{{ s.sku }}</td>
          <td>{{ s.name_zh if locale.startswith('zh') else s.name_en }}</td>
          <td>{{ t.get('cat_' ~ s.category, s.category) }}</td>
          <td>HK${{ s.list_price_hkd }}</td>
          <td>{{ s.stock_units }}</td>
        </tr>
      {% endfor %}
    </table></div>
  </div>
""" + DRAFT_LIST
    pipeline = sum(lead["est_value_hkd"] for lead in open_leads)
    return _page(
        "mod_sales_hub_title", "sales", body,
        skus=active_skus, inquiries=data["inquiries"], leads=leads,
        open_leads=open_leads, pipeline=pipeline,
        drafts=_drafts_for("sales_next_step"),
        **_module_context("sales_hub"),
    )


@bp.get("/content")
def content_studio():
    data = commerce_demo.load_sample_data()
    body = MODULE_HEAD + """
  <div class="section">
    <h3>{{ t.demo_content_draft_title }}</h3>
    {% if imp %}<p class="composer-help">{{ t.imp_products_from_import }} ({{ skus|length }})</p>{% endif %}
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="content">
      <label class="demo-field"><span>{{ t.demo_th_sku }}</span>
        <select name="sku">
          {% for s in skus %}<option value="{{ s.sku }}">{{ s.sku }} · {{ s.name_zh if locale.startswith('zh') else s.name_en }}</option>{% endfor %}
        </select>
      </label>
      <label class="demo-field"><span>{{ t.demo_content_type }}</span>
        <select name="content_type">
          {% for c in content_types %}<option value="{{ c }}">{{ t.get('ctype_' ~ c, c) }}</option>{% endfor %}
        </select>
      </label>
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>
""" + DRAFT_LIST
    return _page(
        "mod_content_studio_title", "content", body,
        skus=_active_skus(data), content_types=commerce_demo.CONTENT_TYPES,
        drafts=_drafts_for("content"),
        **_module_context("content_studio"),
    )


@bp.get("/knowledge")
def knowledge_base():
    data = commerce_demo.load_sample_data()
    entries = commerce_demo.kb_entries(data)
    body = MODULE_HEAD + """
  <div class="section">
    <h3>{{ t.demo_kb_policies }}</h3>
    """ + sample_note("imp_what_kb") + """
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_id }}</th><th>{{ t.demo_th_section }}</th><th>{{ t.demo_th_entry }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_approved_by }}</th></tr>
      {% for e in entries %}
        <tr>
          <td>{{ e.id }}</td>
          <td>{{ t.get('kbsec_' ~ e.section, e.section) }}</td>
          <td><strong>{{ e.title_zh if locale.startswith('zh') else e.title_en }}</strong><br>
              <span class="muted">{{ e.body_zh if locale.startswith('zh') else e.body_en }}</span></td>
          <td>v{{ e.version }}</td>
          <td>{{ e.approved_by }}</td>
        </tr>
      {% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{{ t.demo_kb_approved_updates }}</h3>
    {% if kb_updates %}
      <div class="table-wrap"><table>
        <tr><th>{{ t.demo_th_entry }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_approver }}</th><th>{{ t.demo_th_time }}</th><th>{{ t.demo_th_audit }}</th></tr>
        {% for a in kb_updates %}
          <tr><td>{{ a.source.refs|join(', ') }}</td><td>v{{ a.version }}</td><td>{{ a.approver }}</td><td>{{ a.decided_at_utc|local_time }}</td><td><code>{{ a.audit_artifact_id|short_id(18, 6) }}</code></td></tr>
        {% endfor %}
      </table></div>
    {% else %}
      <div class="empty">{{ t.demo_kb_no_updates }}</div>
    {% endif %}
  </div>

  <div class="section">
    <h3>{{ t.demo_kb_propose_title }}</h3>
    """ + sample_note("imp_what_kb") + """
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="kb_update">
      <label class="demo-field"><span>{{ t.demo_th_entry }}</span>
        <select name="kb_id">{% for e in entries %}<option value="{{ e.id }}">{{ e.id }} · {{ e.title_zh if locale.startswith('zh') else e.title_en }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field demo-field-wide"><span>{{ t.demo_kb_proposal }}</span>
        <textarea name="proposal" rows="3" maxlength="400" required placeholder="{{ t.demo_kb_proposal_ph }}"></textarea>
      </label>
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>

  <div class="section">
    <h3>{{ t.demo_kb_specs }} ({{ skus|length }})</h3>
    """ + sample_note("imp_what_kb") + """
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_sku }}</th><th>{{ t.demo_th_name }}</th><th>{{ t.demo_th_facts }}</th><th>{{ t.demo_th_claims }}</th></tr>
      {% for s in skus %}
        <tr><td>{{ s.sku }}</td><td>{{ s.name_zh if locale.startswith('zh') else s.name_en }}</td><td>{{ s.approved_facts|join(' · ') }}</td><td class="muted">{{ s.claims_policy }}</td></tr>
      {% endfor %}
    </table></div>
  </div>
""" + DRAFT_LIST
    return _page(
        "mod_knowledge_base_title", "knowledge", body,
        entries=entries, skus=data["skus"],
        kb_updates=commerce_demo.approved_kb_updates(),
        drafts=_drafts_for("kb_update"),
        **_module_context("knowledge_base"),
    )


@bp.get("/leads")
def lead_desk():
    rows = commerce_demo.ranked_inquiries()
    body = MODULE_HEAD + """
  <div class="section">
    <h3>{{ t.demo_lead_draft_title }}</h3>
    """ + sample_note("imp_what_inquiries") + """
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="lead_reply">
      <label class="demo-field"><span>{{ t.demo_th_inquiry }}</span>
        <select name="inquiry_id">{% for r in rows %}<option value="{{ r.id }}">{{ r.id }} · {{ r.triage.score }} · {{ t.get('icat_' ~ r.triage.category, r.triage.category) }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field">
        <span>{{ t.demo_language }}</span>
        <select name="language">
          <option value="auto" selected>{{ t.demo_lang_auto }}</option>
          <option value="zh-Hant">{{ t.demo_lang_zh_hant }}</option>
          <option value="en">{{ t.demo_lang_en }}</option>
        </select>
      </label>
      """ + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>

  <div class="section">
    <h3>{{ t.demo_inquiry_triage }}</h3>
    """ + sample_note("imp_what_inquiries") + """
    <p class="composer-help">{{ t.demo_triage_note }}</p>
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_score }}</th><th>{{ t.demo_th_id }}</th><th>{{ t.demo_th_category }}</th><th>{{ t.demo_th_channel }}</th><th>{{ t.demo_th_customer }}</th><th>{{ t.demo_th_message }}</th></tr>
      {% for r in rows %}
        <tr id="{{ r.id }}">
          <td><span class="badge {{ 'waiting_approval' if r.triage.priority == 'high' else ('running' if r.triage.priority == 'medium' else 'todo') }}">{{ r.triage.score }} · {{ t.get('prio_' ~ r.triage.priority, r.triage.priority) }}</span></td>
          <td>{{ r.id }}</td>
          <td>{{ t.get('icat_' ~ r.triage.category, r.triage.category) }}{% if r.triage.high_risk %} <span class="risk-tag risk-price">{{ t.demo_needs_human }}</span>{% endif %}</td>
          <td>{{ t.get('ch_' ~ r.channel, r.channel) }}</td>
          <td>{{ r.customer }}<br><span class="muted">{{ r.sku }}</span></td>
          <td>{{ r.text }}</td>
        </tr>
      {% endfor %}
    </table></div>
  </div>
""" + DRAFT_LIST
    return _page(
        "mod_lead_desk_title", "leads", body,
        rows=rows, drafts=_drafts_for("lead_reply"),
        **_module_context("lead_desk"),
    )


IMPORT_UNMATCHED_WARNING = """
  {% if m.unmatched_order_lines %}<p class="sample-note import-warning" data-unmatched-orders="{{ m.unmatched_order_lines }}">{{ t.imp_warn_unmatched_orders.format(n=m.unmatched_order_lines) }}</p>{% endif %}
"""

IMPORT_METRICS_SALES = IMPORT_UNMATCHED_WARNING + """
  <div class="section">
    <h3>{{ t.imp_sales_by_sku }}</h3>
    {% if m.sales_by_sku %}
    <div class="table-wrap"><table data-imp-sales>
      <tr><th>{{ t.imp_th_sku }}</th><th>{{ t.imp_th_name }}</th><th>{{ t.imp_th_units }}</th><th>{{ t.imp_th_orders }}</th><th>{{ t.imp_th_revenue }}</th><th>{{ t.imp_th_share }}</th></tr>
      {% for r in m.sales_by_sku[:15] %}
        <tr><td>{{ r.sku }}</td><td>{{ r.name }}</td><td>{{ r.units }}</td><td>{{ r.orders }}</td><td>HK${{ '{:,.2f}'.format(r.revenue) }}</td><td>{{ r.share_pct }}%</td></tr>
      {% endfor %}
    </table></div>
    {% else %}<div class="empty">{{ t.imp_no_orders }}</div>{% endif %}
  </div>
"""

IMPORT_METRICS_COVER = """
  <div class="section">
    <h3>{{ t.imp_stock_cover }}</h3>
    <p class="composer-help">{{ t.imp_stock_cover_note.format(days=m.velocity_window_days, low=m.low_cover_days) }}</p>
    <div class="table-wrap"><table data-imp-cover>
      <tr><th>{{ t.imp_th_sku }}</th><th>{{ t.imp_th_name }}</th><th>{{ t.imp_th_stock }}</th><th>{{ t.imp_th_velocity }}</th><th>{{ t.imp_th_cover }}</th><th>{{ t.imp_th_status }}</th></tr>
      {% for r in m.stock_cover[:15] %}
        <tr>
          <td>{{ r.sku }}</td><td>{{ r.name }}</td><td>{{ r.stock }}</td><td>{{ r.velocity_per_day }}</td>
          <td>{{ r.days_of_cover if r.days_of_cover is not none else '—' }}</td>
          <td><span class="badge {{ 'failed' if r.status == 'out' else ('waiting_approval' if r.status == 'low' else 'done') }}">{{ t['imp_cover_' ~ r.status] }}</span></td>
        </tr>
      {% endfor %}
    </table></div>
  </div>
"""

IMPORT_METRICS_TRAFFIC = """
  <div class="section">
    <h3>{{ t.imp_traffic_by_source }}</h3>
    {% if m.traffic_by_source %}
    <div class="table-wrap"><table data-imp-traffic>
      <tr><th>{{ t.imp_th_source }}</th><th>{{ t.imp_th_pageviews }}</th><th></th><th>{{ t.imp_th_share }}</th></tr>
      {% for r in m.traffic_by_source %}
        <tr><td>{{ r.source }}</td><td>{{ '{:,}'.format(r.pageviews) }}</td>
          <td style="width:30%"><div class="kpi-bar"><span style="width: {{ r.share_pct }}%"></span></div></td>
          <td>{{ r.share_pct }}%</td></tr>
      {% endfor %}
    </table></div>
    {% else %}<div class="empty">{{ t.imp_no_traffic }}</div>{% endif %}
  </div>
"""


@bp.get("/campaigns")
def campaign_engine():
    data = commerce_demo.load_sample_data()
    metrics = commerce_import.compute_metrics()
    body = MODULE_HEAD + """
  {% if m %}
  <div class="section">
    <h3>{{ t.imp_campaign_title }}</h3>
    <form method="post" action="/demo/draft" class="demo-form" data-import-campaign-form>
      <input type="hidden" name="kind" value="import_campaign">
      <label class="demo-field"><span>{{ t.demo_th_sku }}</span>
        <select name="sku">{% for s in skus %}<option value="{{ s.sku }}">{{ s.sku }} · {{ s.name_en }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field"><span>{{ t.demo_objective }}</span>
        <select name="objective">{% for o in objectives %}<option value="{{ o }}">{{ t.get('obj_' ~ o, o) }}</option>{% endfor %}</select>
      </label>
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
    <p class="composer-help">{{ t.imp_insight_help }}</p>
    """ + sample_note("imp_what_audiences") + """
  </div>
  """ + IMPORT_METRICS_SALES + IMPORT_METRICS_COVER + IMPORT_METRICS_TRAFFIC + """
  {% else %}
  <div class="section">
    <h3>{{ t.demo_campaign_draft_title }}</h3>
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="campaign">
      <label class="demo-field"><span>{{ t.demo_th_sku }}</span>
        <select name="sku">{% for s in skus %}<option value="{{ s.sku }}">{{ s.sku }} · {{ s.name_zh if locale.startswith('zh') else s.name_en }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field"><span>{{ t.demo_audience }}</span>
        <select name="audience_id">{% for a in audiences %}<option value="{{ a.id }}">{{ a.name_zh if locale.startswith('zh') else a.name_en }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field"><span>{{ t.demo_objective }}</span>
        <select name="objective">{% for o in objectives %}<option value="{{ o }}">{{ t.get('obj_' ~ o, o) }}</option>{% endfor %}</select>
      </label>
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>
  {% endif %}
""" + DRAFT_LIST
    return _page(
        "mod_campaign_engine_title", "campaigns", body,
        m=metrics,
        skus=_active_skus(data), audiences=data["campaign"]["audiences"],
        objectives=commerce_demo.CAMPAIGN_OBJECTIVES,
        drafts=_drafts_for("campaign", "import_campaign"),
        **_module_context("campaign_engine"),
    )


@bp.get("/market")
def market_dashboard():
    metrics = commerce_import.compute_metrics()
    summary = None if metrics else commerce_demo.kpi_summary()
    body = MODULE_HEAD + """
  {% if m %}
  <h3 data-imp-metrics>{{ t.imp_metrics_title }}</h3>
  """ + IMPORT_UNMATCHED_WARNING + """
  {% if m.order_period %}<p class="composer-help">{{ t.imp_period.format(start=m.order_period[0], end=m.order_period[1]) }}</p>{% endif %}
  <div class="grid">
    <div class="card"><span class="metric-label">{{ t.imp_kpi_revenue }}</span><span class="metric-value">HK${{ '{:,.0f}'.format(m.totals.revenue) }}</span></div>
    <div class="card"><span class="metric-label">{{ t.imp_kpi_orders }}</span><span class="metric-value">{{ m.totals.orders }}</span></div>
    <div class="card"><span class="metric-label">{{ t.imp_kpi_units }}</span><span class="metric-value">{{ m.totals.units }}</span></div>
    <div class="card"><span class="metric-label">{{ t.imp_kpi_aov }}</span><span class="metric-value">HK${{ '{:,.0f}'.format(m.totals.aov) }}</span></div>
    <div class="card"><span class="metric-label">{{ t.imp_kpi_pageviews }}</span><span class="metric-value">{{ '{:,}'.format(m.totals.pageviews) }}</span></div>
    <div class="card"><span class="metric-label">{{ t.imp_kpi_conversion }}</span><span class="metric-value">{{ m.conversion.rate_pct ~ '%' if m.conversion else '—' }}</span></div>
  </div>
  <p class="composer-help" data-conversion-assumption>{% if m.conversion %}{{ t.imp_conversion_assumption.format(start=m.conversion.start, end=m.conversion.end, orders=m.conversion.orders, pageviews='{:,}'.format(m.conversion.pageviews)) }}{% else %}{{ t.imp_conversion_na }}{% endif %}</p>

  <div class="section">
    <h3>{{ t.imp_insight_title }}</h3>
    <form method="post" action="/demo/draft" class="demo-form" data-import-insight-form>
      <input type="hidden" name="kind" value="import_insight">
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
    <p class="composer-help">{{ t.imp_insight_help }}</p>
  </div>

  <div class="section">
    <h3>{{ t.imp_revenue_by_week }}</h3>
    {% if m.revenue_by_week %}
    <div class="table-wrap"><table data-imp-weeks>
      <tr><th>{{ t.imp_th_week }}</th><th>{{ t.imp_th_revenue }}</th><th></th><th>{{ t.imp_th_orders }}</th><th>{{ t.imp_th_units }}</th></tr>
      {% for r in m.revenue_by_week %}
        <tr><td>{{ r.week }} · {{ r.week_start }}</td><td>HK${{ '{:,.2f}'.format(r.revenue) }}</td>
          <td style="width:30%"><div class="kpi-bar"><span style="width: {{ r.bar_pct }}%"></span></div></td>
          <td>{{ r.orders }}</td><td>{{ r.units }}</td></tr>
      {% endfor %}
    </table></div>
    {% else %}<div class="empty">{{ t.imp_no_orders }}</div>{% endif %}
  </div>
  """ + IMPORT_METRICS_SALES + IMPORT_METRICS_COVER + IMPORT_METRICS_TRAFFIC + """
  <p class="sample-note">{{ t.imp_sample_hidden_note }} {{ t.imp_sample_section.format(what=t.imp_what_inquiries) }}</p>
  {% else %}
  <div class="grid">
    <div class="card"><span class="metric-label">{{ t.demo_kpi_sessions }}</span><span class="metric-value">{{ '{:,}'.format(k.totals.sessions) }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_kpi_inquiries }}</span><span class="metric-value">{{ k.totals.inquiries }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_kpi_leads }}</span><span class="metric-value">{{ k.totals.leads }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_kpi_orders }}</span><span class="metric-value">{{ k.totals.orders }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_kpi_order_rate }}</span><span class="metric-value">{{ k.overall_order_rate }}%</span></div>
  </div>
  <p class="composer-help">{{ t.demo_kpi_note }}</p>

  <div class="section">
    <h3>{{ t.demo_kpi_weekly }}</h3>
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_week }}</th><th>{{ t.demo_kpi_sessions }}</th><th></th><th>{{ t.demo_kpi_inquiries }}</th><th>{{ t.demo_kpi_leads }}</th><th>{{ t.demo_kpi_orders }}</th><th>{{ t.demo_kpi_funnel }}</th></tr>
      {% for r in k.rows %}
        <tr>
          <td>{{ r.week }}</td><td>{{ '{:,}'.format(r.sessions) }}</td>
          <td style="width:30%"><div class="kpi-bar"><span style="width: {{ r.bar_pct }}%"></span></div></td>
          <td>{{ r.inquiries }}</td><td>{{ r.leads }}</td><td>{{ r.orders }}</td>
          <td class="muted">{{ r.inquiry_rate }}% → {{ r.lead_rate }}% → {{ r.order_rate }}%</td>
        </tr>
      {% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{{ t.demo_kpi_by_channel }}</h3>
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_channel }}</th><th>{{ t.demo_kpi_inquiries }}</th></tr>
      {% for c in k.by_channel %}<tr><td>{{ t.get('ch_' ~ c.channel, c.channel) }}</td><td>{{ c.inquiries }}</td></tr>{% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{{ t.demo_insight_draft_title }}</h3>
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="market_insight">
      """ + LANGUAGE_FIELD + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>
  {% endif %}
""" + DRAFT_LIST
    return _page(
        "mod_market_dashboard_title", "market", body,
        m=metrics, k=summary,
        drafts=_drafts_for("market_insight", "import_insight"),
        **_module_context("market_dashboard"),
    )


# ---------------------------------------------------------------------------
# v0.19.0: /import (CSV upload, folder import, toggle, reset)
# ---------------------------------------------------------------------------

IMPORT_BODY = """
  <div class="section">
    <h3>{{ t.imp_status_title }}</h3>
    {% if state and state.data %}
      <p data-import-status="{{ 'active' if state.active else 'inactive' }}"><strong>{{ t.imp_status_active if state.active else t.imp_status_inactive }}</strong></p>
      <p class="composer-help">{{ t.imp_status_detail.format(time=state.imported_at_utc|local_time, products=state.counts.products, orders=state.counts.orders, traffic=state.counts.traffic) }}</p>
      {% if unmatched_orders %}<p class="sample-note import-warning" data-unmatched-orders="{{ unmatched_orders }}">{{ t.imp_warn_unmatched_orders.format(n=unmatched_orders) }}</p>{% endif %}
      <div class="inbox-actions">
        <form method="post" action="/import/toggle" class="demo-form">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="active" value="{{ '0' if state.active else '1' }}">
          <button type="submit" data-import-toggle>{{ t.imp_toggle_off if state.active else t.imp_toggle_on }}</button>
        </form>
        <form method="post" action="/import/reset" class="demo-form import-reset-form" data-confirm="{{ t.imp_reset_confirm }}" onsubmit="return window.confirm(this.getAttribute('data-confirm'));">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <button type="submit" class="btn-reject" data-import-reset>{{ t.imp_reset_button }}</button>
          <span class="composer-help import-hint">{{ t.imp_reset_help }}</span>
        </form>
      </div>
    {% else %}
      <p data-import-status="none">{{ t.imp_status_none }}</p>
    {% endif %}
  </div>

  <div class="section">
    <h3>{{ t.imp_heading }}</h3>
    <p class="composer-help">{{ t.imp_intro }}</p>
    <h4>{{ t.imp_schema_title }}</h4>
    <div class="table-wrap"><table>
      {% for kind, cols in schemas %}
        <tr><td>{{ t['imp_file_' ~ kind] }}</td><td><code>{{ cols|join(',') }}</code></td></tr>
      {% endfor %}
    </table></div>
    <ul class="composer-help import-rules" data-import-rules>
      <li data-rule-merge>{{ t.imp_rule_merge }}</li>
      <li data-rule-encoding>{{ t.imp_rule_encoding }}</li>
      <li data-rule-products-only>{{ t.imp_rule_products_only }}</li>
    </ul>
  </div>

  <div class="section">
    <h3>{{ t.imp_upload_title }}</h3>
    <form method="post" action="/import" enctype="multipart/form-data" class="demo-form">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      {% for kind, cols in schemas %}
        <label class="demo-field"><span>{{ t['imp_file_' ~ kind] }}</span>
          <input type="file" name="{{ kind }}" accept=".csv,text/csv"></label>
      {% endfor %}
      <button type="submit">{{ t.imp_upload_button }}</button>
      <p class="composer-help">{{ t.imp_upload_help.format(max_mb=max_mb) }}</p>
    </form>
  </div>

  <div class="section">
    <h3>{{ t.imp_folder_title }}</h3>
    <form method="post" action="/import/folder" class="demo-form">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button type="submit">{{ t.imp_folder_button }}</button>
      <span class="composer-help import-hint">{{ t.imp_folder_help }} · <code>{{ import_dir }}</code></span>
    </form>
  </div>

  <div class="section" data-import-report>
    <h3>{{ t.imp_report_title }}</h3>
    {% if report %}
      <p class="composer-help">{{ t.imp_report_meta.format(time=report.imported_at_utc|local_time, source=t.get('imp_source_' ~ report.source, report.source)) }}</p>
      <div class="table-wrap"><table>
        <tr><th>{{ t.imp_th_file }}</th><th>{{ t.imp_th_total }}</th><th>{{ t.imp_th_valid }}</th><th>{{ t.imp_th_rejected }}</th><th>{{ t.imp_th_merged }}</th><th>{{ t.imp_th_encoding }}</th><th>{{ t.imp_th_result }}</th></tr>
        {% for kind, cols in schemas %}
          {% set f = report.files.get(kind) %}
          <tr data-import-file="{{ kind }}">
            <td>{{ t['imp_file_' ~ kind] }}</td>
            {% if f %}
              <td>{{ f.rows_total }}</td><td>{{ f.rows_valid }}</td><td data-rows-rejected>{{ f.rows_rejected }}</td>
              <td data-rows-merged>{{ f.rows_merged or 0 }}</td>
              <td data-encoding="{{ f.encoding or '' }}">{{ t.get('imp_enc_' ~ f.encoding, f.encoding|upper) if f.encoding else '—' }}</td>
              <td><span class="badge {{ 'done' if f.accepted else 'failed' }}">{{ t.imp_file_accepted if f.accepted else t.imp_file_skipped }}</span></td>
            {% else %}
              <td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td class="muted">{{ t.imp_file_missing }}</td>
            {% endif %}
          </tr>
        {% endfor %}
      </table></div>
      {% if notices %}
        <ul class="import-notices" data-import-notices>
          {% for n in notices %}<li class="sample-note import-warning" data-notice-code="{{ n.code }}">{{ t['imp_file_' ~ n.file] }}: {{ n.message }}</li>{% endfor %}
        </ul>
      {% endif %}
      {% if errors %}
        <div class="table-wrap"><table data-import-errors>
          <tr><th>{{ t.imp_th_file }}</th><th>{{ t.imp_th_row }}</th><th>{{ t.imp_th_field }}</th><th>{{ t.imp_th_value }}</th><th>{{ t.imp_th_problem }}</th></tr>
          {% for e in errors %}
            <tr class="import-error-row" data-error-code="{{ e.code }}">
              <td>{{ t['imp_file_' ~ e.file] }}</td><td>{{ e.row or '—' }}</td>
              <td>{{ t.get('imp_field_' ~ e.field, e.field) if e.field else '—' }}</td>
              <td><code>{{ e.value }}</code></td><td>{{ e.message }}</td>
            </tr>
          {% endfor %}
        </table></div>
        {% if hidden_errors > 0 %}<p class="composer-help">{{ t.imp_errors_more.format(n=hidden_errors) }}</p>{% endif %}
      {% else %}
        <div class="empty">{{ t.imp_no_errors }}</div>
      {% endif %}
    {% else %}
      <div class="empty">{{ t.imp_report_none }}</div>
    {% endif %}
  </div>
"""


@bp.get("/import")
def import_page():
    t = _t()
    state = commerce_import.load_state()
    report = (state or {}).get("last_report")
    errors = []
    notices = []
    if report:
        errors = [
            {**item, "message": commerce_import.error_message(item, t)}
            for item in report.get("errors") or []
        ]
        notices = [
            {**item, "message": commerce_import.notice_message(item, t)}
            for item in report.get("notices") or []
        ]
        notices = [item for item in notices if item["message"]]
    unmatched = 0
    if state and state.get("data"):
        unmatched = commerce_import.count_unmatched_orders(
            state["data"].get("products"), state["data"].get("orders"))
    return _page(
        "mod_data_import_title", "import", MODULE_HEAD + IMPORT_BODY,
        state=state, report=report, errors=errors, notices=notices,
        unmatched_orders=unmatched,
        hidden_errors=(report or {}).get("error_count", 0) - len(errors),
        schemas=[(kind, commerce_import.SCHEMAS[kind]) for kind in commerce_import.FILE_ORDER],
        max_mb=commerce_import.MAX_FILE_BYTES // (1024 * 1024),
        import_dir="data/import/",
        **_module_context("data_import"),
    )


def _after_import(report):
    if report is None:
        return
    _flash("ok" if report["accepted"] else "error",
           "import_done" if report["accepted"] else "import_nothing_valid")


@bp.post("/import")
def import_upload():
    require_csrf()
    files = {}
    for kind in commerce_import.FILE_ORDER:
        storage = request.files.get(kind)
        if storage is None or not (storage.filename or "").strip():
            continue
        if not storage.filename.lower().endswith(".csv"):
            _flash("error", "import_bad_type")
            return redirect("/import")
        data = storage.stream.read(commerce_import.MAX_FILE_BYTES + 1)
        files[kind] = (storage.filename[:120], data)
    if not files:
        _flash("error", "import_no_files")
        return redirect("/import")
    _after_import(commerce_import.import_files(files, source="upload"))
    return redirect("/import")


@bp.post("/import/folder")
def import_folder():
    require_csrf()
    report = commerce_import.import_from_folder(source="folder")
    if report is None:
        _flash("error", "import_folder_empty")
    else:
        _after_import(report)
    return redirect("/import")


@bp.post("/import/toggle")
def import_toggle():
    require_csrf()
    active = request.form.get("active") == "1"
    if not commerce_import.set_active(active):
        _flash("error", "no_import_data")
    else:
        _flash("ok", "import_on" if active else "import_off")
    return redirect("/import")


@bp.post("/import/reset")
def import_reset():
    require_csrf()
    commerce_import.reset_import()
    _flash("ok", "import_reset")
    return redirect("/import")


@bp.get("/inbox")
def approval_inbox():
    drafts = commerce_demo.draft_views(locale=_HOOKS["get_locale"]())
    pending = [d for d in drafts if d["approval_status"] == "waiting_approval"]
    decided = [d for d in drafts if d["approval_status"] in {"approved", "rejected"}]
    body = MODULE_HEAD + """
  <div class="grid">
    <div class="card"><span class="metric-label">{{ t.demo_inbox_pending }}</span><span class="metric-value">{{ pending|length }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_inbox_high_risk }}</span><span class="metric-value">{{ pending|selectattr('high_risk')|list|length }}</span></div>
    <div class="card"><span class="metric-label">{{ t.demo_inbox_decided }}</span><span class="metric-value">{{ decided|length }}</span></div>
  </div>
  <div class="section"><div class="warning">{{ t.demo_inbox_rule }}</div></div>

  <h3>{{ t.demo_inbox_pending }}</h3>
  {% if not pending %}<div class="section"><div class="empty">{{ t.demo_inbox_empty }}</div></div>{% endif %}
  {% for d in pending %}
    <div class="section inbox-item {{ 'inbox-high' if d.high_risk }}" id="{{ d.id }}">
      <div class="inbox-head">
        <div>
          <span class="badge waiting_approval">{{ d.status|status_label }}</span>
          <strong>{{ t.get('mod_' ~ d.module ~ '_title', d.module) }}</strong> · {{ t.get('kind_' ~ d.kind, d.kind) }} · v{{ d.version }}
          <div class="muted">{{ d.title }}</div>
        </div>
        <div>{% for r in d.risk_tags %}<span class="risk-tag risk-{{ r }}">{{ t.get('risk_' ~ r, r) }}</span>{% endfor %}</div>
      </div>
      <dl class="kv">
        <dt>{{ t.demo_th_source }}</dt><dd>{{ d.source.refs|join(', ') }} · <span class="muted">{{ d.source.dataset }}</span></dd>
        <dt>{{ t.demo_th_generator }}</dt><dd>{{ d.provenance.provider }}{% if d.provenance.model %} · {{ d.provenance.model }}{% endif %}{% if d.provenance.fallback_reason %} <span class="muted">({{ t.demo_fallback }})</span>{% endif %}{% if d.provenance.edited_by_human %} · {{ t.demo_edited }}{% endif %}</dd>
        <dt>{{ t.demo_th_operator }}</dt><dd>{{ d.versions[-1].created_by }} · {{ d.versions[-1].created_at_utc|local_time }}</dd>
        <dt>{{ t.demo_th_task }}</dt><dd><a href="/tasks/{{ d.id }}"><code>{{ d.id }}</code></a></dd>
      </dl>
      <pre class="draft-body">{{ d.body }}</pre>
      <div class="inbox-actions">
        <form method="post" action="/inbox/{{ d.id }}/approve" class="demo-form">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="version" value="{{ d.version }}">
          <label class="demo-field"><span>{{ t.demo_channel }}</span>
            <select name="channel">{% for c in d.channels %}<option value="{{ c }}">{{ t.get('ch_' ~ c, c) }}</option>{% endfor %}</select>
          </label>
          """ + OPERATOR_FIELDS + """
          <label class="demo-field"><span>{{ t.demo_note }}</span><input type="text" name="note" maxlength="300"></label>
          <button type="submit" class="btn-approve">{{ t.demo_approve }} v{{ d.version }}</button>
        </form>
        <form method="post" action="/inbox/{{ d.id }}/reject" class="demo-form">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="version" value="{{ d.version }}">
          """ + OPERATOR_FIELDS + """
          <label class="demo-field"><span>{{ t.demo_reject_reason }}</span><input type="text" name="note" maxlength="300" required></label>
          <button type="submit" class="btn-reject">{{ t.demo_reject }}</button>
        </form>
      </div>
      <details class="quiet-details">
        <summary>{{ t.demo_edit_version }}</summary>
        <form method="post" action="/inbox/{{ d.id }}/revise" class="demo-form">
          <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
          <input type="hidden" name="version" value="{{ d.version }}">
          <label class="demo-field demo-field-wide"><span>{{ t.demo_draft_text }}</span>
            <textarea name="body" rows="8" maxlength="4000" required>{{ d.body }}</textarea></label>
          """ + OPERATOR_FIELDS + """
          <button type="submit">{{ t.demo_save_version }}</button>
        </form>
      </details>
    </div>
  {% endfor %}

  <div class="section">
    <h3>{{ t.demo_inbox_decided }}</h3>
    {% if decided %}
      <div class="table-wrap"><table>
        <tr><th>{{ t.th_task }}</th><th>{{ t.th_status }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_approver }}</th><th>{{ t.demo_channel }}</th><th>{{ t.demo_th_time }}</th></tr>
        {% for d in decided %}
          <tr id="{{ d.id }}">
            <td><a href="/tasks/{{ d.id }}">{{ d.title }}</a></td>
            <td><span class="badge {{ d.status }}">{{ d.status|status_label }}</span></td>
            <td>v{{ d.decision.version if d.decision else d.version }}</td>
            <td>{{ d.decision.approver if d.decision else '' }}</td>
            <td>{{ t.get('ch_' ~ d.decision.publish_channel, d.decision.publish_channel) if d.decision and d.decision.publish_channel else '—' }}</td>
            <td>{{ d.decision.decided_at_utc|local_time if d.decision else '' }}</td>
          </tr>
        {% endfor %}
      </table></div>
    {% else %}
      <div class="empty">{{ t.demo_no_drafts }}</div>
    {% endif %}
    <p class="composer-help"><a href="/audit">{{ t.demo_view_audit }}</a></p>
  </div>
"""
    return _page(
        "mod_approval_inbox_title", "inbox", body,
        pending=pending, decided=decided,
        **_module_context("approval_inbox"),
    )


@bp.get("/audit")
def audit_log():
    body = MODULE_HEAD + """
  <div class="section"><div class="warning">{{ t.demo_audit_publish_note }}</div></div>
  <div class="section">
    <h3>{{ t.demo_audit_records }} ({{ records|length }})</h3>
    {% if records %}
      <div class="table-wrap"><table>
        <tr><th>{{ t.demo_th_time }}</th><th>{{ t.demo_th_decision }}</th><th>{{ t.demo_th_module }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_source }}</th><th>{{ t.demo_th_operator }}</th><th>{{ t.demo_th_approver }}</th><th>{{ t.demo_channel }}</th><th>{{ t.demo_th_audit }}</th></tr>
        {% for a in records %}
          <tr>
            <td>{{ a.decided_at_utc|local_time }}</td>
            <td><span class="badge {{ a.decision }}">{{ a.decision|status_label }}</span></td>
            <td>{{ t.get('mod_' ~ a.module ~ '_title', a.module) }}<br><a class="muted" href="/tasks/{{ a.task_id }}">{{ a.task_id|short_id(14, 6) }}</a></td>
            <td>v{{ a.version }}<br><code>{{ a.content_sha256|short_id(14, 4) }}</code></td>
            <td>{{ a.source.refs|join(', ') }}<br><span class="muted">{{ a.source.provider }}{% if a.source.edited_by_human %} · {{ t.demo_edited }}{% endif %}</span></td>
            <td>{{ a.operator }}</td>
            <td>{{ a.approver }}</td>
            <td>{{ t.get('ch_' ~ a.publish_channel, a.publish_channel) if a.publish_channel else '—' }}<br><span class="muted">{{ t.get('pubmode_' ~ a.publish_mode, a.publish_mode) }}</span></td>
            <td><a href="/artifacts/ecom_audit"><code>{{ a.audit_artifact_id|short_id(18, 6) }}</code></a></td>
          </tr>
        {% endfor %}
      </table></div>
    {% else %}
      <div class="empty">{{ t.demo_audit_empty }}</div>
    {% endif %}
  </div>
  <div class="section">
    <h3>{{ t.demo_audit_events }}</h3>
    {% if events %}
      {% for e in events[:40] %}
        <div class="event">
          <span class="event-time">{{ e.timestamp|short_time }}</span>
          <span class="event-name">{{ e.event }}</span>
          <a href="/tasks/{{ e.task_id }}">{{ e.task_id|short_id(14, 6) }}</a>
          <p class="event-message">{{ e.message }}</p>
        </div>
      {% endfor %}
    {% else %}
      <div class="empty">{{ t.empty_events }}</div>
    {% endif %}
  </div>
"""
    return _page(
        "mod_audit_log_title", "audit", body,
        records=commerce_demo.audit_records(),
        events=commerce_demo.demo_events(),
        **_module_context("audit_log"),
    )


# ---------------------------------------------------------------------------
# POST actions (CSRF + named operator on every one)
# ---------------------------------------------------------------------------

DRAFT_PARAM_FIELDS = (
    "sku", "content_type", "lead_id", "inquiry_id",
    "audience_id", "objective", "kb_id", "proposal",
)

KIND_RETURN = {
    kind: MODULE_ROUTES[spec["module"]]
    for kind, spec in commerce_demo.DRAFT_KINDS.items()
}


@bp.post("/demo/draft")
def create_draft():
    require_csrf()
    kind = request.form.get("kind", "")
    if kind not in commerce_demo.DRAFT_KINDS:
        abort(400)
    back = KIND_RETURN[kind]
    operator = request.form.get("operator", "")
    _remember_operator(operator)

    last = session.get("demo_last_draft_at", 0)
    if time.time() - last < DRAFT_MIN_INTERVAL_SECONDS:
        _flash("error", "rate_limited")
        return redirect(back)

    params = {
        name: request.form.get(name, "") for name in DRAFT_PARAM_FIELDS
    }
    language = request.form.get("language", "en")
    if kind == "lead_reply" and language == "auto":
        inquiry = commerce_demo.inquiry_index().get(params["inquiry_id"])
        language = (
            inquiry["lang"]
            if inquiry and inquiry.get("lang") in commerce_demo.LANGUAGES
            else "en"
        )

    session_key = session.get("chat_id") or session.get("csrf_token", "")
    try:
        result = commerce_demo.create_draft(
            kind,
            params,
            operator,
            language=language,
            session_id_sha256=commerce_demo.sha256_value(session_key),
        )
    except DemoError as error:
        _flash("error", error.code)
        return redirect(back)

    session["demo_last_draft_at"] = time.time()
    _flash("ok", "draft_created", task_id=result["task_id"])
    return redirect(back)


def _decision_route(task_id, action):
    require_csrf()
    if not commerce_demo.is_demo_task_id(task_id):
        abort(404)
    operator = request.form.get("operator", "")
    _remember_operator(operator)
    version = request.form.get("version", "")
    try:
        if action == "revise":
            commerce_demo.revise_draft(
                task_id, request.form.get("body", ""), operator, version
            )
            code = "draft_revised"
        else:
            commerce_demo.decide(
                task_id,
                action,
                operator,
                version,
                channel=request.form.get("channel"),
                note=request.form.get("note", ""),
            )
            code = action
    except DemoError as error:
        if error.code == "not_found":
            abort(404)
        _flash("error", error.code, task_id=task_id)
        return redirect(f"/inbox#{task_id}")
    _flash("ok", code, task_id=task_id)
    return redirect("/audit" if action in {"approved", "rejected"} else f"/inbox#{task_id}")


@bp.post("/inbox/<task_id>/approve")
def approve(task_id):
    return _decision_route(task_id, "approved")


@bp.post("/inbox/<task_id>/reject")
def reject(task_id):
    return _decision_route(task_id, "rejected")


@bp.post("/inbox/<task_id>/revise")
def revise(task_id):
    return _decision_route(task_id, "revise")
