"""ORCH admin pages (v0.20.0, WP-ORCH-11).

/admin/users        accounts + account changes (second-admin approval)
/admin/approvers    per-module approver assignment
/admin/retention    retention settings, deadline default, purge (audited)
/admin/permissions  權限清單 (users, roles, module approvers) + CSV export

Admin role only; every POST needs the CSRF token. Registered from
orch_ui with hooks so it never imports orch_ui (no __main__ double import).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, g, redirect, request, session

import orch_auth

bp = Blueprint("admin", __name__)
_HOOKS = {}


def register(app, *, render_page, get_csrf_token, get_locale, ui_strings):
    _HOOKS.update(render_page=render_page, get_csrf_token=get_csrf_token,
                  get_locale=get_locale, ui_strings=ui_strings)
    app.register_blueprint(bp)


def _t():
    return _HOOKS["ui_strings"](_HOOKS["get_locale"]())


def _admin():
    user = g.get("user") or {}
    if user.get("role") != "admin":
        abort(403)
    return user["username"]


def _csrf():
    expected = _HOOKS["get_csrf_token"]()
    submitted = request.form.get("csrf_token", "")
    if not submitted or len(submitted) != len(expected) or not secrets.compare_digest(
        expected, submitted
    ):
        abort(400)


def _flash(kind, code):
    session["admin_flash"] = {"kind": kind, "code": code}


def _page(title_key, active, body, **context):
    t = _t()
    context["flash"] = session.pop("admin_flash", None)
    head = """
  {% if flash %}
    <div class="demo-flash demo-flash-{{ flash.kind }}" role="status" data-admin-flash="{{ flash.code }}">
      {{ t.get('gov_msg_' ~ flash.code, t.get('auth_err_' ~ flash.code, t.auth_err_generic)) }}
    </div>
  {% endif %}
  <h2>{{ t[title_key] }}</h2>
