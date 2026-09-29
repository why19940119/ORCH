"""ORCH cross-border e-commerce demo pages (v0.18.0).

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
    return _HOOKS["render_page"](
        t[title_key], active, COMMON_HEAD + body, **context
    )


COMMON_HEAD = """
  <div class="sample-banner" data-sample-banner>
    <strong>{{ t.demo_sample_badge }}</strong>
    <span>{{ t.demo_sample_banner }}</span>
    <span class="sample-meta">{{ meta.brand }} · {{ meta.target_market }} · {{ meta.version }}</span>
  </div>
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
      <option value="zh-Hant" {% if locale.startswith('zh') %}selected{% endif %}>繁體中文</option>
      <option value="en" {% if not locale.startswith('zh') %}selected{% endif %}>English</option>
    </select>
  </label>
"""

DRAFT_SUBMIT = """
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button type="submit">{{ t.demo_draft_button }}</button>
  <p class="composer-help">{{ t.demo_draft_help }}</p>
"""

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


def _drafts_for(kind):
    return [d for d in commerce_demo.draft_views() if d["kind"] == kind][:10]


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
    <div class="table-wrap"><table>
      <tr><th>ID</th><th>SKU</th><th>{{ t.demo_th_qty }}</th><th>{{ t.demo_th_stage }}</th><th>{{ t.demo_th_value }}</th><th>{{ t.demo_th_inquiry }}</th></tr>
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
    <h3>{{ t.demo_products }} ({{ skus|length }})</h3>
    <div class="table-wrap"><table>
      <tr><th>SKU</th><th>{{ t.demo_th_name }}</th><th>{{ t.demo_th_category }}</th><th>{{ t.demo_th_price }}</th><th>{{ t.demo_th_stock }}</th></tr>
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
        skus=data["skus"], inquiries=data["inquiries"], leads=leads,
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
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="content">
      <label class="demo-field"><span>SKU</span>
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
        skus=data["skus"], content_types=commerce_demo.CONTENT_TYPES,
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
    <div class="table-wrap"><table>
      <tr><th>ID</th><th>{{ t.demo_th_section }}</th><th>{{ t.demo_th_entry }}</th><th>{{ t.demo_th_version }}</th><th>{{ t.demo_th_approved_by }}</th></tr>
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
    <div class="table-wrap"><table>
      <tr><th>SKU</th><th>{{ t.demo_th_name }}</th><th>{{ t.demo_th_facts }}</th><th>{{ t.demo_th_claims }}</th></tr>
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
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="lead_reply">
      <label class="demo-field"><span>{{ t.demo_th_inquiry }}</span>
        <select name="inquiry_id">{% for r in rows %}<option value="{{ r.id }}">{{ r.id }} · {{ r.triage.score }} · {{ t.get('icat_' ~ r.triage.category, r.triage.category) }}</option>{% endfor %}</select>
      </label>
      <label class="demo-field">
        <span>{{ t.demo_language }}</span>
        <select name="language">
          <option value="auto" selected>{{ t.demo_lang_auto }}</option>
          <option value="zh-Hant">繁體中文</option>
          <option value="en">English</option>
        </select>
      </label>
      """ + OPERATOR_FIELDS + DRAFT_SUBMIT + """
    </form>
  </div>

  <div class="section">
    <h3>{{ t.demo_inquiry_triage }}</h3>
    <p class="composer-help">{{ t.demo_triage_note }}</p>
    <div class="table-wrap"><table>
      <tr><th>{{ t.demo_th_score }}</th><th>ID</th><th>{{ t.demo_th_category }}</th><th>{{ t.demo_th_channel }}</th><th>{{ t.demo_th_customer }}</th><th>{{ t.demo_th_message }}</th></tr>
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


@bp.get("/campaigns")
def campaign_engine():
    data = commerce_demo.load_sample_data()
    body = MODULE_HEAD + """
  <div class="section">
    <h3>{{ t.demo_campaign_draft_title }}</h3>
    <form method="post" action="/demo/draft" class="demo-form">
      <input type="hidden" name="kind" value="campaign">
      <label class="demo-field"><span>SKU</span>
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
""" + DRAFT_LIST
    return _page(
        "mod_campaign_engine_title", "campaigns", body,
        skus=data["skus"], audiences=data["campaign"]["audiences"],
        objectives=commerce_demo.CAMPAIGN_OBJECTIVES,
        drafts=_drafts_for("campaign"),
        **_module_context("campaign_engine"),
    )


@bp.get("/market")
def market_dashboard():
    summary = commerce_demo.kpi_summary()
    body = MODULE_HEAD + """
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
""" + DRAFT_LIST
    return _page(
        "mod_market_dashboard_title", "market", body,
        k=summary, drafts=_drafts_for("market_insight"),
        **_module_context("market_dashboard"),
    )


@bp.get("/inbox")
def approval_inbox():
    drafts = commerce_demo.draft_views()
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
