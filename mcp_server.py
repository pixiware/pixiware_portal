"""
Pixiware Portal — read-only MCP server for agencies.

An agency signs up in the portal, copies their personal MCP URL (``<base>/mcp``)
into Claude / ChatGPT, and the AI client walks a standard OAuth 2.1 flow
(discovery -> dynamic client registration -> authorize -> token). The agency
signs in once in the browser to approve, and the client receives a bearer token
scoped to that agency. Every MCP tool is read-only and automatically scoped to
the authenticated agency's own clients.

This module is a self-contained Flask blueprint. It reaches into the main
``app`` module for the DB connection and a couple of helpers (imported lazily to
avoid a circular import) and is registered from the bottom of ``app.py`` via
``register_mcp(app)``.

Transport: MCP Streamable HTTP (JSON-RPC 2.0 over a single POST /mcp). We reply
with a plain ``application/json`` body rather than an SSE stream, which every
current MCP client accepts and keeps the server stateless.
"""

import base64
import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify, Response, redirect, render_template_string

# The main application module (its DB helpers and a couple of utilities). It is
# injected via register_mcp() rather than imported at module top: app.py runs as
# __main__ in production, so a top-level ``import app`` would re-execute app.py as
# a second module and cause a circular import. Injection sidesteps that entirely.
portal = None

mcp_bp = Blueprint('mcp_bp', __name__)

MCP_PROTOCOL_VERSION = '2025-06-18'
SERVER_NAME = 'Pixiware Portal'
SERVER_VERSION = '1.0.0'
DEFAULT_SCOPE = 'mcp:read'
ACCESS_TOKEN_TTL = timedelta(hours=12)
AUTH_CODE_TTL = timedelta(minutes=5)

_mcp_schema_ready = False


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def _ensure_mcp_schema(conn):
    global _mcp_schema_ready
    if _mcp_schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.mcp_oauth_clients (
                client_id        text PRIMARY KEY,
                client_secret    text,
                client_name      text,
                redirect_uris    jsonb NOT NULL DEFAULT '[]'::jsonb,
                grant_types      jsonb,
                scope            text,
                token_auth_method text,
                created_at       timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.mcp_oauth_codes (
                code                  text PRIMARY KEY,
                client_id             text NOT NULL,
                user_id               bigint NOT NULL,
                redirect_uri          text,
                code_challenge        text,
                code_challenge_method text,
                scope                 text,
                resource              text,
                expires_at            timestamptz NOT NULL,
                used                  boolean NOT NULL DEFAULT false,
                created_at            timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.mcp_oauth_tokens (
                access_token  text PRIMARY KEY,
                refresh_token text UNIQUE,
                client_id     text NOT NULL,
                user_id       bigint NOT NULL,
                scope         text,
                expires_at    timestamptz NOT NULL,
                created_at    timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cur.execute(
            'CREATE INDEX IF NOT EXISTS idx_mcp_tokens_refresh ON public.mcp_oauth_tokens (refresh_token)'
        )
    _mcp_schema_ready = True


@contextmanager
def _db():
    conn = portal.get_db_connection()
    try:
        _ensure_mcp_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # dates and everything else with an isoformat()
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _base_url():
    """External base URL for building discovery/redirect URLs."""
    configured = (getattr(portal, 'APP_URL', '') or '').strip().rstrip('/')
    if configured:
        return configured
    return request.url_root.rstrip('/')


def _token():
    return secrets.token_urlsafe(32)


def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = (
        'Authorization, Content-Type, mcp-protocol-version, mcp-session-id'
    )
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Expose-Headers'] = 'WWW-Authenticate, mcp-session-id'
    return resp


@mcp_bp.after_request
def _add_cors(resp):
    return _cors(resp)


# --------------------------------------------------------------------------- #
# OAuth 2.1 discovery
# --------------------------------------------------------------------------- #
def _protected_resource_doc():
    base = _base_url()
    return {
        'resource': f'{base}/mcp',
        'authorization_servers': [base],
        'scopes_supported': [DEFAULT_SCOPE],
        'bearer_methods_supported': ['header'],
        'resource_documentation': f'{base}/mcp',
    }


def _authorization_server_doc():
    base = _base_url()
    return {
        'issuer': base,
        'authorization_endpoint': f'{base}/oauth/authorize',
        'token_endpoint': f'{base}/oauth/token',
        'registration_endpoint': f'{base}/oauth/register',
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256', 'plain'],
        'token_endpoint_auth_methods_supported': [
            'none', 'client_secret_post', 'client_secret_basic',
        ],
        'scopes_supported': [DEFAULT_SCOPE],
    }


@mcp_bp.route('/.well-known/oauth-protected-resource', methods=['GET', 'OPTIONS'])
@mcp_bp.route('/.well-known/oauth-protected-resource/<path:tail>', methods=['GET', 'OPTIONS'])
def well_known_protected_resource(tail=None):
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))
    return jsonify(_protected_resource_doc())


@mcp_bp.route('/.well-known/oauth-authorization-server', methods=['GET', 'OPTIONS'])
@mcp_bp.route('/.well-known/oauth-authorization-server/<path:tail>', methods=['GET', 'OPTIONS'])
@mcp_bp.route('/.well-known/openid-configuration', methods=['GET', 'OPTIONS'])
def well_known_authorization_server(tail=None):
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))
    return jsonify(_authorization_server_doc())


