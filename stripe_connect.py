"""
Read-only Stripe Connect billing visibility for agencies.

An agency connects its *existing* Stripe account in one click (OAuth, scope
read_only, registered as an Extension). We never process payments, never write
to Stripe, never hold funds, and never touch the agency's own API keys. All
portal + MCP reads hit the local `invoice_cache`, never Stripe, in the request
path.

READ-ONLY IS A HARD ARCHITECTURAL CONSTRAINT. Do not add write paths (create /
send / void / refund / metadata write) without an explicit fresh decision.

Naming: everything here is Connect-specific so it can never collide with the
platform's own subscription billing (users.stripe_customer_id, /stripe/webhook,
STRIPE_WEBHOOK_SECRET). This module uses stripe_account_id /
stripe_connect_customer_id / /stripe/connect/webhook / STRIPE_CONNECT_WEBHOOK_SECRET.
"""

import os
import re
import json
import secrets
import threading
import difflib
from datetime import datetime, timezone, timedelta

import stripe
from flask import (
    Blueprint, request, session, redirect, url_for, jsonify, abort,
    render_template, render_template_string,
)

portal = None  # the main app module, injected via register_stripe()

stripe_bp = Blueprint('stripe_bp', __name__)

FUZZY_THRESHOLD = 0.82
BACKFILL_MONTHS = 24


# --------------------------------------------------------------------------- #
# Config (all from env — never guessed; the human registers the Connect app)
# --------------------------------------------------------------------------- #
def _connect_client_id():
    return os.environ.get('STRIPE_CONNECT_CLIENT_ID')


def _connect_webhook_secret():
    return os.environ.get('STRIPE_CONNECT_WEBHOOK_SECRET')


def _base_url():
    return (os.environ.get('PORTAL_BASE_URL')
            or os.environ.get('APP_URL')
            or request.url_root.rstrip('/')).rstrip('/')


def is_configured():
    return bool(_connect_client_id())


class StripeNotConnected(Exception):
    pass