"""
    return _HOOKS["render_page"](t[title_key], active, head + body,
                                 title_key=title_key, **context)


def _run(action, success_code, back):
    try:
        result = action()
    except orch_auth.AuthError as error:
        _flash("error", error.code)
        return redirect(back)
    code = success_code(result) if callable(success_code) else success_code
    _flash("ok", code)
    return redirect(back)


# ---------------------------------------------------------------------------
# Users + account changes
# ---------------------------------------------------------------------------

USERS_BODY = """
  <p class="subtitle">{{ t.adm_users_intro }}</p>
  {% if single_admin and bootstrap_open %}<div class="warning" data-single-admin>{{ t.adm_single_admin_note }}</div>
  {% elif single_admin %}<div class="warning" data-single-admin-closed>{{ t.adm_single_admin_closed_note }}</div>{% endif %}

  <div class="section">
    <h3>{{ t.adm_pending_title }}</h3>
    {% set open = changes|selectattr('status', 'equalto', 'pending')|list %}
    {% if open %}
      <div class="table-wrap"><table data-pending-changes>
        <tr><th>{{ t.adm_th_change }}</th><th>{{ t.adm_th_target }}</th><th>{{ t.adm_th_requested_by }}</th><th>{{ t.demo_th_time }}</th><th></th></tr>
        {% for c in open %}
          <tr id="{{ c.id }}">
            <td>{{ t['adm_kind_' ~ c.kind] }}{% if c.params.role %} → {{ t['role_' ~ c.params.role] }}{% endif %}</td>
            <td>{{ c.target }}</td><td>{{ c.requested_by }}</td><td>{{ c.requested_at_utc|local_time }}</td>
            <td>
              {% if c.requested_by|lower == current_user.username|lower %}
                <span class="muted">{{ t.adm_waiting_other_admin }}</span>
              {% else %}
                <form method="post" action="/admin/changes/{{ c.id }}/approved" class="demo-form">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit" class="btn-approve">{{ t.demo_approve }}</button>
                </form>
                <form method="post" action="/admin/changes/{{ c.id }}/rejected" class="demo-form">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                  <button type="submit" class="btn-reject">{{ t.demo_reject }}</button>
                </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </table></div>
    {% else %}
      <div class="empty">{{ t.adm_no_pending }}</div>
    {% endif %}
  </div>

  <div class="section">
    <h3>{{ t.adm_users_title }} ({{ users|length }})</h3>
    <div class="table-wrap"><table data-users>
      <tr><th>{{ t.auth_username }}</th><th>{{ t.perm_col_role }}</th><th>{{ t.perm_col_status }}</th><th>{{ t.perm_col_last_login }}</th><th>{{ t.adm_th_actions }}</th></tr>
      {% for u in users %}
        <tr>
          <td>{{ u.username }}</td>
          <td>{{ t['role_' ~ u.role] }}</td>
          <td>{% if u.disabled %}{{ t.perm_status_disabled }}{% elif now_locked(u) %}{{ t.adm_status_locked }}{% else %}{{ t.perm_status_active }}{% endif %}</td>
          <td>{{ u.last_login_utc|local_time if u.last_login_utc else '—' }}</td>
          <td>
            {% if u.username|lower != current_user.username|lower %}
            <form method="post" action="/admin/users/request" class="demo-form">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <input type="hidden" name="target" value="{{ u.username }}">
              <input type="hidden" name="kind" value="change_role">
              <select name="role">{% for r in roles if r != u.role %}<option value="{{ r }}">{{ t['role_' ~ r] }}</option>{% endfor %}</select>
              <button type="submit">{{ t.adm_kind_change_role }}</button>
            </form>
            <form method="post" action="/admin/users/request" class="demo-form">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <input type="hidden" name="target" value="{{ u.username }}">
              <input type="hidden" name="kind" value="{{ 'enable' if u.disabled else 'disable' }}">
              <button type="submit" class="{{ '' if u.disabled else 'btn-reject' }}">{{ t['adm_kind_' ~ ('enable' if u.disabled else 'disable')] }}</button>
            </form>
            {% endif %}
            <form method="post" action="/admin/users/request" class="demo-form">
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <input type="hidden" name="target" value="{{ u.username }}">
              <input type="hidden" name="kind" value="reset_password">
              <input type="password" name="password" minlength="10" maxlength="128" required placeholder="{{ t.adm_new_password }}" autocomplete="new-password">
              <button type="submit">{{ t.adm_kind_reset_password }}</button>
            </form>
            {% if now_locked(u) or u.failed_logins %}
            <form method="post" action="/admin/users/unlock" class="demo-form" data-unlock-form>
              <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
              <input type="hidden" name="target" value="{{ u.username }}">
              <button type="submit">{{ t.adm_unlock }}</button>
            </form>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{{ t.adm_create_title }}</h3>
    <form method="post" action="/admin/users/request" class="demo-form" data-create-user>
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <input type="hidden" name="kind" value="create_user">
      <label class="demo-field"><span>{{ t.auth_username }}</span><input type="text" name="target" required minlength="2" maxlength="40"></label>
      <label class="demo-field"><span>{{ t.perm_col_role }}</span>
        <select name="role">{% for r in roles %}<option value="{{ r }}" {% if r == 'editor' %}selected{% endif %}>{{ t['role_' ~ r] }}</option>{% endfor %}</select></label>
      <label class="demo-field"><span>{{ t.adm_temp_password }}</span><input type="password" name="password" required minlength="10" maxlength="128" autocomplete="new-password"></label>
      <button type="submit">{{ t.adm_request_button }}</button>
      <p class="composer-help">{{ t.adm_create_help }}</p>
    </form>
  </div>

  <div class="section">
    <h3>{{ t.adm_roles_title }}</h3>
    <div class="table-wrap"><table>
      {% for r in roles %}<tr><td><strong>{{ t['role_' ~ r] }}</strong></td><td>{{ t['role_' ~ r ~ '_desc'] }}</td></tr>{% endfor %}
    </table></div>
  </div>

  <div class="section">
    <h3>{{ t.adm_history_title }}</h3>
    {% set done = changes|rejectattr('status', 'equalto', 'pending')|list %}
    {% if done %}
      <div class="table-wrap"><table>
        <tr><th>{{ t.adm_th_change }}</th><th>{{ t.adm_th_target }}</th><th>{{ t.adm_th_requested_by }}</th><th>{{ t.adm_th_decided_by }}</th><th>{{ t.perm_col_status }}</th></tr>
        {% for c in done[:30] %}
          <tr><td>{{ t['adm_kind_' ~ c.kind] }}{% if c.params.role %} → {{ t['role_' ~ c.params.role] }}{% endif %}</td><td>{{ c.target }}</td><td>{{ c.requested_by }}</td>
            <td>{{ c.decided_by }} · {{ c.decided_at_utc|local_time }}{% if c.bootstrap_exception %} <span class="legacy-tag">{{ t.adm_bootstrap_exception }}</span>{% endif %}</td>
            <td>{{ t['adm_status_' ~ c.status] }}{% if c.auto_close_reason %} <span class="composer-help" data-auto-close-reason="{{ c.auto_close_reason }}">· {{ t.get('auth_err_' ~ c.auto_close_reason, c.auto_close_reason) }}</span>{% endif %}</td></tr>
        {% endfor %}
      </table></div>
    {% else %}<div class="empty">{{ t.adm_no_history }}</div>{% endif %}
  </div>