# --------------------------------------------------------------------------- #
# Dynamic client registration (RFC 7591)
# --------------------------------------------------------------------------- #
@mcp_bp.route('/oauth/register', methods=['POST', 'OPTIONS'])
def oauth_register():
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))

    body = request.get_json(silent=True) or {}
    redirect_uris = body.get('redirect_uris') or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return jsonify({'error': 'invalid_client_metadata',
                        'error_description': 'redirect_uris is required'}), 400

    client_name = body.get('client_name') or 'MCP Client'
    grant_types = body.get('grant_types') or ['authorization_code', 'refresh_token']
    auth_method = body.get('token_endpoint_auth_method') or 'none'
    scope = body.get('scope') or DEFAULT_SCOPE

    client_id = 'mcp_' + secrets.token_urlsafe(16)
    client_secret = None
    if auth_method != 'none':
        client_secret = secrets.token_urlsafe(32)

    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''
                INSERT INTO public.mcp_oauth_clients
                    (client_id, client_secret, client_name, redirect_uris,
                     grant_types, scope, token_auth_method)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    client_id, client_secret, client_name,
                    json.dumps(redirect_uris), json.dumps(grant_types),
                    scope, auth_method,
                ),
            )

    resp = {
        'client_id': client_id,
        'client_id_issued_at': int(_now().timestamp()),
        'redirect_uris': redirect_uris,
        'grant_types': grant_types,
        'response_types': ['code'],
        'token_endpoint_auth_method': auth_method,
        'scope': scope,
        'client_name': client_name,
    }
    if client_secret:
        resp['client_secret'] = client_secret
        resp['client_secret_expires_at'] = 0
    return jsonify(resp), 201