def stripe_params(stripe_account_id):
    """The ONLY way to build Stripe request params. Every Connect call must
    spread this so `stripe_account` is never forgotten — a call without it reads
    OUR account instead of the agency's, a silent data-leak-shaped bug."""
    if not stripe_account_id:
        raise StripeNotConnected()
    return {'stripe_account': stripe_account_id}


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _agency_account(cur, agency_id):
    cur.execute('SELECT stripe_account_id FROM public.users WHERE id = %s', (agency_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _current_agency_id():
    """Return the logged-in user's id iff they are an agency, else None."""
    uid = session.get('user_id')
    if not uid:
        return None
    try:
        with portal.get_db_connection() as conn:
            with conn.cursor() as cur:
                role = portal.get_user_role(cur, uid)
        return int(uid) if portal.is_agency_role(role) else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# OAuth connect flow
# --------------------------------------------------------------------------- #
@stripe_bp.route('/stripe/connect')
def stripe_connect_start():
    agency_id = _current_agency_id()
    if agency_id is None:
        return redirect(url_for('sign_in'))
    if not is_configured():
        return redirect(url_for('settings', billing='unconfigured'))

    from urllib.parse import urlencode
    state = secrets.token_urlsafe(32)
    session['stripe_oauth_state'] = state
    params = {
        'response_type': 'code',
        'client_id': _connect_client_id(),
        'scope': 'read_only',
        'state': state,
        'redirect_uri': f'{_base_url()}/stripe/callback',
    }
    return redirect('https://connect.stripe.com/oauth/authorize?' + urlencode(params))


@stripe_bp.route('/stripe/callback')
def stripe_callback():
    agency_id = _current_agency_id()
    if agency_id is None:
        return redirect(url_for('sign_in'))

    # CSRF: pop + constant-time compare.
    expected = session.pop('stripe_oauth_state', None)
    got = request.args.get('state')
    if not expected or not got or not secrets.compare_digest(expected, got):
        abort(403)

    if request.args.get('error'):
        # access_denied == user clicked cancel — not an error page.
        return redirect(url_for('settings', billing='cancelled'))

    code = request.args.get('code')
    if not code:
        return redirect(url_for('settings', billing='error'))

    portal.configure_stripe()
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            # Per-agency lock around the exchange. The token endpoint is NOT
            # idempotent — consuming a code twice REVOKES the connection — so a
            # double-fired callback / refresh / retry must not re-exchange.
            cur.execute('SELECT stripe_account_id FROM public.users WHERE id = %s FOR UPDATE', (agency_id,))
            row = cur.fetchone()
            if row and row[0]:
                # Already connected: a repeat callback. Short-circuit — do NOT
                # call Stripe again (that would revoke the live connection).
                return redirect(url_for('stripe_bp.stripe_reconcile'))
            try:
                resp = stripe.OAuth.token(grant_type='authorization_code', code=code)
            except Exception as exc:  # noqa: BLE001
                print(f'stripe oauth exchange failed: {exc}')
                return redirect(url_for('settings', billing='error'))

            # Persist ONLY the account id (+ livemode for sanity). access_token /
            # refresh_token / publishable_key are deprecated — we authenticate
            # with our platform key + Stripe-Account header. Do not store them.
            account_id = resp['stripe_user_id']
            livemode = bool(resp.get('livemode'))
            cur.execute(
                'UPDATE public.users SET stripe_account_id = %s, stripe_connected_at = now(), '
                'stripe_livemode = %s WHERE id = %s',
                (account_id, livemode, agency_id),
            )

    _enqueue_initial_sync(agency_id)
    return redirect(url_for('stripe_bp.stripe_reconcile'))


@stripe_bp.route('/stripe/disconnect', methods=['POST'])
def stripe_disconnect():
    agency_id = _current_agency_id()
    if agency_id is None:
        return jsonify({'error': 'forbidden'}), 403

    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            account_id = _agency_account(cur, agency_id)

    if account_id:
        portal.configure_stripe()
        try:
            stripe.OAuth.deauthorize(client_id=_connect_client_id(), stripe_user_id=account_id)
        except Exception as exc:  # noqa: BLE001 — already revoked is fine
            print(f'stripe deauthorize note: {exc}')

    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE public.users SET stripe_account_id = NULL, stripe_connected_at = NULL, '
                'stripe_livemode = NULL WHERE id = %s',
                (agency_id,),
            )
            cur.execute('DELETE FROM public.invoice_cache WHERE agency_id = %s', (agency_id,))
            # Keep stripe_connect_customer_id: if they reconnect, the mapping
            # still resolves and they skip reconciliation.
    return jsonify({'ok': True})


def _mark_connection_dead(agency_id):
    """A Stripe permission error means the agency revoked us from their own
    dashboard. Tear the connection down so the UI prompts a reconnect instead
    of 500ing on every call."""
    try:
        with portal.get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE public.users SET stripe_account_id = NULL, stripe_connected_at = NULL, '
                    'stripe_livemode = NULL WHERE id = %s',
                    (agency_id,),
                )
    except Exception as exc:  # noqa: BLE001
        print(f'could not mark connection dead: {exc}')