"""


@bp.get("/admin/users")
def users():
    _admin()
    store = orch_auth.load_store()
    return _page(
        "adm_users_heading", "admin_users", USERS_BODY,
        users=orch_auth.list_users(store), changes=orch_auth.pending_changes(store),
        roles=orch_auth.ROLES, single_admin=len(orch_auth.active_admins(store)) == 1,
        bootstrap_open=orch_auth.bootstrap_open(store), now_locked=orch_auth.is_locked,
    )


@bp.post("/admin/users/request")
def request_change():
    actor = _admin()
    _csrf()
    kind = request.form.get("kind", "")
    return _run(
        lambda: orch_auth.request_change(
            actor, kind, request.form.get("target", ""),
            role=request.form.get("role") if kind in {"create_user", "change_role"} else None,
            password=request.form.get("password") if kind in {"create_user", "reset_password"} else None,
        ),
        lambda change: ("change_applied" if change["status"] == "applied"
                        else "awaiting_second_admin" if change.get("awaiting_second_admin")
                        else "change_requested"),
        "/admin/users",
    )


@bp.post("/admin/users/unlock")
def unlock_user():
    """Review fix: admins clear a lockout from the web UI (audited, CSRF)."""
    actor = _admin()
    _csrf()
    return _run(
        lambda: orch_auth.unlock_user(request.form.get("target", ""), actor=actor, via="web"),
        "unlocked",
        "/admin/users",
    )


@bp.post("/admin/changes/<change_id>/<decision>")
def decide_change(change_id, decision):
    actor = _admin()
    _csrf()
    if decision not in {"approved", "rejected"}:
        abort(404)
    return _run(
        lambda: orch_auth.decide_change(actor, change_id, decision),
        lambda change: ("change_auto_closed" if change.get("status") == "auto_closed"
                        else "change_" + decision),
        "/admin/users",
    )


# ---------------------------------------------------------------------------
# Module approvers
# ---------------------------------------------------------------------------

APPROVERS_BODY = """
  <p class="subtitle">{{ t.adm_approvers_intro }}</p>
  <form method="post" action="/admin/approvers" class="section">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <div class="table-wrap"><table data-module-approvers>
      <tr><th>{{ t.perm_col_module }}</th>{% for a in approvers %}<th>{{ a.username }}</th>{% endfor %}</tr>
      {% for m in modules %}
        <tr><td>{{ t['mod_' ~ m ~ '_title'] }}{% if not assigned[m] %} <span class="muted">({{ t.perm_any_approver }})</span>{% endif %}</td>
          {% for a in approvers %}
            <td><input type="checkbox" name="{{ m }}" value="{{ a.username }}" {% if a.username in assigned[m] %}checked{% endif %} aria-label="{{ m }} {{ a.username }}"></td>
          {% endfor %}
        </tr>
      {% endfor %}
    </table></div>
    {% if approvers %}<button type="submit">{{ t.adm_save }}</button>{% else %}<div class="empty">{{ t.adm_no_approvers }}</div>{% endif %}
  </form>
"""


@bp.get("/admin/approvers")
def approvers():
    _admin()
    store = orch_auth.load_store()
    return _page(
        "adm_approvers_heading", "admin_approvers", APPROVERS_BODY,
        modules=orch_auth.MODULES, assigned=orch_auth.module_approvers(store),
        approvers=[u for u in orch_auth.list_users(store)
                   if u["role"] == "approver" and not u["disabled"]],
    )


@bp.post("/admin/approvers")
def save_approvers():
    actor = _admin()
    _csrf()

    def action():
        current = orch_auth.module_approvers()
        for module in orch_auth.MODULES:
            wanted = request.form.getlist(module)
            if sorted(wanted) != sorted(current[module]):
                orch_auth.set_module_approvers(actor, module, wanted)

    return _run(action, "approvers_saved", "/admin/approvers")


# ---------------------------------------------------------------------------
# Retention + purge
# ---------------------------------------------------------------------------

RETENTION_BODY = """
  <p class="subtitle">{{ t.adm_retention_intro }}</p>
  <form method="post" action="/admin/retention" class="section demo-form">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <label class="demo-field"><span>{{ t.adm_draft_days }}</span><input type="number" name="draft_retention_days" min="1" max="3650" value="{{ s.draft_retention_days }}"></label>
    <label class="demo-field"><span>{{ t.adm_upload_days }}</span><input type="number" name="upload_retention_days" min="1" max="3650" value="{{ s.upload_retention_days }}"></label>
    <label class="demo-field"><span>{{ t.adm_deadline_default }}</span><input type="number" name="default_deadline_hours" min="1" max="720" value="{{ s.default_deadline_hours }}"></label>
    <button type="submit">{{ t.adm_save }}</button>
  </form>
  <div class="section">
    <h3>{{ t.adm_purge_title }}</h3>
    <p class="composer-help">{{ t.adm_purge_help }}</p>
    <form method="post" action="/admin/retention/purge" class="demo-form">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button type="submit" class="btn-reject" data-purge>{{ t.adm_purge_button }}</button>
      <span class="composer-help">{{ t.adm_purge_cli }}</span>
    </form>
    {% if purges %}
      <div class="table-wrap"><table data-purges>
        <tr><th>{{ t.demo_th_time }}</th><th>{{ t.gov_th_actor }}</th><th>{{ t.adm_th_drafts_removed }}</th><th>{{ t.adm_th_uploads_removed }}</th></tr>
        {% for p in purges[:20] %}<tr><td>{{ p.ts_utc|local_time }}</td><td>{{ p.actor }}</td><td>{{ p.drafts_removed }}</td><td>{{ p.upload_batches_removed }}</td></tr>{% endfor %}
      </table></div>
    {% endif %}
  </div>