def _load_client(cur, client_id):
    cur.execute(
        '''SELECT client_id, client_secret, redirect_uris, token_auth_method
           FROM public.mcp_oauth_clients WHERE client_id = %s''',
        (client_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    redirect_uris = row[2] if isinstance(row[2], list) else json.loads(row[2] or '[]')
    return {
        'client_id': row[0],
        'client_secret': row[1],
        'redirect_uris': redirect_uris,
        'token_auth_method': row[3],
    }


# --------------------------------------------------------------------------- #
# Authorization endpoint
# --------------------------------------------------------------------------- #
_AUTHORIZE_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect AI · Pixiware Portal</title>
<style>
  :root { --grad: linear-gradient(135deg, #8bd34f 0%, #37a3b0 100%); }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f6f8fa; color:#0b1220; display:flex; min-height:100vh;
         align-items:center; justify-content:center; padding:24px; }
  .card { background:#fff; width:100%; max-width:420px; border:1px solid #e6eaef;
          border-radius:16px; padding:32px; box-shadow:0 12px 40px rgba(16,32,64,.08); }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:#5b6673; margin:0 0 8px; font-size:.95rem; }
  .scope { background:#f0fbf4; border:1px solid #d6f0df; border-radius:10px;
           padding:12px 14px; margin:18px 0; font-size:.85rem; color:#2b6b47; }
  .scope b { color:#1d4d33; }
  label { display:block; font-size:.8rem; font-weight:600; color:#3a4552; margin:14px 0 6px; }
  input { width:100%; padding:12px 14px; border:1px solid #d5dbe2; border-radius:10px;
          font-size:1rem; }
  input:focus { outline:none; border-color:#37a3b0; box-shadow:0 0 0 3px rgba(55,163,176,.15); }
  button { width:100%; margin-top:22px; padding:14px; border:none; border-radius:10px;
           background:var(--grad); color:#fff; font-size:1.05rem; font-weight:700;
           cursor:pointer; }
  .err { background:#fdecec; color:#b12727; border:1px solid #f6c9c9; border-radius:10px;
         padding:10px 12px; margin-top:16px; font-size:.85rem; }
  .who { font-size:.8rem; color:#5b6673; margin-top:10px; }
  .foot { margin-top:18px; font-size:.72rem; color:#95a0ad; text-align:center; }
</style>
</head>
<body>
  <div class="card">
    <h1>Connect AI</h1>
    <p class="sub"><b>{{ client_name }}</b> is requesting access to your Pixiware agency data.</p>
    <div class="scope">
      Read-only access to <b>your clients, chats, PixiVault documents,
      delivery dates and form submissions</b>. It cannot send messages or change anything.
    </div>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post" action="{{ base }}/oauth/authorize">
      {% for k, v in hidden.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
      <label for="email">Agency email</label>
      <input id="email" name="email" type="email" value="{{ prefill_email }}" autocomplete="username" required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Approve access</button>
    </form>
    <p class="foot">You are approving on behalf of your agency. Only agency accounts can connect.</p>
  </div>
</body>
</html>
"""


def _authorize_params(source):
    return {
        'response_type': source.get('response_type', 'code'),
        'client_id': source.get('client_id', ''),
        'redirect_uri': source.get('redirect_uri', ''),
        'code_challenge': source.get('code_challenge', ''),
        'code_challenge_method': source.get('code_challenge_method', ''),
        'state': source.get('state', ''),
        'scope': source.get('scope', DEFAULT_SCOPE),
        'resource': source.get('resource', ''),
    }


def _redirect_with_error(redirect_uri, state, error, description):
    from urllib.parse import urlencode
    sep = '&' if '?' in redirect_uri else '?'
    params = {'error': error, 'error_description': description}
    if state:
        params['state'] = state
    return redirect(f'{redirect_uri}{sep}{urlencode(params)}')


@mcp_bp.route('/oauth/authorize', methods=['GET', 'OPTIONS'])
def oauth_authorize():
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))

    params = _authorize_params(request.args)
    with _db() as conn:
        with conn.cursor() as cur:
            client = _load_client(cur, params['client_id'])

    if not client:
        return jsonify({'error': 'invalid_client',
                        'error_description': 'Unknown client_id'}), 400
    if params['redirect_uri'] not in client['redirect_uris']:
        return jsonify({'error': 'invalid_request',
                        'error_description': 'redirect_uri not registered'}), 400
    if params['response_type'] != 'code':
        return _redirect_with_error(params['redirect_uri'], params['state'],
                                    'unsupported_response_type', 'Only code is supported')

    return render_template_string(
        _AUTHORIZE_PAGE,
        base=_base_url(),
        client_name=_client_name(params['client_id']),
        hidden=params,
        prefill_email='',
        error=None,
    )


def _client_name(client_id):
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT client_name FROM public.mcp_oauth_clients WHERE client_id = %s',
                            (client_id,))
                row = cur.fetchone()
                return (row[0] if row and row[0] else 'An AI assistant')
    except Exception:
        return 'An AI assistant'


@mcp_bp.route('/oauth/authorize', methods=['POST'])
def oauth_authorize_submit():
    params = _authorize_params(request.form)
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''

    with _db() as conn:
        with conn.cursor() as cur:
            client = _load_client(cur, params['client_id'])
            if not client or params['redirect_uri'] not in (client['redirect_uris'] if client else []):
                return jsonify({'error': 'invalid_request',
                                'error_description': 'Invalid client or redirect_uri'}), 400

            # Authenticate the agency operator.
            cur.execute(
                'SELECT id, password, role FROM public.users WHERE email = %s',
                (email,),
            )
            user = cur.fetchone()
            invalid = (
                not user
                or not portal.check_password_hash(user[1], password)
            )
            if invalid:
                return render_template_string(
                    _AUTHORIZE_PAGE, base=_base_url(),
                    client_name=_client_name(params['client_id']),
                    hidden=params, prefill_email=email,
                    error='Incorrect email or password.',
                )
            if not portal.is_agency_role(user[2]):
                return render_template_string(
                    _AUTHORIZE_PAGE, base=_base_url(),
                    client_name=_client_name(params['client_id']),
                    hidden=params, prefill_email=email,
                    error='Only agency accounts can connect an AI assistant.',
                )

            user_id = int(user[0])
            code = 'code_' + secrets.token_urlsafe(24)
            cur.execute(
                '''
                INSERT INTO public.mcp_oauth_codes
                    (code, client_id, user_id, redirect_uri, code_challenge,
                     code_challenge_method, scope, resource, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    code, params['client_id'], user_id, params['redirect_uri'],
                    params['code_challenge'] or None,
                    params['code_challenge_method'] or None,
                    params['scope'] or DEFAULT_SCOPE,
                    params['resource'] or None,
                    _now() + AUTH_CODE_TTL,
                ),
            )

    from urllib.parse import urlencode
    sep = '&' if '?' in params['redirect_uri'] else '?'
    out = {'code': code}
    if params['state']:
        out['state'] = params['state']
    return redirect(f"{params['redirect_uri']}{sep}{urlencode(out)}")


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #
def _verify_pkce(verifier, challenge, method):
    if not challenge:
        return True
    if not verifier:
        return False
    if (method or 'plain') == 'S256':
        digest = hashlib.sha256(verifier.encode('ascii')).digest()
        calc = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return secrets.compare_digest(calc, challenge)
    return secrets.compare_digest(verifier, challenge)


def _client_credentials():
    """Return (client_id, client_secret) from Basic header or POST body."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Basic '):
        try:
            raw = base64.b64decode(auth[6:]).decode('utf-8')
            cid, _, csec = raw.partition(':')
            return cid, csec
        except Exception:
            pass
    return request.form.get('client_id'), request.form.get('client_secret')


def _token_error(error, description, status=400):
    return jsonify({'error': error, 'error_description': description}), status


@mcp_bp.route('/oauth/token', methods=['POST', 'OPTIONS'])
def oauth_token():
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))

    grant_type = request.form.get('grant_type')
    body_cid, body_secret = _client_credentials()

    if grant_type == 'authorization_code':
        code = request.form.get('code')
        redirect_uri = request.form.get('redirect_uri')
        verifier = request.form.get('code_verifier')
        if not code:
            return _token_error('invalid_request', 'code is required')

        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''SELECT client_id, user_id, redirect_uri, code_challenge,
                              code_challenge_method, scope, expires_at, used
                       FROM public.mcp_oauth_codes WHERE code = %s FOR UPDATE''',
                    (code,),
                )
                row = cur.fetchone()
                if not row:
                    return _token_error('invalid_grant', 'Unknown authorization code')
                (c_client, c_user, c_redirect, c_challenge,
                 c_method, c_scope, c_expires, c_used) = row
                if c_used:
                    return _token_error('invalid_grant', 'Authorization code already used')
                if c_expires < _now():
                    return _token_error('invalid_grant', 'Authorization code expired')

                client = _load_client(cur, c_client)
                if not client:
                    return _token_error('invalid_client', 'Unknown client')

                # Confidential clients must present their secret; public clients
                # must present a valid PKCE verifier.
                if client['client_secret']:
                    if not body_secret or not secrets.compare_digest(body_secret, client['client_secret']):
                        return _token_error('invalid_client', 'Bad client secret', 401)
                if body_cid and body_cid != c_client:
                    return _token_error('invalid_grant', 'client_id mismatch')
                if redirect_uri and redirect_uri != c_redirect:
                    return _token_error('invalid_grant', 'redirect_uri mismatch')
                if not _verify_pkce(verifier, c_challenge, c_method):
                    return _token_error('invalid_grant', 'PKCE verification failed')

                cur.execute('UPDATE public.mcp_oauth_codes SET used = true WHERE code = %s', (code,))
                access, refresh = _issue_token(cur, c_client, int(c_user), c_scope or DEFAULT_SCOPE)

        return _token_response(access, refresh, c_scope or DEFAULT_SCOPE)

    if grant_type == 'refresh_token':
        refresh_token = request.form.get('refresh_token')
        if not refresh_token:
            return _token_error('invalid_request', 'refresh_token is required')
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''SELECT client_id, user_id, scope FROM public.mcp_oauth_tokens
                       WHERE refresh_token = %s FOR UPDATE''',
                    (refresh_token,),
                )
                row = cur.fetchone()
                if not row:
                    return _token_error('invalid_grant', 'Unknown refresh token')
                r_client, r_user, r_scope = row
                client = _load_client(cur, r_client)
                if client and client['client_secret']:
                    if not body_secret or not secrets.compare_digest(body_secret, client['client_secret']):
                        return _token_error('invalid_client', 'Bad client secret', 401)
                # Rotate: delete the old row, issue a fresh pair.
                cur.execute('DELETE FROM public.mcp_oauth_tokens WHERE refresh_token = %s',
                            (refresh_token,))
                access, refresh = _issue_token(cur, r_client, int(r_user), r_scope or DEFAULT_SCOPE)
        return _token_response(access, refresh, r_scope or DEFAULT_SCOPE)

    return _token_error('unsupported_grant_type', f'Unsupported grant_type: {grant_type}')


def _issue_token(cur, client_id, user_id, scope):
    access = 'at_' + _token()
    refresh = 'rt_' + _token()
    cur.execute(
        '''INSERT INTO public.mcp_oauth_tokens
               (access_token, refresh_token, client_id, user_id, scope, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s)''',
        (access, refresh, client_id, user_id, scope, _now() + ACCESS_TOKEN_TTL),
    )
    return access, refresh


def _token_response(access, refresh, scope):
    return jsonify({
        'access_token': access,
        'token_type': 'Bearer',
        'expires_in': int(ACCESS_TOKEN_TTL.total_seconds()),
        'refresh_token': refresh,
        'scope': scope,
    })


# --------------------------------------------------------------------------- #
# Bearer auth for the MCP endpoint
# --------------------------------------------------------------------------- #
def _authenticate(cur):
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    token = auth[7:].strip()
    cur.execute(
        'SELECT user_id, expires_at FROM public.mcp_oauth_tokens WHERE access_token = %s',
        (token,),
    )
    row = cur.fetchone()
    if not row:
        return None
    if row[1] and row[1] < _now():
        return None
    return int(row[0])


def _unauthorized():
    base = _base_url()
    resp = jsonify({
        'jsonrpc': '2.0',
        'error': {'code': -32001, 'message': 'Unauthorized'},
        'id': None,
    })
    resp.status_code = 401
    resp.headers['WWW-Authenticate'] = (
        f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
    )
    return resp


# --------------------------------------------------------------------------- #
# Read-only data access (all scoped to the authenticated agency)
# --------------------------------------------------------------------------- #
def _agency_context(cur, agency_id):
    cur.execute(
        'SELECT COALESCE(name, email), email, role, chats FROM public.users WHERE id = %s',
        (agency_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        'id': agency_id,
        'name': row[0],
        'email': row[1],
        'role': row[2],
        'client_ids': portal.parse_chat_ids(row[3]),
    }


def _client_rows(cur, agency_id):
    ctx = _agency_context(cur, agency_id)
    ids = ctx['client_ids'] if ctx else []
    if not ids:
        return []
    cur.execute(
        '''SELECT id, COALESCE(name, email), email, COALESCE(pro_user, false),
                  COALESCE(progress, 0), delivery_date, site_url
           FROM public.users
           WHERE id = ANY(%s)
           ORDER BY COALESCE(name, email)''',
        (ids,),
    )
    return cur.fetchall()


def _require_client(cur, agency_id, client_id):
    ctx = _agency_context(cur, agency_id)
    if not ctx or int(client_id) not in ctx['client_ids']:
        raise ToolError(f'Client {client_id} is not one of your clients')
    return True


class ToolError(Exception):
    pass


def _serialize_attachments(raw):
    out = []
    for att in portal.parse_attachments(raw):
        out.append({
            'name': att.get('name'),
            'mime': att.get('mime'),
            'size': att.get('size'),
            'path': att.get('path'),
        })
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def tool_whoami(cur, agency_id, args):
    ctx = _agency_context(cur, agency_id)
    return {
        'id': ctx['id'],
        'name': ctx['name'],
        'email': ctx['email'],
        'role': ctx['role'],
        'client_count': len(ctx['client_ids']),
    }


def tool_list_clients(cur, agency_id, args):
    rows = _client_rows(cur, agency_id)
    ids = [r[0] for r in rows]
    stats = {}
    if ids:
        cur.execute(
            '''
            SELECT other, COUNT(*), MAX(created_at) FROM (
                SELECT CASE WHEN sender_id = %s THEN receiver_id ELSE sender_id END AS other,
                       created_at
                FROM public.messages
                WHERE sender_id = %s OR receiver_id = %s
            ) t
            WHERE other = ANY(%s)
            GROUP BY other
            ''',
            (agency_id, agency_id, agency_id, ids),
        )
        for other, count, last in cur.fetchall():
            stats[other] = (count, last)
    clients = []
    for cid, name, email, pro, progress, delivery, site in rows:
        count, last = stats.get(cid, (0, None))
        clients.append({
            'client_id': cid,
            'name': name,
            'email': email,
            'pro_user': bool(pro),
            'progress': progress,
            'delivery_date': _iso(delivery),
            'site_url': site,
            'message_count': count,
            'last_message_at': _iso(last),
        })
    return {'clients': clients, 'count': len(clients)}


def tool_get_client(cur, agency_id, args):
    client_id = _need_int(args, 'client_id')
    _require_client(cur, agency_id, client_id)
    cur.execute(
        '''SELECT id, COALESCE(name, email), email, COALESCE(pro_user, false),
                  COALESCE(progress, 0), delivery_date, site_url, created_at
           FROM public.users WHERE id = %s''',
        (client_id,),
    )
    r = cur.fetchone()
    if not r:
        raise ToolError('Client not found')
    cur.execute(
        '''SELECT COUNT(*) FROM public.messages
           WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)''',
        (agency_id, client_id, client_id, agency_id),
    )
    msg_count = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM public.vault_items WHERE client_id = %s AND kind = %s',
                (client_id, 'file'))
    vault_files = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM public.form_submissions WHERE agency_id = %s AND client_id = %s',
                (agency_id, client_id))
    submissions = cur.fetchone()[0]
    return {
        'client_id': r[0],
        'name': r[1],
        'email': r[2],
        'pro_user': bool(r[3]),
        'progress': r[4],
        'delivery_date': _iso(r[5]),
        'site_url': r[6],
        'created_at': _iso(r[7]),
        'message_count': msg_count,
        'vault_file_count': vault_files,
        'form_submission_count': submissions,
    }


def tool_get_messages(cur, agency_id, args):
    client_id = _need_int(args, 'client_id')
    _require_client(cur, agency_id, client_id)
    limit = _opt_int(args, 'limit', 50)
    limit = max(1, min(limit, 500))
    cur.execute(
        '''SELECT id, sender_id, body, COALESCE(attachments, '[]'::jsonb),
                  form_item_id, created_at
           FROM public.messages
           WHERE (sender_id = %s AND receiver_id = %s)
              OR (sender_id = %s AND receiver_id = %s)
           ORDER BY created_at DESC
           LIMIT %s''',
        (agency_id, client_id, client_id, agency_id, limit),
    )
    rows = cur.fetchall()
    form_names = _form_names(cur, [r[4] for r in rows if r[4]])
    messages = []
    for msg_id, sender_id, body, attachments, form_item_id, created in reversed(rows):
        msg = {
            'id': msg_id,
            'from': 'agency' if sender_id == agency_id else 'client',
            'body': body or '',
            'created_at': _iso(created),
            'attachments': _serialize_attachments(attachments),
        }
        if form_item_id:
            msg['form'] = {'form_item_id': form_item_id,
                           'name': form_names.get(form_item_id)}
        messages.append(msg)
    return {'client_id': client_id, 'count': len(messages), 'messages': messages}


def tool_get_vault(cur, agency_id, args):
    client_id = _need_int(args, 'client_id')
    _require_client(cur, agency_id, client_id)
    cur.execute(
        '''SELECT id, parent_id, kind, name, mime, size_bytes, created_at
           FROM public.vault_items
           WHERE client_id = %s
           ORDER BY created_at ASC''',
        (client_id,),
    )
    items = []
    for iid, parent, kind, name, mime, size, created in cur.fetchall():
        item = {
            'id': iid,
            'parent_id': parent,
            'kind': kind,
            'name': name,
            'created_at': _iso(created),
        }
        if kind == 'file':
            item['mime'] = mime
            item['size'] = size
        items.append(item)
    return {
        'client_id': client_id,
        'count': len(items),
        'items': items,
        'tree': _build_tree(items),
    }


def tool_list_delivery_dates(cur, agency_id, args):
    rows = _client_rows(cur, agency_id)
    out = []
    for cid, name, email, pro, progress, delivery, site in rows:
        out.append({
            'client_id': cid,
            'name': name,
            'delivery_date': _iso(delivery),
            'progress': progress,
        })
    return {'deliveries': out, 'count': len(out)}


def tool_get_form_submissions(cur, agency_id, args):
    client_id = _need_int(args, 'client_id')
    _require_client(cur, agency_id, client_id)
    cur.execute(
        '''SELECT fs.id, fi.name, fs.answers, fs.created_at, fs.submitted_by
           FROM public.form_submissions fs
           LEFT JOIN public.form_items fi ON fi.id = fs.form_item_id
           WHERE fs.agency_id = %s AND fs.client_id = %s
           ORDER BY fs.created_at DESC''',
        (agency_id, client_id),
    )
    subs = []
    for sid, form_name, answers, created, submitted_by in cur.fetchall():
        subs.append({
            'id': sid,
            'form_name': form_name,
            'answers': answers,
            'created_at': _iso(created),
            'submitted_by': 'client' if submitted_by == client_id else 'agency',
        })
    return {'client_id': client_id, 'count': len(subs), 'submissions': subs}


def tool_search_messages(cur, agency_id, args):
    query = (args.get('query') or '').strip()
    if not query:
        raise ToolError('query is required')
    ctx = _agency_context(cur, agency_id)
    ids = ctx['client_ids'] if ctx else []
    if not ids:
        return {'query': query, 'count': 0, 'matches': []}
    client_filter = args.get('client_id')
    if client_filter is not None:
        _require_client(cur, agency_id, client_filter)
        ids = [int(client_filter)]
    limit = _opt_int(args, 'limit', 50)
    limit = max(1, min(limit, 200))
    cur.execute(
        '''SELECT id, sender_id, receiver_id, body, created_at
           FROM public.messages
           WHERE body ILIKE %s
             AND ((sender_id = %s AND receiver_id = ANY(%s))
               OR (receiver_id = %s AND sender_id = ANY(%s)))
           ORDER BY created_at DESC
           LIMIT %s''',
        (f'%{query}%', agency_id, ids, agency_id, ids, limit),
    )
    matches = []
    for mid, sender, receiver, body, created in cur.fetchall():
        other = receiver if sender == agency_id else sender
        matches.append({
            'message_id': mid,
            'client_id': other,
            'from': 'agency' if sender == agency_id else 'client',
            'body': body or '',
            'created_at': _iso(created),
        })
    return {'query': query, 'count': len(matches), 'matches': matches}


def tool_list_forms(cur, agency_id, args):
    cur.execute(
        '''SELECT id, parent_id, kind, name, schema, created_at
           FROM public.form_items
           WHERE agency_id = %s
           ORDER BY created_at ASC''',
        (agency_id,),
    )
    items = []
    for fid, parent, kind, name, schema, created in cur.fetchall():
        item = {
            'id': fid,
            'parent_id': parent,
            'kind': kind,
            'name': name,
            'created_at': _iso(created),
        }
        if kind == 'form':
            fields = schema.get('fields', []) if isinstance(schema, dict) else []
            item['field_count'] = len(fields)
        items.append(item)
    return {'count': len(items), 'items': items, 'tree': _build_tree(items)}


def tool_get_form(cur, agency_id, args):
    form_id = _need_int(args, 'form_id')
    cur.execute(
        'SELECT id, name, kind, schema, created_at FROM public.form_items WHERE id = %s AND agency_id = %s',
        (form_id, agency_id),
    )
    r = cur.fetchone()
    if not r:
        raise ToolError('Form not found or not owned by your agency')
    if r[2] != 'form':
        raise ToolError('That item is a folder, not a form')
    schema = r[3] if isinstance(r[3], dict) else {}
    fields = schema.get('fields', []) if isinstance(schema, dict) else []
    return {
        'form_id': r[0],
        'name': r[1],
        'fields': fields,
        'field_count': len(fields),
        'created_at': _iso(r[4]),
    }


def tool_list_form_submissions(cur, agency_id, args):
    ids = None
    if args.get('client_id') is not None:
        _require_client(cur, agency_id, args['client_id'])
        ids = [int(args['client_id'])]
    if ids is None:
        cur.execute(
            '''SELECT fs.id, fs.client_id, COALESCE(u.name, u.email), fi.name,
                      fs.answers, fs.created_at, fs.submitted_by
               FROM public.form_submissions fs
               LEFT JOIN public.form_items fi ON fi.id = fs.form_item_id
               LEFT JOIN public.users u ON u.id = fs.client_id
               WHERE fs.agency_id = %s
               ORDER BY fs.created_at DESC''',
            (agency_id,),
        )
    else:
        cur.execute(
            '''SELECT fs.id, fs.client_id, COALESCE(u.name, u.email), fi.name,
                      fs.answers, fs.created_at, fs.submitted_by
               FROM public.form_submissions fs
               LEFT JOIN public.form_items fi ON fi.id = fs.form_item_id
               LEFT JOIN public.users u ON u.id = fs.client_id
               WHERE fs.agency_id = %s AND fs.client_id = ANY(%s)
               ORDER BY fs.created_at DESC''',
            (agency_id, ids),
        )
    subs = []
    for sid, cid, cname, fname, answers, created, submitted_by in cur.fetchall():
        subs.append({
            'id': sid,
            'client_id': cid,
            'client_name': cname,
            'form_name': fname,
            'answers': answers,
            'created_at': _iso(created),
            'submitted_by': 'client' if submitted_by == cid else 'agency',
        })
    return {'count': len(subs), 'submissions': subs}


# ---- tool helpers --------------------------------------------------------- #
def _need_int(args, key):
    if key not in args or args[key] is None:
        raise ToolError(f'{key} is required')
    try:
        return int(args[key])
    except (TypeError, ValueError):
        raise ToolError(f'{key} must be an integer')


def _opt_int(args, key, default):
    if args.get(key) is None:
        return default
    try:
        return int(args[key])
    except (TypeError, ValueError):
        return default


def _form_names(cur, form_item_ids):
    ids = [i for i in set(form_item_ids) if i]
    if not ids:
        return {}
    cur.execute('SELECT id, name FROM public.form_items WHERE id = ANY(%s)', (ids,))
    return {row[0]: row[1] for row in cur.fetchall()}


def _build_tree(items):
    by_parent = {}
    for it in items:
        by_parent.setdefault(it['parent_id'], []).append(it)

    def build(parent_id):
        nodes = []
        for it in by_parent.get(parent_id, []):
            node = {k: it[k] for k in it}
            if it['kind'] == 'folder':
                node['children'] = build(it['id'])
            nodes.append(node)
        return nodes

    return build(None)


TOOLS = [
    {
        'name': 'whoami',
        'description': 'Return the authenticated agency profile (name, email, and how many clients it has). Call this first to confirm the connection.',
        'handler': tool_whoami,
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'list_clients',
        'description': 'List every client belonging to the agency, with progress %, delivery date, website URL, plan, message count and last activity. This is the entry point for discovering client_ids.',
        'handler': tool_list_clients,
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'get_client',
        'description': 'Full detail for one client: profile, progress %, delivery date, website URL, plan, and counts of messages, PixiVault files and form submissions.',
        'handler': tool_get_client,
        'inputSchema': {
            'type': 'object',
            'properties': {'client_id': {'type': 'integer', 'description': 'The client id (from list_clients).'}},
            'required': ['client_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_messages',
        'description': "Read the full chat conversation between the agency and a client, oldest-to-newest. Each message notes whether it is from 'agency' or 'client', its timestamp, and any attachments or attached forms.",
        'handler': tool_get_messages,
        'inputSchema': {
            'type': 'object',
            'properties': {
                'client_id': {'type': 'integer', 'description': 'The client id.'},
                'limit': {'type': 'integer', 'description': 'Max messages (most recent), default 50, max 500.'},
            },
            'required': ['client_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_vault',
        'description': "Read a client's PixiVault: all documents and folders, returned both as a flat list and a nested tree. Files include mime type and size.",
        'handler': tool_get_vault,
        'inputSchema': {
            'type': 'object',
            'properties': {'client_id': {'type': 'integer', 'description': 'The client id.'}},
            'required': ['client_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'list_delivery_dates',
        'description': 'Get the delivery date and progress % for every client in one call — useful for status overviews and deadline tracking.',
        'handler': tool_list_delivery_dates,
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'get_form_submissions',
        'description': "Read all form submissions a client has completed, including the form name and the submitted answers.",
        'handler': tool_get_form_submissions,
        'inputSchema': {
            'type': 'object',
            'properties': {'client_id': {'type': 'integer', 'description': 'The client id.'}},
            'required': ['client_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'search_messages',
        'description': 'Full-text search across all of the agency\'s client conversations (case-insensitive). Optionally restrict to a single client_id.',
        'handler': tool_search_messages,
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Text to search for in message bodies.'},
                'client_id': {'type': 'integer', 'description': 'Optional: restrict to one client.'},
                'limit': {'type': 'integer', 'description': 'Max results, default 50, max 200.'},
            },
            'required': ['query'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'list_forms',
        'description': "List the agency's form library (the Form Builder): all folders and form templates, as a flat list and a nested tree. Forms include a field_count. Use get_form for the full field definitions.",
        'handler': tool_list_forms,
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'get_form',
        'description': 'Get one form template in full: every field with its type (text/textarea/file), label, required flag and placeholder. Use list_forms to discover form_ids.',
        'handler': tool_get_form,
        'inputSchema': {
            'type': 'object',
            'properties': {'form_id': {'type': 'integer', 'description': 'The form template id (from list_forms).'}},
            'required': ['form_id'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'list_form_submissions',
        'description': "List every form submission across all the agency's clients (form name, which client submitted it, and the answers). Optionally restrict to one client_id.",
        'handler': tool_list_form_submissions,
        'inputSchema': {
            'type': 'object',
            'properties': {'client_id': {'type': 'integer', 'description': 'Optional: restrict to one client.'}},
            'additionalProperties': False,
        },
    },
]

TOOLS_BY_NAME = {t['name']: t for t in TOOLS}


def _public_tool(t):
    return {'name': t['name'], 'description': t['description'], 'inputSchema': t['inputSchema']}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP endpoint
# --------------------------------------------------------------------------- #
def _rpc_result(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _rpc_error(req_id, code, message):
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


def _handle_rpc(message, agency_id):
    """Handle one JSON-RPC message. Returns a dict response, or None for notifications."""
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, 'Invalid Request')
    method = message.get('method')
    req_id = message.get('id')
    params = message.get('params') or {}
    is_notification = 'id' not in message

    if method == 'initialize':
        requested = params.get('protocolVersion') or MCP_PROTOCOL_VERSION
        return _rpc_result(req_id, {
            'protocolVersion': requested,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
            'instructions': (
                'Read-only access to a Pixiware agency. Start with whoami and '
                'list_clients to discover client_ids, then use get_client, '
                'get_messages, get_vault, list_delivery_dates, get_form_submissions '
                'and search_messages. Every tool is automatically scoped to your agency.'
            ),
        })

    if method in ('notifications/initialized', 'notifications/cancelled'):
        return None

    if method == 'ping':
        return _rpc_result(req_id, {})

    if method == 'tools/list':
        return _rpc_result(req_id, {'tools': [_public_tool(t) for t in TOOLS]})

    if method in ('resources/list',):
        return _rpc_result(req_id, {'resources': []})
    if method in ('prompts/list',):
        return _rpc_result(req_id, {'prompts': []})

    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        tool = TOOLS_BY_NAME.get(name)
        if not tool:
            return _rpc_result(req_id, {
                'content': [{'type': 'text', 'text': f'Unknown tool: {name}'}],
                'isError': True,
            })
        try:
            with _db() as conn:
                with conn.cursor() as cur:
                    data = tool['handler'](cur, agency_id, args)
        except ToolError as exc:
            return _rpc_result(req_id, {
                'content': [{'type': 'text', 'text': str(exc)}],
                'isError': True,
            })
        except Exception as exc:  # pragma: no cover - defensive
            return _rpc_result(req_id, {
                'content': [{'type': 'text', 'text': f'Internal error: {exc}'}],
                'isError': True,
            })
        return _rpc_result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(data, default=str)}],
            'structuredContent': data,
            'isError': False,
        })

    if is_notification:
        return None
    return _rpc_error(req_id, -32601, f'Method not found: {method}')


@mcp_bp.route('/mcp', methods=['POST', 'GET', 'OPTIONS'])
def mcp_endpoint():
    if request.method == 'OPTIONS':
        return _cors(Response(status=204))
    if request.method == 'GET':
        # No server-initiated SSE stream in this stateless implementation.
        resp = jsonify({'error': 'Use POST for JSON-RPC'})
        resp.status_code = 405
        resp.headers['Allow'] = 'POST, OPTIONS'
        return resp

    with _db() as conn:
        with conn.cursor() as cur:
            agency_id = _authenticate(cur)
    if agency_id is None:
        return _unauthorized()

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(_rpc_error(None, -32700, 'Parse error')), 400

    if isinstance(payload, list):
        responses = []
        for msg in payload:
            r = _handle_rpc(msg, agency_id)
            if r is not None:
                responses.append(r)
        if not responses:
            return Response(status=202)
        return jsonify(responses)

    response = _handle_rpc(payload, agency_id)
    if response is None:
        return Response(status=202)
    return jsonify(response)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_mcp(flask_app, portal_module):
    """Register the MCP blueprint. ``portal_module`` is the main app module,
    passed in to avoid a circular import when app.py runs as __main__."""
    global portal
    portal = portal_module
    flask_app.register_blueprint(mcp_bp)


def mcp_url_for(base=None):
    return f'{(base or _base_url())}/mcp'