# --------------------------------------------------------------------------- #
# Invoice cache upsert + sync
# --------------------------------------------------------------------------- #
def _to_dt(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _obj_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _resolve_client_id(cur, agency_id, customer_id):
    if not customer_id:
        return None
    cur.execute(
        'SELECT id FROM public.users WHERE agency_id = %s AND stripe_connect_customer_id = %s',
        (agency_id, customer_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def upsert_invoice(cur, agency_id, inv, event_ts=None):
    """Idempotent upsert of one invoice into the cache. `event_ts` (epoch) lets a
    webhook skip if the row was already written after this event was created
    (out-of-order / retried delivery)."""
    invoice_id = _obj_get(inv, 'id')
    if not invoice_id:
        return
    customer_id = _obj_get(inv, 'customer')
    # Out-of-order guard.
    if event_ts is not None:
        cur.execute(
            'SELECT synced_at FROM public.invoice_cache WHERE agency_id = %s AND stripe_invoice_id = %s',
            (agency_id, invoice_id),
        )
        existing = cur.fetchone()
        if existing and existing[0] and existing[0] > _to_dt(event_ts):
            return

    client_id = _resolve_client_id(cur, agency_id, customer_id)
    cur.execute(
        '''
        INSERT INTO public.invoice_cache
            (agency_id, client_id, stripe_invoice_id, stripe_customer_id, number, status,
             amount_due, amount_paid, currency, due_date, finalized_at, hosted_invoice_url, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (agency_id, stripe_invoice_id) DO UPDATE SET
            client_id = EXCLUDED.client_id,
            stripe_customer_id = EXCLUDED.stripe_customer_id,
            number = EXCLUDED.number,
            status = EXCLUDED.status,
            amount_due = EXCLUDED.amount_due,
            amount_paid = EXCLUDED.amount_paid,
            currency = EXCLUDED.currency,
            due_date = EXCLUDED.due_date,
            finalized_at = EXCLUDED.finalized_at,
            hosted_invoice_url = EXCLUDED.hosted_invoice_url,
            synced_at = now()
        ''',
        (
            agency_id, client_id, invoice_id, customer_id,
            _obj_get(inv, 'number'),
            _obj_get(inv, 'status') or 'draft',
            int(_obj_get(inv, 'amount_due') or 0),
            int(_obj_get(inv, 'amount_paid') or 0),
            (_obj_get(inv, 'currency') or 'usd'),
            _to_dt(_obj_get(inv, 'due_date')),
            _to_dt((_obj_get(inv, 'status_transitions') or {}).get('finalized_at') if isinstance(_obj_get(inv, 'status_transitions'), dict) else None),
            _obj_get(inv, 'hosted_invoice_url'),
        ),
    )


def backfill_invoices(agency_id, months=BACKFILL_MONTHS):
    portal.configure_stripe()
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            account_id = _agency_account(cur, agency_id)
    if not account_id:
        return
    created_gte = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp())
    try:
        it = stripe.Invoice.list(limit=100, created={'gte': created_gte}, **stripe_params(account_id))
        with portal.get_db_connection() as conn:
            with conn.cursor() as cur:
                for inv in it.auto_paging_iter():
                    upsert_invoice(cur, agency_id, inv)
            conn.commit()
    except stripe.error.PermissionError:
        _mark_connection_dead(agency_id)
    except Exception as exc:  # noqa: BLE001
        print(f'stripe backfill error for agency {agency_id}: {exc}')


def _enqueue_initial_sync(agency_id):
    threading.Thread(target=backfill_invoices, args=(agency_id,), daemon=True).start()


# --------------------------------------------------------------------------- #
# Connect webhook (separate endpoint + secret from the platform webhook)
# --------------------------------------------------------------------------- #
_INVOICE_EVENTS = {
    'invoice.finalized', 'invoice.paid', 'invoice.payment_failed',
    'invoice.voided', 'invoice.marked_uncollectible',
}


@stripe_bp.route('/stripe/connect/webhook', methods=['POST'])
def stripe_connect_webhook():
    secret = _connect_webhook_secret()
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    if not secret:
        abort(400)
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:  # noqa: BLE001 — invalid signature / payload
        abort(400)

    etype = event.get('type')
    account = event.get('account')  # the connected account the event came from
    obj = event.get('data', {}).get('object', {})
    created = event.get('created')

    if etype == 'account.application.deauthorized':
        # This is how we learn they disconnected from their own dashboard.
        if account:
            _teardown_account(account)
        return '', 200

    if not account:
        return '', 200

    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM public.users WHERE stripe_account_id = %s AND role = 'agency'",
                (account,),
            )
            row = cur.fetchone()
            if not row:
                return '', 200
            agency_id = row[0]
            if etype in _INVOICE_EVENTS:
                upsert_invoice(cur, agency_id, obj, event_ts=created)
            elif etype == 'customer.deleted':
                cur.execute(
                    'UPDATE public.users SET stripe_connect_customer_id = NULL '
                    'WHERE agency_id = %s AND stripe_connect_customer_id = %s',
                    (agency_id, _obj_get(obj, 'id')),
                )
            # customer.updated: name/email changes don't affect cached invoices.
    return '', 200


def _teardown_account(account_id):
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM public.users WHERE stripe_account_id = %s", (account_id,))
            row = cur.fetchone()
            if not row:
                return
            agency_id = row[0]
            cur.execute(
                'UPDATE public.users SET stripe_account_id = NULL, stripe_connected_at = NULL, '
                'stripe_livemode = NULL WHERE id = %s',
                (agency_id,),
            )
            cur.execute('DELETE FROM public.invoice_cache WHERE agency_id = %s', (agency_id,))


def nightly_reconcile():
    """Backstop for missed webhooks: re-pull invoices modified in the last 48h
    for every connected agency. Intended to be called from a cron entrypoint."""
    portal.configure_stripe()
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, stripe_account_id FROM public.users WHERE stripe_account_id IS NOT NULL AND role = 'agency'")
            agencies = cur.fetchall()
    since = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    for agency_id, account_id in agencies:
        try:
            it = stripe.Invoice.list(limit=100, created={'gte': since}, **stripe_params(account_id))
            with portal.get_db_connection() as conn:
                with conn.cursor() as cur:
                    for inv in it.auto_paging_iter():
                        upsert_invoice(cur, agency_id, inv)
                conn.commit()
        except stripe.error.PermissionError:
            _mark_connection_dead(agency_id)
        except Exception as exc:  # noqa: BLE001
            print(f'nightly reconcile warning for agency {agency_id}: {exc}')


# --------------------------------------------------------------------------- #
# Reconciliation: match portal clients <-> Stripe customers
# --------------------------------------------------------------------------- #
_NAME_STOPWORDS = re.compile(r'\b(ltd|limited|llc|inc|incorporated|co|company|plc|and)\b')


def normalise_name(name):
    s = (name or '').lower()
    s = s.replace('&', ' and ')
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = _NAME_STOPWORDS.sub(' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _fetch_stripe_customers(account_id):
    customers = []
    for c in stripe.Customer.list(limit=100, **stripe_params(account_id)).auto_paging_iter():
        customers.append({
            'id': _obj_get(c, 'id'),
            'name': _obj_get(c, 'name') or '',
            'email': (_obj_get(c, 'email') or '').lower(),
        })
    return customers


def propose_matches(agency_id):
    """Score portal clients against the agency's Stripe customers. Suggestions
    only — nothing is written; every link is user-confirmed."""
    portal.configure_stripe()
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            account_id = _agency_account(cur, agency_id)
            if not account_id:
                raise StripeNotConnected()
            cur.execute(
                "SELECT id, COALESCE(name, email), email FROM public.users "
                "WHERE role = 'client' AND agency_id = %s AND stripe_connect_customer_id IS NULL "
                "ORDER BY COALESCE(name, email)",
                (agency_id,),
            )
            clients = [{'id': r[0], 'name': r[1], 'email': (r[2] or '').lower()} for r in cur.fetchall()]

    customers = _fetch_stripe_customers(account_id)
    by_email = {}
    by_norm = {}
    for cust in customers:
        if cust['email']:
            by_email.setdefault(cust['email'], cust)
        n = normalise_name(cust['name'])
        if n:
            by_norm.setdefault(n, cust)

    matched, unmatched_clients = [], []
    used = set()

    def take(cust, confidence, client):
        used.add(cust['id'])
        matched.append({'client': client, 'customer': cust, 'confidence': confidence})

    for client in clients:
        cust = by_email.get(client['email']) if client['email'] else None
        if cust and cust['id'] not in used:
            take(cust, 'high', client)
            continue
        n = normalise_name(client['name'])
        cust = by_norm.get(n) if n else None
        if cust and cust['id'] not in used:
            take(cust, 'high', client)
            continue
        # fuzzy name suggestion
        best, best_ratio = None, 0.0
        for cand in customers:
            if cand['id'] in used:
                continue
            ratio = difflib.SequenceMatcher(None, n, normalise_name(cand['name'])).ratio() if n else 0.0
            if ratio > best_ratio:
                best, best_ratio = cand, ratio
        if best and best_ratio >= FUZZY_THRESHOLD:
            take(best, 'suggestion', client)
        else:
            unmatched_clients.append(client)

    unmatched_customers = [c for c in customers if c['id'] not in used]
    return {
        'matched': matched,
        'unmatched_clients': unmatched_clients,
        'unmatched_customers': unmatched_customers,
    }


@stripe_bp.route('/stripe/reconcile')
def stripe_reconcile():
    agency_id = _current_agency_id()
    if agency_id is None:
        return redirect(url_for('sign_in'))
    return render_template('stripe-reconcile.html')


@stripe_bp.route('/stripe/reconcile/data')
def stripe_reconcile_data():
    agency_id = _current_agency_id()
    if agency_id is None:
        return jsonify({'error': 'forbidden'}), 403
    try:
        return jsonify(propose_matches(agency_id))
    except StripeNotConnected:
        return jsonify({'error': 'not_connected'}), 409
    except stripe.error.PermissionError:
        _mark_connection_dead(agency_id)
        return jsonify({'error': 'connection_revoked'}), 409
    except Exception as exc:  # noqa: BLE001
        return jsonify({'error': str(exc)}), 500


@stripe_bp.route('/stripe/reconcile/confirm', methods=['POST'])
def stripe_reconcile_confirm():
    agency_id = _current_agency_id()
    if agency_id is None:
        return jsonify({'error': 'forbidden'}), 403
    payload = request.get_json(silent=True) or {}
    links = payload.get('links') or []  # [{client_id, customer_id}]
    linked = 0
    with portal.get_db_connection() as conn:
        with conn.cursor() as cur:
            for link in links:
                try:
                    client_id = int(link.get('client_id'))
                    customer_id = str(link.get('customer_id'))
                except (TypeError, ValueError):
                    continue
                if not customer_id:
                    continue
                # Only map a client that belongs to this agency.
                cur.execute(
                    "SELECT 1 FROM public.users WHERE id = %s AND agency_id = %s AND role = 'client'",
                    (client_id, agency_id),
                )
                if not cur.fetchone():
                    continue
                cur.execute(
                    'UPDATE public.users SET stripe_connect_customer_id = %s WHERE id = %s',
                    (customer_id, client_id),
                )
                # Attribute any already-cached invoices for this customer.
                cur.execute(
                    'UPDATE public.invoice_cache SET client_id = %s '
                    'WHERE agency_id = %s AND stripe_customer_id = %s',
                    (client_id, agency_id, customer_id),
                )
                linked += 1
    return jsonify({'ok': True, 'linked': linked})


# --------------------------------------------------------------------------- #
# Status helper for the settings UI
# --------------------------------------------------------------------------- #
def get_billing_status(cur, agency_id):
    cur.execute(
        'SELECT stripe_account_id, stripe_connected_at, stripe_livemode FROM public.users WHERE id = %s',
        (agency_id,),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return {'connected': False, 'configured': is_configured()}
    cur.execute(
        'SELECT COUNT(*), MAX(synced_at), COUNT(*) FILTER (WHERE status = %s), '
        'COUNT(*) FILTER (WHERE client_id IS NULL) FROM public.invoice_cache WHERE agency_id = %s',
        ('open', agency_id),
    )
    counts = cur.fetchone()
    return {
        'connected': True,
        'configured': True,
        'account_id': row[0],
        'connected_at': row[1].isoformat() if row[1] else None,
        'livemode': row[2],
        'invoice_count': counts[0],
        'synced_at': counts[1].isoformat() if counts[1] else None,
        'open_count': counts[2],
        'unassigned_count': counts[3],
    }


def register_stripe(flask_app, portal_module):
    global portal
    portal = portal_module
    flask_app.register_blueprint(stripe_bp)