"""


@bp.get("/admin/retention")
def retention():
    _admin()
    purges = [r for r in orch_auth.read_audit(1000) if r.get("event") == "retention_purge"]
    return _page("adm_retention_heading", "admin_retention", RETENTION_BODY,
                 s=orch_auth.settings(), purges=purges)


@bp.post("/admin/retention")
def save_retention():
    actor = _admin()
    _csrf()
    return _run(
        lambda: orch_auth.update_settings(
            actor,
            draft_retention_days=request.form.get("draft_retention_days"),
            upload_retention_days=request.form.get("upload_retention_days"),
            default_deadline_hours=request.form.get("default_deadline_hours"),
        ),
        "settings_saved",
        "/admin/retention",
    )


@bp.post("/admin/retention/purge")
def run_purge():
    actor = _admin()
    _csrf()
    return _run(lambda: orch_auth.purge(actor), "purged", "/admin/retention")


# ---------------------------------------------------------------------------
# 權限清單
# ---------------------------------------------------------------------------

PERMISSIONS_BODY = """
  <p class="subtitle">{{ t.perm_intro }}</p>
  <p><a class="button-link" href="/admin/permissions.csv" data-export>{{ t.perm_export }}</a></p>
  <div class="section">
    <h3>{{ t.perm_users_title }}</h3>
    <div class="table-wrap"><table data-permissions>
      <tr><th>{{ t.perm_col_user }}</th><th>{{ t.perm_col_role }}</th><th>{{ t.perm_col_status }}</th><th>{{ t.perm_col_modules }}</th><th>{{ t.perm_col_created }}</th><th>{{ t.perm_col_last_login }}</th></tr>
      {% for r in rows %}
        <tr><td>{{ r.username }}</td><td>{{ t['role_' ~ r.role] }}</td>
          <td>{{ t.perm_status_disabled if r.disabled else t.perm_status_active }}</td>
          <td>{% for m in r.approve_modules %}{{ t['mod_' ~ m ~ '_title'] }}{% if not loop.last %} / {% endif %}{% else %}—{% endfor %}</td>
          <td>{{ r.created_at_utc|local_time }}</td><td>{{ r.last_login_utc|local_time if r.last_login_utc else '—' }}</td></tr>
      {% endfor %}
    </table></div>
  </div>
  <div class="section">
    <h3>{{ t.perm_modules_title }}</h3>
    <div class="table-wrap"><table>
      <tr><th>{{ t.perm_col_module }}</th><th>{{ t.perm_col_assigned }}</th></tr>
      {% for m, names in assigned.items() %}<tr><td>{{ t['mod_' ~ m ~ '_title'] }}</td><td>{{ names|join(', ') if names else t.perm_any_approver }}</td></tr>{% endfor %}
    </table></div>
  </div>
  <div class="section">
    <h3>{{ t.adm_roles_title }}</h3>
    <div class="table-wrap"><table>
      {% for r in roles %}<tr><td><strong>{{ t['role_' ~ r] }}</strong></td><td>{{ t['role_' ~ r ~ '_desc'] }}</td></tr>{% endfor %}
    </table></div>
  </div>
"""


@bp.get("/admin/permissions")
def permissions():
    _admin()
    store = orch_auth.load_store()
    return _page("perm_heading", "admin_permissions", PERMISSIONS_BODY,
                 rows=orch_auth.permission_rows(store),
                 assigned=orch_auth.module_approvers(store), roles=orch_auth.ROLES)


@bp.get("/admin/permissions.csv")
def permissions_csv():
    actor = _admin()
    content = orch_auth.permissions_csv(_t())
    orch_auth.audit("permissions_exported", actor, format="csv")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        content.encode("utf-8"),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="orch-permissions-{stamp}.csv"',
                 "Cache-Control": "no-store"},
    )
