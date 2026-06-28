from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
import json
import mimetypes
import os
import secrets
import uuid
import psycopg
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import stripe

ATTACHMENTS_BUCKET = 'message_attatchments'
VAULT_BUCKET = 'vault_documents'
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_VAULT_BYTES = 25 * 1024 * 1024
ALLOWED_ATTACHMENT_TYPES = {
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
    'application/pdf',
    'text/plain',
    'text/csv',
    'application/zip',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.txt', '.csv', '.zip',
    '.doc', '.docx', '.xlsx', '.pptx',
}
EXTENSION_MIME_MAP = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.pdf': 'application/pdf',
    '.txt': 'text/plain',
    '.csv': 'text/csv',
    '.zip': 'application/zip',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
def _load_env_file():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())

def parse_chat_ids(chats_raw):
    if not chats_raw:
        return []
    return [int(part) for part in chats_raw.split(',') if part.strip()]

def format_chat_ids(chat_ids):
    if not chat_ids:
        return ''
    return ','.join(str(chat_id) for chat_id in chat_ids) + ','

def can_message(user_id, chat_id, cursor):
    cursor.execute('SELECT chats FROM public.users WHERE id = %s', (user_id,))
    row = cursor.fetchone()
    if not row:
        return False
    return chat_id in parse_chat_ids(row[0])

def get_supabase_project_ref_from_db_url(db_url):
    if not db_url:
        return None
    username = urlparse(db_url).username or ''
    if username.startswith('postgres.'):
        return username.split('.', 1)[1]
    return None

def get_supabase_config():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')

    if url:
        url = url.strip()
        if url.startswith('postgresql://') or '@' in url:
            url = None
        elif not url.startswith('http'):
            url = f'https://{url}'
        url = url.rstrip('/') if url else None

    if not url:
        project_ref = get_supabase_project_ref_from_db_url(DB_URL)
        if project_ref:
            url = f'https://{project_ref}.supabase.co'

    if url and key:
        return url, key
    return None, None

def parse_attachments(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    if isinstance(raw, list):
        return raw
    return []

def enrich_attachments(attachments, chat_id):
    enriched = []
    for attachment in parse_attachments(attachments):
        path = attachment.get('path')
        if not path:
            continue
        enriched.append({
            **attachment,
            'url': url_for('chat_attachment', chat_id=chat_id, path=path),
        })
    return enriched

def resolve_attachment_mime(filename, reported_mime):
    reported = (reported_mime or '').split(';')[0].strip()
    if reported in ALLOWED_ATTACHMENT_TYPES:
        return reported

    guessed, _ = mimetypes.guess_type(filename or '')
    if guessed in ALLOWED_ATTACHMENT_TYPES:
        return guessed

    ext = os.path.splitext(filename or '')[1].lower()
    if ext in ALLOWED_ATTACHMENT_EXTENSIONS:
        return EXTENSION_MIME_MAP[ext]

    raise ValueError('file type is not allowed')

def can_access_attachment_path(path, user_id, chat_id):
    if not path or '..' in path:
        return False
    return path.startswith(f'{user_id}/') or path.startswith(f'{chat_id}/')

def upload_attachment(file_storage, sender_id):
    supabase_url, service_key = get_supabase_config()
    if not supabase_url or not service_key:
        raise ValueError('file storage is not configured')

    data = file_storage.read()
    if not data:
        raise ValueError('empty file')
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError('file is too large (max 10MB)')

    mime = resolve_attachment_mime(file_storage.filename, file_storage.mimetype)
    safe_name = secure_filename(file_storage.filename) or 'file'
    object_path = f'{sender_id}/{uuid.uuid4().hex}_{safe_name}'
    upload_url = f'{supabase_url}/storage/v1/object/{ATTACHMENTS_BUCKET}/{object_path}'

    req = Request(
        upload_url,
        data=data,
        method='POST',
        headers={
            'Authorization': f'Bearer {service_key}',
            'apikey': service_key,
            'Content-Type': mime,
            'x-upsert': 'false',
        },
    )
    try:
        with urlopen(req) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise ValueError(f'upload failed: {detail or exc.reason}') from exc

    return {
        'name': file_storage.filename or safe_name,
        'path': object_path,
        'mime': mime,
        'size': len(data),
    }

def fetch_conversation(cursor, user_id, chat_id):
    cursor.execute(
        'SELECT COALESCE(name, email) FROM public.users WHERE id = %s',
        (chat_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None, None

    other_name = row[0]
    cursor.execute(
        '''
        SELECT id, sender_id, body, COALESCE(attachments, '[]'::jsonb), form_item_id, todo_order
        FROM public.messages
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
        ORDER BY created_at ASC
        ''',
        (user_id, chat_id, chat_id, user_id),
    )
    rows = cursor.fetchall()
    messages = []
    for msg_id, sender_id, body, attachments, form_item_id, todo_order in rows:
        message = {
            'id': msg_id,
            'sender': 'you' if sender_id == user_id else other_name,
            'body': body or '',
            'attachments': enrich_attachments(attachments, chat_id),
        }
        if form_item_id:
            card = get_form_card(cursor, msg_id, form_item_id)
            if card:
                message['form'] = card
                message['todo_order'] = todo_order
        messages.append(message)
    return other_name, messages

def upsert_user_presence(cursor, user_id):
    cursor.execute('SELECT public.upsert_user_presence(%s)', (user_id,))

def set_user_typing(cursor, user_id, chat_id, seconds=5):
    cursor.execute(
        'SELECT public.set_user_typing(%s, %s, %s)',
        (user_id, chat_id, seconds),
    )

def clear_user_typing(cursor, user_id):
    cursor.execute(
        '''
        UPDATE public.user_presence
        SET typing_chat_id = NULL, typing_until = NULL
        WHERE user_id = %s
        ''',
        (user_id,),
    )

def get_chat_presence(cursor, viewer_id, chat_id):
    # chat_id is the other person; they are typing in this chat when typing_chat_id = viewer_id
    cursor.execute(
        '''
        SELECT
            last_seen_at > now() - interval '45 seconds' AS is_online,
            typing_chat_id = %s AND typing_until > now() AS is_typing
        FROM public.user_presence
        WHERE user_id = %s
        ''',
        (viewer_id, chat_id),
    )
    row = cursor.fetchone()
    if not row:
        return {'online': False, 'typing': False}
    return {'online': bool(row[0]), 'typing': bool(row[1])}

def attachment_error_response(message, status_code):
    return (
        f'<html><body style="font-family:sans-serif;padding:2rem;text-align:center">'
        f'<p>{message}</p></body></html>',
        status_code,
        {'Content-Type': 'text/html; charset=utf-8'},
    )

def get_user_role(cursor, user_id):
    cursor.execute('SELECT role FROM public.users WHERE id = %s', (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def is_agency_role(role):
    return role == 'agency'

def get_preview_user_id(role, user_id, chat_id):
    if is_agency_role(role):
        return chat_id
    return user_id

def get_site_url(cursor, role, user_id, chat_id):
    preview_user_id = get_preview_user_id(role, user_id, chat_id)
    cursor.execute(
        'SELECT site_url FROM public.users WHERE id = %s',
        (preview_user_id,),
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return None
    return row[0]

def normalize_site_url(url):
    url = (url or '').strip()
    if not url:
        return None
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    return url

def get_client_progress(cursor, role, user_id, chat_id):
    client_id = get_preview_user_id(role, user_id, chat_id)
    cursor.execute('SELECT COALESCE(progress, 0), delivery_date FROM public.users WHERE id = %s', (client_id,))
    row = cursor.fetchone()
    if not row:
        return 0, None
    return (row[0] or 0), (row[1].isoformat() if row[1] else None)

def get_vault_client_id(cursor, user_id, chat_id):
    # A vault belongs to the client in the conversation. For an agency this is
    # the chat partner (the client); for a client it is themselves.
    role = get_user_role(cursor, user_id)
    return get_preview_user_id(role, user_id, chat_id)

def serialize_vault_item(row, chat_id):
    item_id, parent_id, kind, name, pos_x, pos_y, mime, size_bytes = row
    item = {
        'id': item_id,
        'parent_id': parent_id,
        'kind': kind,
        'name': name,
        'x': pos_x,
        'y': pos_y,
    }
    if kind == 'file':
        item['mime'] = mime
        item['size'] = size_bytes
        item['url'] = url_for('vault_file', chat_id=chat_id, item_id=item_id)
    return item

def fetch_vault_items(cursor, client_id, chat_id):
    cursor.execute(
        '''
        SELECT id, parent_id, kind, name, pos_x, pos_y, mime, size_bytes
        FROM public.vault_items
        WHERE client_id = %s
        ORDER BY created_at ASC
        ''',
        (client_id,),
    )
    return [serialize_vault_item(row, chat_id) for row in cursor.fetchall()]

def vault_parent_ok(cursor, parent_id, client_id):
    if parent_id is None:
        return True
    cursor.execute(
        "SELECT 1 FROM public.vault_items WHERE id = %s AND client_id = %s AND kind = 'folder'",
        (parent_id, client_id),
    )
    return cursor.fetchone() is not None

def upload_vault_object(file_storage, client_id):
    supabase_url, service_key = get_supabase_config()
    if not supabase_url or not service_key:
        raise ValueError('file storage is not configured')

    data = file_storage.read()
    if not data:
        raise ValueError('empty file')
    if len(data) > MAX_VAULT_BYTES:
        raise ValueError('file is too large (max 25MB)')

    mime = resolve_attachment_mime(file_storage.filename, file_storage.mimetype)
    safe_name = secure_filename(file_storage.filename) or 'file'
    object_path = f'{client_id}/{uuid.uuid4().hex}_{safe_name}'
    upload_url = f'{supabase_url}/storage/v1/object/{VAULT_BUCKET}/{object_path}'

    req = Request(
        upload_url,
        data=data,
        method='POST',
        headers={
            'Authorization': f'Bearer {service_key}',
            'apikey': service_key,
            'Content-Type': mime,
            'x-upsert': 'false',
        },
    )
    try:
        with urlopen(req) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise ValueError(f'upload failed: {detail or exc.reason}') from exc

    return {'path': object_path, 'mime': mime, 'size': len(data)}

def delete_vault_object(path):
    supabase_url, service_key = get_supabase_config()
    if not supabase_url or not service_key or not path:
        return
    delete_url = f'{supabase_url}/storage/v1/object/{VAULT_BUCKET}/{quote(path, safe="/")}'
    req = Request(
        delete_url,
        method='DELETE',
        headers={'Authorization': f'Bearer {service_key}', 'apikey': service_key},
    )
    try:
        with urlopen(req) as response:
            response.read()
    except HTTPError:
        pass

def ensure_vault_folder(cursor, client_id, name, uploaded_by):
    # Find a root-level folder by name in the client's vault, creating it if missing.
    name = (name or '').strip()
    if not name:
        return None
    cursor.execute(
        "SELECT id FROM public.vault_items "
        "WHERE client_id = %s AND parent_id IS NULL AND kind = 'folder' AND lower(name) = lower(%s) "
        'LIMIT 1',
        (client_id, name),
    )
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute(
        "INSERT INTO public.vault_items (client_id, parent_id, kind, name, pos_x, pos_y, uploaded_by) "
        "VALUES (%s, NULL, 'folder', %s, 40, 40, %s) RETURNING id",
        (client_id, name, uploaded_by),
    )
    return cursor.fetchone()[0]

def normalize_form_schema(raw):
    if not isinstance(raw, dict):
        return {'fields': []}
    fields = raw.get('fields')
    if not isinstance(fields, list):
        fields = []
    clean = []
    for idx, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        ftype = field.get('type')
        if ftype not in ('text', 'textarea', 'file'):
            continue
        entry = {
            'id': str(field.get('id') or f'f{idx}')[:40],
            'type': ftype,
            'label': (str(field.get('label') or '').strip()[:200]) or 'Untitled',
            'required': bool(field.get('required')),
        }
        if ftype in ('text', 'textarea'):
            entry['placeholder'] = str(field.get('placeholder') or '').strip()[:200]
        if ftype == 'file':
            entry['folder'] = str(field.get('folder') or '').strip()[:120]
        clean.append(entry)
    return {'fields': clean[:50]}

def get_form_card(cursor, message_id, form_item_id):
    cursor.execute('SELECT name, schema FROM public.form_items WHERE id = %s', (form_item_id,))
    row = cursor.fetchone()
    if not row:
        return None
    name, schema = row
    fields = schema.get('fields', []) if isinstance(schema, dict) else []
    cursor.execute('SELECT answers FROM public.form_submissions WHERE message_id = %s LIMIT 1', (message_id,))
    sub = cursor.fetchone()
    card = {'form_item_id': form_item_id, 'name': name, 'fields': fields, 'submitted': sub is not None}
    if sub is not None:
        card['answers'] = sub[0] if isinstance(sub[0], list) else []
    return card

def serialize_form_item(row):
    item_id, parent_id, kind, name, pos_x, pos_y, field_count = row
    item = {'id': item_id, 'parent_id': parent_id, 'kind': kind, 'name': name, 'x': pos_x, 'y': pos_y}
    if kind == 'form':
        item['fields'] = field_count
    return item

def forms_agency_guard(cursor):
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'error': 'unauthorized'}), 401)
    if not is_agency_role(get_user_role(cursor, user_id)):
        return None, (jsonify({'error': 'forbidden'}), 403)
    return user_id, None

def is_pro_user(value):
    return bool(value)

def get_user_pro_status(cursor, user_id):
    cursor.execute(
        'SELECT COALESCE(pro_user, false) FROM public.users WHERE id = %s',
        (user_id,),
    )
    row = cursor.fetchone()
    return is_pro_user(row[0]) if row else False

def configure_stripe():
    stripe.api_key = os.getenv('STRIPE_SECRET_KEY')

def stripe_field(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except Exception:
        return getattr(obj, key, default)

def is_stale_stripe_reference_error(exc):
    if not isinstance(exc, stripe.error.StripeError):
        return False
    message = str(exc)
    return (
        'No such customer' in message
        or 'No such subscription' in message
        or 'similar object exists in test mode' in message
        or 'similar object exists in live mode' in message
    )

def clear_stripe_billing_ids(cursor, user_id):
    try:
        cursor.execute('SAVEPOINT stripe_billing_clear')
        cursor.execute(
            '''
            UPDATE public.users
            SET stripe_customer_id = NULL,
                stripe_subscription_id = NULL,
                pro_user = false
            WHERE id = %s
            ''',
            (user_id,),
        )
        cursor.execute('RELEASE SAVEPOINT stripe_billing_clear')
    except psycopg.Error:
        cursor.execute('ROLLBACK TO SAVEPOINT stripe_billing_clear')

def get_stripe_customer_id(cursor, user_id, email):
    stored_id = None
    try:
        cursor.execute('SAVEPOINT stripe_customer_lookup')
        cursor.execute(
            'SELECT stripe_customer_id FROM public.users WHERE id = %s',
            (user_id,),
        )
        row = cursor.fetchone()
        cursor.execute('RELEASE SAVEPOINT stripe_customer_lookup')
        if row and row[0]:
            stored_id = row[0]
    except psycopg.Error:
        cursor.execute('ROLLBACK TO SAVEPOINT stripe_customer_lookup')

    configure_stripe()
    if stored_id:
        try:
            stripe.Customer.retrieve(stored_id)
            return stored_id
        except stripe.error.StripeError as exc:
            if is_stale_stripe_reference_error(exc):
                print(f'Clearing stale Stripe customer {stored_id} for user {user_id}')
                clear_stripe_billing_ids(cursor, user_id)
            else:
                print(f'Could not verify Stripe customer {stored_id}: {exc}')
                return None

    try:
        customers = stripe.Customer.list(email=email, limit=1)
    except stripe.error.StripeError as exc:
        print(f'Could not look up Stripe customer for {email}: {exc}')
        return None

    if not customers.data:
        return None

    customer_id = customers.data[0].id
    try:
        cursor.execute('SAVEPOINT stripe_customer_save')
        cursor.execute(
            'UPDATE public.users SET stripe_customer_id = %s WHERE id = %s',
            (customer_id, user_id),
        )
        cursor.execute('RELEASE SAVEPOINT stripe_customer_save')
    except psycopg.Error:
        cursor.execute('ROLLBACK TO SAVEPOINT stripe_customer_save')
    return customer_id

def get_active_subscription_id(customer_id, fallback_subscription_id=None):
    if fallback_subscription_id:
        try:
            subscription = stripe.Subscription.retrieve(fallback_subscription_id)
            status = stripe_field(subscription, 'status')
            if status and status not in ('canceled', 'incomplete_expired'):
                return fallback_subscription_id
        except stripe.error.StripeError as exc:
            if is_stale_stripe_reference_error(exc):
                pass
            else:
                print(f'Could not retrieve subscription {fallback_subscription_id}: {exc}')

    try:
        subscriptions = stripe.Subscription.list(customer=customer_id, status='all', limit=10)
    except stripe.error.StripeError as exc:
        print(f'Could not list subscriptions for {customer_id}: {exc}')
        return None
    for subscription in subscriptions.data:
        status = stripe_field(subscription, 'status')
        if status and status not in ('canceled', 'incomplete_expired'):
            return stripe_field(subscription, 'id')
    return None

def activate_user_subscription(cursor, user_id, customer_id=None, subscription_id=None):
    cursor.execute(
        'UPDATE public.users SET pro_user = true WHERE id = %s',
        (user_id,),
    )
    if customer_id or subscription_id:
        try:
            cursor.execute('SAVEPOINT stripe_ids')
            cursor.execute(
                '''
                UPDATE public.users
                SET stripe_customer_id = COALESCE(%s, stripe_customer_id),
                    stripe_subscription_id = COALESCE(%s, stripe_subscription_id)
                WHERE id = %s
                ''',
                (customer_id, subscription_id, user_id),
            )
            cursor.execute('RELEASE SAVEPOINT stripe_ids')
        except psycopg.Error as exc:
            cursor.execute('ROLLBACK TO SAVEPOINT stripe_ids')
            print(f'Could not save Stripe IDs for user {user_id}: {exc}')

def fulfill_checkout_session(checkout_session):
    metadata = stripe_field(checkout_session, 'metadata', {})
    metadata = metadata or {}
    client_id = stripe_field(metadata, 'client_id') or stripe_field(checkout_session, 'client_reference_id')
    status = stripe_field(checkout_session, 'status')
    payment_status = stripe_field(checkout_session, 'payment_status')
    customer_id = stripe_field(checkout_session, 'customer')
    subscription_id = stripe_field(checkout_session, 'subscription')

    if status != 'complete':
        return False
    if payment_status not in ('paid', 'no_payment_required'):
        return False
    if not client_id or not str(client_id).isdigit():
        return False

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            activate_user_subscription(
                cursor,
                int(client_id),
                customer_id,
                subscription_id,
            )
    return True

def sync_user_subscription_from_stripe(cursor, user_id, email):
    configure_stripe()
    if not stripe.api_key:
        return get_user_pro_status(cursor, user_id)

    customer_id = get_stripe_customer_id(cursor, user_id, email)
    if not customer_id:
        return get_user_pro_status(cursor, user_id)

    try:
        subscriptions = stripe.Subscription.list(customer=customer_id, status='active', limit=1)
    except stripe.error.StripeError as exc:
        print(f'Could not sync subscription for user {user_id}: {exc}')
        return get_user_pro_status(cursor, user_id)

    has_active = bool(subscriptions.data)

    if has_active:
        subscription = subscriptions.data[0]
        activate_user_subscription(cursor, user_id, customer_id, subscription.id)
        return True

    set_user_pro_status(cursor, user_id, False)
    return False

def set_user_pro_status(cursor, user_id, pro_user, customer_id=None, subscription_id=None):
    if pro_user:
        activate_user_subscription(cursor, user_id, customer_id, subscription_id)
        return

    cursor.execute(
        'UPDATE public.users SET pro_user = false WHERE id = %s',
        (user_id,),
    )
    try:
        cursor.execute('SAVEPOINT stripe_subscription_clear')
        cursor.execute(
            'UPDATE public.users SET stripe_subscription_id = NULL WHERE id = %s',
            (user_id,),
        )
        cursor.execute('RELEASE SAVEPOINT stripe_subscription_clear')
    except psycopg.Error:
        cursor.execute('ROLLBACK TO SAVEPOINT stripe_subscription_clear')

def deactivate_user_by_subscription(cursor, subscription_id, client_id=None, customer_id=None):
    if subscription_id:
        try:
            cursor.execute('SAVEPOINT stripe_subscription_deactivate')
            cursor.execute(
                '''
                UPDATE public.users
                SET pro_user = false, stripe_subscription_id = NULL
                WHERE stripe_subscription_id = %s
                ''',
                (subscription_id,),
            )
            if cursor.rowcount:
                cursor.execute('RELEASE SAVEPOINT stripe_subscription_deactivate')
                return
            cursor.execute('RELEASE SAVEPOINT stripe_subscription_deactivate')
        except psycopg.Error:
            cursor.execute('ROLLBACK TO SAVEPOINT stripe_subscription_deactivate')

    if client_id and str(client_id).isdigit():
        set_user_pro_status(cursor, int(client_id), False)
        return

    if customer_id:
        try:
            cursor.execute('SAVEPOINT stripe_customer_deactivate')
            cursor.execute(
                '''
                UPDATE public.users
                SET pro_user = false, stripe_subscription_id = NULL
                WHERE stripe_customer_id = %s
                ''',
                (customer_id,),
            )
            cursor.execute('RELEASE SAVEPOINT stripe_customer_deactivate')
        except psycopg.Error:
            cursor.execute('ROLLBACK TO SAVEPOINT stripe_customer_deactivate')

_load_env_file()
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'dev')
app.permanent_session_lifetime = timedelta(days=30)


@app.before_request
def _make_session_permanent():
    session.permanent = True
APP_URL = (os.environ.get('APP_URL') or '').rstrip('/')
if APP_URL.startswith('https://'):
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
DB_URL = os.environ.get('DB_URL')
ADMIN_ACC_ID = 7
_stripe_schema_ready = False
_messages_schema_ready = False
_agency_schema_ready = False
_vault_schema_ready = False
_forms_schema_ready = False


def external_url(endpoint, **values):
    if APP_URL:
        return f'{APP_URL}{url_for(endpoint, _external=False, **values)}'
    return url_for(endpoint, _external=True, **values)


def ensure_stripe_schema(conn):
    global _stripe_schema_ready
    if _stripe_schema_ready:
        return
    with conn.cursor() as cursor:
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS stripe_customer_id text')
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS stripe_subscription_id text')
    _stripe_schema_ready = True


def ensure_messages_schema(conn):
    global _messages_schema_ready
    if _messages_schema_ready:
        return
    with conn.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS attachments jsonb DEFAULT '[]'::jsonb"
        )
        cursor.execute(
            '''
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'messages'
              AND column_name = 'attatchments'
            '''
        )
        if cursor.fetchone():
            cursor.execute(
                '''
                UPDATE public.messages
                SET attachments = attatchments
                WHERE attachments IS NULL OR attachments = '[]'::jsonb
                '''
            )
    _messages_schema_ready = True


def ensure_agency_schema(conn):
    global _agency_schema_ready
    if _agency_schema_ready:
        return
    with conn.cursor() as cursor:
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role text')
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS agency_id bigint')
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS progress integer')
        cursor.execute('ALTER TABLE public.users ADD COLUMN IF NOT EXISTS delivery_date date')
        # One-time backfill of existing accounts. The original admin (ADMIN_ACC_ID)
        # becomes an agency; everyone else becomes a client of that agency. Only rows
        # whose role is still NULL are touched, so this is safe to run on every boot.
        cursor.execute(
            "UPDATE public.users SET role = 'agency' WHERE id = %s AND role IS NULL",
            (ADMIN_ACC_ID,),
        )
        cursor.execute(
            "UPDATE public.users SET role = 'client', agency_id = %s "
            'WHERE role IS NULL AND id <> %s',
            (ADMIN_ACC_ID, ADMIN_ACC_ID),
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.invitations (
                id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                token text UNIQUE NOT NULL,
                agency_id bigint NOT NULL,
                email text,
                status text NOT NULL DEFAULT 'pending',
                accepted_user_id bigint,
                created_at timestamptz NOT NULL DEFAULT now(),
                expires_at timestamptz
            )
            '''
        )
    _agency_schema_ready = True


def ensure_vault_schema(conn):
    global _vault_schema_ready
    if _vault_schema_ready:
        return
    with conn.cursor() as cursor:
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.vault_items (
                id           bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                client_id    bigint NOT NULL,
                parent_id    bigint REFERENCES public.vault_items(id) ON DELETE CASCADE,
                kind         text   NOT NULL CHECK (kind IN ('folder', 'file')),
                name         text   NOT NULL,
                pos_x        double precision NOT NULL DEFAULT 40,
                pos_y        double precision NOT NULL DEFAULT 40,
                storage_path text,
                mime         text,
                size_bytes   bigint,
                uploaded_by  bigint,
                created_at   timestamptz NOT NULL DEFAULT now(),
                updated_at   timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_vault_items_client ON public.vault_items (client_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_vault_items_parent ON public.vault_items (parent_id)'
        )
        try:
            cursor.execute('SAVEPOINT vault_bucket')
            cursor.execute(
                '''
                INSERT INTO storage.buckets (id, name, public)
                VALUES (%s, %s, false)
                ON CONFLICT (id) DO NOTHING
                ''',
                (VAULT_BUCKET, VAULT_BUCKET),
            )
            cursor.execute('RELEASE SAVEPOINT vault_bucket')
        except psycopg.Error:
            # storage schema may not be writable from this role; the bucket can be
            # created once in the Supabase dashboard instead.
            cursor.execute('ROLLBACK TO SAVEPOINT vault_bucket')
    _vault_schema_ready = True


def ensure_forms_schema(conn):
    global _forms_schema_ready
    if _forms_schema_ready:
        return
    with conn.cursor() as cursor:
        cursor.execute('ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS form_item_id bigint')
        cursor.execute('ALTER TABLE public.messages ADD COLUMN IF NOT EXISTS todo_order integer')
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.form_items (
                id         bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                agency_id  bigint NOT NULL,
                parent_id  bigint REFERENCES public.form_items(id) ON DELETE CASCADE,
                kind       text   NOT NULL CHECK (kind IN ('folder', 'form')),
                name       text   NOT NULL,
                pos_x      double precision NOT NULL DEFAULT 40,
                pos_y      double precision NOT NULL DEFAULT 40,
                schema     jsonb,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_form_items_agency ON public.form_items (agency_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_form_items_parent ON public.form_items (parent_id)')
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS public.form_submissions (
                id           bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                form_item_id bigint,
                message_id   bigint,
                agency_id    bigint,
                client_id    bigint,
                submitted_by bigint,
                answers      jsonb,
                created_at   timestamptz NOT NULL DEFAULT now()
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_form_submissions_message ON public.form_submissions (message_id)')
    _forms_schema_ready = True


def get_db_connection():
    conn = psycopg.connect(DB_URL, prepare_threshold=None)
    try:
        ensure_stripe_schema(conn)
        ensure_messages_schema(conn)
        ensure_agency_schema(conn)
        ensure_vault_schema(conn)
        ensure_forms_schema(conn)
    except psycopg.Error as exc:
        conn.rollback()
        print(f'Could not ensure database schema: {exc}')
    return conn


@app.route('/')
def index():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('sign-in.html')

@app.route('/dashboard')
def dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('sign_in'))

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT id, email, chats, COALESCE(pro_user, false), role FROM public.users WHERE id = %s',
                (user_id,),
            )
            row = cursor.fetchone()

            if not row:
                session.clear()
                return redirect(url_for('sign_in'))

            sync_user_subscription_from_stripe(cursor, user_id, row[1])

            cursor.execute(
                'SELECT COALESCE(pro_user, false) FROM public.users WHERE id = %s',
                (user_id,),
            )
            pro_user = is_pro_user(cursor.fetchone()[0])

            chat_ids = parse_chat_ids(row[2])
            chats = []
            if chat_ids:
                cursor.execute(
                    'SELECT COALESCE(name, email), id, COALESCE(pro_user, false) FROM public.users WHERE id = ANY(%s)',
                    (chat_ids,),
                )
                chats = cursor.fetchall()

    return render_template(
        'dashboard.html',
        user_id=row[0],
        email=row[1],
        chats=chats,
        pro_user=pro_user,
        is_agency=is_agency_role(row[4]),
    )

@app.route('/settings')
def settings():
    if not session.get('user_id'):
        return redirect(url_for('sign_in'))

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            sync_user_subscription_from_stripe(
                cursor,
                session.get('user_id'),
                session.get('email'),
            )
            pro_user = get_user_pro_status(cursor, session.get('user_id'))

    plan = 'pro' if pro_user else 'free'
    return render_template(
        'settings.html',
        email=session.get('email'),
        plan=plan,
        pro_user=pro_user,
    )

@app.route('/sign-in')
def sign_in():
    return render_template('sign-in.html')

@app.route('/sign-up')
def sign_up():
    return render_template('sign-up.html')

@app.route('/sign-up-api', methods=['POST'])
def sign_up_api():
    email = request.form['email']
    password = request.form['password']
    org_name = request.form['org_name']
    hashed_password = generate_password_hash(password)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO public.users (email, password, created_at, name, chats, role)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                ''',
                (email, hashed_password, datetime.now(timezone.utc), org_name, '', 'agency'),
            )
            user_id = cursor.fetchone()[0]

            session['user_id'] = int(user_id)
            session['email'] = email
            session.modified = True

    return redirect(url_for('dashboard'))

@app.route('/invite/create', methods=['POST'])
def invite_create():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403

            token = secrets.token_urlsafe(24)
            cursor.execute(
                '''
                INSERT INTO public.invitations (token, agency_id, created_at)
                VALUES (%s, %s, %s)
                ''',
                (token, user_id, datetime.now(timezone.utc)),
            )

    return jsonify({'url': external_url('invite_landing', token=token)})

@app.route('/invite/<token>')
def invite_landing(token):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT i.agency_id, i.status, COALESCE(u.name, u.email)
                FROM public.invitations i
                JOIN public.users u ON u.id = i.agency_id
                WHERE i.token = %s
                ''',
                (token,),
            )
            row = cursor.fetchone()

    if not row or row[1] != 'pending':
        return render_template('invite-invalid.html')

    return render_template('sign-up-client.html', token=token, agency_name=row[2])

@app.route('/invite/accept', methods=['POST'])
def invite_accept():
    token = request.form.get('token', '')
    email = request.form.get('email', '')
    password = request.form.get('password', '')
    name = request.form.get('name', '')

    if not token or not email or not password or not name:
        return redirect(url_for('invite_landing', token=token) if token else url_for('sign_in'))

    hashed_password = generate_password_hash(password)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT agency_id, status FROM public.invitations WHERE token = %s FOR UPDATE",
                (token,),
            )
            invite = cursor.fetchone()
            if not invite or invite[1] != 'pending':
                return render_template('invite-invalid.html')

            agency_id = invite[0]

            cursor.execute(
                '''
                INSERT INTO public.users (email, password, created_at, name, chats, role, agency_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
                ''',
                (
                    email,
                    hashed_password,
                    datetime.now(timezone.utc),
                    name,
                    format_chat_ids([agency_id]),
                    'client',
                    agency_id,
                ),
            )
            client_id = cursor.fetchone()[0]

            cursor.execute(
                'SELECT COALESCE(name, email) FROM public.users WHERE id = %s',
                (agency_id,),
            )
            agency_name = cursor.fetchone()[0]
            cursor.execute(
                'INSERT INTO public.messages (sender_id, receiver_id, created_at, body) VALUES (%s, %s, %s, %s)',
                (
                    agency_id,
                    client_id,
                    datetime.now(timezone.utc),
                    f'Hello {name}, welcome to your {agency_name} portal',
                ),
            )

            cursor.execute('SELECT chats FROM public.users WHERE id = %s', (agency_id,))
            agency_chat_ids = parse_chat_ids(cursor.fetchone()[0])
            if client_id not in agency_chat_ids:
                agency_chat_ids.append(client_id)
            cursor.execute(
                'UPDATE public.users SET chats = %s WHERE id = %s',
                (format_chat_ids(agency_chat_ids), agency_id),
            )

            cursor.execute(
                '''
                UPDATE public.invitations
                SET status = 'accepted', accepted_user_id = %s, email = %s
                WHERE token = %s
                ''',
                (client_id, email, token),
            )

            session['user_id'] = int(client_id)
            session['email'] = email
            session.modified = True

    return redirect(url_for('dashboard'))

@app.route('/sign-in-api', methods=['POST'])
def sign_in_api():
    email = request.form['email']
    password = request.form['password']
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT id, email, password FROM public.users WHERE email = %s',
                (email,),
            )
            row = cursor.fetchone()
            if row and check_password_hash(row[2], password):
                session['user_id'] = int(row[0])
                session['email'] = row[1]
                session.modified = True
                return redirect(url_for('dashboard'))

    return redirect(url_for('sign_in'))

@app.route('/chat/<int:chat_id>')
def chat(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('sign_in'))

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return redirect(url_for('dashboard'))

            other_persons_name, conversation_list = fetch_conversation(cursor, user_id, chat_id)
            if other_persons_name is None:
                return redirect(url_for('dashboard'))

            role = get_user_role(cursor, user_id)
            site_url = get_site_url(cursor, role, user_id, chat_id)
            other_pro_user = get_user_pro_status(cursor, chat_id)
            progress, delivery_date = get_client_progress(cursor, role, user_id, chat_id)

    is_admin = is_agency_role(role)
    return render_template(
        'chat.html',
        chat_id=chat_id,
        conversation_list=conversation_list,
        other_persons_name=other_persons_name,
        other_pro_user=other_pro_user,
        site_url=site_url,
        admin=is_admin,
        user_id=user_id,
        progress=progress,
        delivery_date=delivery_date,
    )

@app.route('/chat/<int:chat_id>/messages')
def chat_messages(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            other_name, messages = fetch_conversation(cursor, user_id, chat_id)
            if other_name is None:
                return jsonify({'error': 'not found'}), 404

    return jsonify({'messages': messages})

@app.route('/chat/<int:chat_id>/presence', methods=['GET', 'POST'])
def chat_presence(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            if request.method == 'POST':
                upsert_user_presence(cursor, user_id)
                return jsonify({'ok': True})

            presence = get_chat_presence(cursor, user_id, chat_id)

    return jsonify(presence)

@app.route('/chat/<int:chat_id>/typing', methods=['POST'])
def chat_typing(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            set_user_typing(cursor, user_id, chat_id)

    return jsonify({'ok': True})

@app.route('/chat/<int:chat_id>/attachment')
def chat_attachment(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return attachment_error_response('Sign in to view this file.', 401)

    path = request.args.get('path', '').strip()
    if not can_access_attachment_path(path, user_id, chat_id):
        return attachment_error_response('This file is not available.', 400)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return attachment_error_response('You do not have access to this chat.', 403)

    supabase_url, service_key = get_supabase_config()
    if not supabase_url or not service_key:
        return attachment_error_response('File storage is not configured.', 503)

    download_url = f'{supabase_url}/storage/v1/object/{ATTACHMENTS_BUCKET}/{quote(path, safe="/")}'
    req = Request(
        download_url,
        headers={
            'Authorization': f'Bearer {service_key}',
            'apikey': service_key,
        },
    )
    try:
        with urlopen(req) as response:
            data = response.read()
            mime = response.headers.get('Content-Type', 'application/octet-stream')
    except HTTPError:
        return attachment_error_response('File not found.', 404)

    filename = path.rsplit('/', 1)[-1]
    display_name = filename.split('_', 1)[1] if '_' in filename else filename
    return Response(
        data,
        mimetype=mime,
        headers={'Content-Disposition': f'inline; filename="{display_name}"'},
    )

@app.route('/chat/<int:chat_id>/send', methods=['POST'])
def chat_send(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    if request.content_type and 'multipart/form-data' in request.content_type:
        body = (request.form.get('body') or '').strip()
        files = [file for file in request.files.getlist('files') if file and file.filename]
    else:
        body = (request.json or {}).get('body', '').strip()
        files = []

    if not body and not files:
        return jsonify({'error': 'empty message'}), 400

    attachments = []
    try:
        for file_storage in files:
            attachments.append(upload_attachment(file_storage, user_id))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            cursor.execute('SELECT id FROM public.users WHERE id = %s', (chat_id,))
            if not cursor.fetchone():
                return jsonify({'error': 'not found'}), 404

            cursor.execute(
                '''
                INSERT INTO public.messages (sender_id, receiver_id, created_at, body, attachments)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING id
                ''',
                (
                    user_id,
                    chat_id,
                    datetime.now(timezone.utc),
                    body,
                    json.dumps(attachments) if attachments else None,
                ),
            )
            message_id = cursor.fetchone()[0]
            clear_user_typing(cursor, user_id)

    return jsonify({'ok': True, 'id': message_id})

@app.route('/chat/<int:chat_id>/site-url', methods=['POST'])
def save_site_url(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    site_url = normalize_site_url((request.json or {}).get('site_url', ''))
    if not site_url:
        return jsonify({'error': 'empty url'}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            cursor.execute('SELECT id FROM public.users WHERE id = %s', (chat_id,))
            if not cursor.fetchone():
                return jsonify({'error': 'not found'}), 404

            cursor.execute(
                'UPDATE public.users SET site_url = %s WHERE id = %s',
                (site_url, chat_id),
            )

    return jsonify({'ok': True, 'site_url': site_url})

@app.route('/chat/<int:chat_id>/progress', methods=['GET', 'POST'])
def chat_progress(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403
            role = get_user_role(cursor, user_id)

            if request.method == 'POST':
                if not is_agency_role(role):
                    return jsonify({'error': 'forbidden'}), 403
                client_id = get_preview_user_id(role, user_id, chat_id)
                payload = request.json or {}

                if 'progress' in payload:
                    try:
                        progress = max(0, min(100, int(payload.get('progress'))))
                    except (TypeError, ValueError):
                        return jsonify({'error': 'invalid progress'}), 400
                    cursor.execute('UPDATE public.users SET progress = %s WHERE id = %s', (progress, client_id))

                if 'delivery_date' in payload:
                    raw = (payload.get('delivery_date') or '').strip()
                    if raw:
                        try:
                            parsed = datetime.strptime(raw, '%Y-%m-%d').date()
                        except ValueError:
                            return jsonify({'error': 'invalid date'}), 400
                        cursor.execute('UPDATE public.users SET delivery_date = %s WHERE id = %s', (parsed, client_id))
                    else:
                        cursor.execute('UPDATE public.users SET delivery_date = NULL WHERE id = %s', (client_id,))

            progress, delivery_date = get_client_progress(cursor, role, user_id, chat_id)

    return jsonify({'progress': progress, 'delivery_date': delivery_date})

@app.route('/chat/<int:chat_id>/delete', methods=['POST'])
def chat_delete(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    paths = []
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403
            # the client must belong to this agency
            cursor.execute('SELECT agency_id FROM public.users WHERE id = %s', (chat_id,))
            row = cursor.fetchone()
            if not row or row[0] != user_id:
                return jsonify({'error': 'forbidden'}), 403
            client_id = chat_id

            cursor.execute(
                "SELECT storage_path FROM public.vault_items WHERE client_id=%s AND kind='file' AND storage_path IS NOT NULL",
                (client_id,),
            )
            paths = [r[0] for r in cursor.fetchall()]

            cursor.execute('DELETE FROM public.vault_items WHERE client_id=%s', (client_id,))
            cursor.execute('DELETE FROM public.form_submissions WHERE client_id=%s', (client_id,))
            cursor.execute('DELETE FROM public.messages WHERE sender_id=%s OR receiver_id=%s', (client_id, client_id))
            try:
                cursor.execute('SAVEPOINT presence_del')
                cursor.execute('DELETE FROM public.user_presence WHERE user_id=%s', (client_id,))
                cursor.execute('RELEASE SAVEPOINT presence_del')
            except psycopg.Error:
                cursor.execute('ROLLBACK TO SAVEPOINT presence_del')

            cursor.execute('SELECT chats FROM public.users WHERE id=%s', (user_id,))
            agency_chats = cursor.fetchone()
            remaining = [i for i in parse_chat_ids(agency_chats[0] if agency_chats else '') if i != client_id]
            cursor.execute('UPDATE public.users SET chats=%s WHERE id=%s', (format_chat_ids(remaining), user_id))

            cursor.execute('DELETE FROM public.users WHERE id=%s', (client_id,))

    for path in paths:
        delete_vault_object(path)
    return jsonify({'ok': True})

@app.route('/chat/<int:chat_id>/todos/reorder', methods=['POST'])
def chat_todos_reorder(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    order = (request.json or {}).get('order')
    if not isinstance(order, list):
        return jsonify({'error': 'invalid order'}), 400
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403
            for index, message_id in enumerate(order):
                if not str(message_id).isdigit():
                    continue
                cursor.execute(
                    '''
                    UPDATE public.messages SET todo_order = %s
                    WHERE id = %s AND form_item_id IS NOT NULL
                      AND ((sender_id=%s AND receiver_id=%s) OR (sender_id=%s AND receiver_id=%s))
                    ''',
                    (index, int(message_id), user_id, chat_id, chat_id, user_id),
                )
    return jsonify({'ok': True})

def _vault_guard(cursor, chat_id):
    """Return (user_id, client_id) if the session user may use this vault, else (None, error_response)."""
    user_id = session.get('user_id')
    if not user_id:
        return None, (jsonify({'error': 'unauthorized'}), 401)
    if not can_message(user_id, chat_id, cursor):
        return None, (jsonify({'error': 'forbidden'}), 403)
    return user_id, get_vault_client_id(cursor, user_id, chat_id)

def _vault_coords(payload):
    try:
        x = float(payload.get('x', 40))
        y = float(payload.get('y', 40))
    except (TypeError, ValueError):
        x, y = 40.0, 40.0
    return x, y

@app.route('/chat/<int:chat_id>/vault')
def vault_list(chat_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id
            items = fetch_vault_items(cursor, client_id, chat_id)
    return jsonify({'items': items})

@app.route('/chat/<int:chat_id>/vault/folder', methods=['POST'])
def vault_create_folder(chat_id):
    payload = request.json or {}
    name = (payload.get('name') or 'New folder').strip()[:120] or 'New folder'
    parent_id = payload.get('parent_id')
    x, y = _vault_coords(payload)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id
            if not vault_parent_ok(cursor, parent_id, client_id):
                return jsonify({'error': 'invalid folder'}), 400

            cursor.execute(
                '''
                INSERT INTO public.vault_items
                    (client_id, parent_id, kind, name, pos_x, pos_y, uploaded_by)
                VALUES (%s, %s, 'folder', %s, %s, %s, %s)
                RETURNING id, parent_id, kind, name, pos_x, pos_y, mime, size_bytes
                ''',
                (client_id, parent_id, name, x, y, user_id),
            )
            item = serialize_vault_item(cursor.fetchone(), chat_id)
    return jsonify({'item': item})

@app.route('/chat/<int:chat_id>/vault/upload', methods=['POST'])
def vault_upload(chat_id):
    parent_raw = request.form.get('parent_id')
    parent_id = int(parent_raw) if parent_raw and parent_raw.isdigit() else None
    x, y = _vault_coords(request.form)
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'error': 'no files'}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id
            if not vault_parent_ok(cursor, parent_id, client_id):
                return jsonify({'error': 'invalid folder'}), 400

            created = []
            for index, file_storage in enumerate(files):
                try:
                    stored = upload_vault_object(file_storage, client_id)
                except ValueError as exc:
                    return jsonify({'error': str(exc)}), 400

                cursor.execute(
                    '''
                    INSERT INTO public.vault_items
                        (client_id, parent_id, kind, name, pos_x, pos_y, storage_path, mime, size_bytes, uploaded_by)
                    VALUES (%s, %s, 'file', %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, parent_id, kind, name, pos_x, pos_y, mime, size_bytes
                    ''',
                    (
                        client_id,
                        parent_id,
                        file_storage.filename[:200],
                        x + index * 28,
                        y + index * 28,
                        stored['path'],
                        stored['mime'],
                        stored['size'],
                        user_id,
                    ),
                )
                created.append(serialize_vault_item(cursor.fetchone(), chat_id))
    return jsonify({'items': created})

@app.route('/chat/<int:chat_id>/vault/item/<int:item_id>/move', methods=['POST'])
def vault_move(chat_id, item_id):
    payload = request.json or {}
    x, y = _vault_coords(payload)
    has_parent = 'parent_id' in payload
    parent_id = payload.get('parent_id')

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id

            cursor.execute(
                'SELECT kind FROM public.vault_items WHERE id = %s AND client_id = %s',
                (item_id, client_id),
            )
            if not cursor.fetchone():
                return jsonify({'error': 'not found'}), 404

            if has_parent:
                if parent_id is not None and not str(parent_id).isdigit():
                    return jsonify({'error': 'invalid folder'}), 400
                parent_id = int(parent_id) if parent_id is not None else None
                if parent_id == item_id:
                    return jsonify({'error': 'cannot nest into itself'}), 400
                if not vault_parent_ok(cursor, parent_id, client_id):
                    return jsonify({'error': 'invalid folder'}), 400
                # Prevent moving a folder into one of its own descendants.
                if parent_id is not None:
                    cursor.execute(
                        '''
                        WITH RECURSIVE descendants AS (
                            SELECT id FROM public.vault_items WHERE id = %s
                            UNION ALL
                            SELECT v.id FROM public.vault_items v
                            JOIN descendants d ON v.parent_id = d.id
                        )
                        SELECT 1 FROM descendants WHERE id = %s
                        ''',
                        (item_id, parent_id),
                    )
                    if cursor.fetchone():
                        return jsonify({'error': 'cannot nest into a subfolder'}), 400

                cursor.execute(
                    'UPDATE public.vault_items SET parent_id = %s, pos_x = %s, pos_y = %s, updated_at = now() WHERE id = %s AND client_id = %s',
                    (parent_id, x, y, item_id, client_id),
                )
            else:
                cursor.execute(
                    'UPDATE public.vault_items SET pos_x = %s, pos_y = %s, updated_at = now() WHERE id = %s AND client_id = %s',
                    (x, y, item_id, client_id),
                )
    return jsonify({'ok': True})

@app.route('/chat/<int:chat_id>/vault/item/<int:item_id>/rename', methods=['POST'])
def vault_rename(chat_id, item_id):
    name = ((request.json or {}).get('name') or '').strip()[:200]
    if not name:
        return jsonify({'error': 'empty name'}), 400
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id
            cursor.execute(
                'UPDATE public.vault_items SET name = %s, updated_at = now() WHERE id = %s AND client_id = %s',
                (name, item_id, client_id),
            )
            if not cursor.rowcount:
                return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True, 'name': name})

@app.route('/chat/<int:chat_id>/vault/item/<int:item_id>/delete', methods=['POST'])
def vault_delete(chat_id, item_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            user_id, client_id = _vault_guard(cursor, chat_id)
            if user_id is None:
                return client_id

            # Collect storage paths of this item and every descendant file first.
            cursor.execute(
                '''
                WITH RECURSIVE tree AS (
                    SELECT id, storage_path, kind FROM public.vault_items
                    WHERE id = %s AND client_id = %s
                    UNION ALL
                    SELECT v.id, v.storage_path, v.kind FROM public.vault_items v
                    JOIN tree t ON v.parent_id = t.id
                )
                SELECT storage_path FROM tree WHERE kind = 'file' AND storage_path IS NOT NULL
                ''',
                (item_id, client_id),
            )
            paths = [r[0] for r in cursor.fetchall()]

            cursor.execute(
                'DELETE FROM public.vault_items WHERE id = %s AND client_id = %s',
                (item_id, client_id),
            )
            if not cursor.rowcount:
                return jsonify({'error': 'not found'}), 404

    for path in paths:
        delete_vault_object(path)
    return jsonify({'ok': True})

@app.route('/chat/<int:chat_id>/vault/item/<int:item_id>/file')
def vault_file(chat_id, item_id):
    user_id = session.get('user_id')
    if not user_id:
        return attachment_error_response('Sign in to view this file.', 401)

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return attachment_error_response('You do not have access to this file.', 403)
            client_id = get_vault_client_id(cursor, user_id, chat_id)
            cursor.execute(
                "SELECT storage_path, mime, name FROM public.vault_items "
                "WHERE id = %s AND client_id = %s AND kind = 'file'",
                (item_id, client_id),
            )
            row = cursor.fetchone()

    if not row or not row[0]:
        return attachment_error_response('File not found.', 404)

    storage_path, mime, name = row
    supabase_url, service_key = get_supabase_config()
    if not supabase_url or not service_key:
        return attachment_error_response('File storage is not configured.', 503)

    download_url = f'{supabase_url}/storage/v1/object/{VAULT_BUCKET}/{quote(storage_path, safe="/")}'
    req = Request(
        download_url,
        headers={'Authorization': f'Bearer {service_key}', 'apikey': service_key},
    )
    try:
        with urlopen(req) as response:
            data = response.read()
            resolved_mime = response.headers.get('Content-Type', mime or 'application/octet-stream')
    except HTTPError:
        return attachment_error_response('File not found.', 404)

    return Response(
        data,
        mimetype=resolved_mime,
        headers={'Content-Disposition': f'inline; filename="{name}"'},
    )

# ---- Form builder (agency dashboard) ----

@app.route('/forms')
def forms_page():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('sign_in'))
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not is_agency_role(get_user_role(cursor, user_id)):
                return redirect(url_for('dashboard'))
    return render_template('forms.html')

@app.route('/forms/list')
def forms_list():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            cursor.execute(
                '''
                SELECT id, parent_id, kind, name, pos_x, pos_y,
                       COALESCE(jsonb_array_length(schema->'fields'), 0)
                FROM public.form_items WHERE agency_id = %s ORDER BY created_at ASC
                ''',
                (agency_id,),
            )
            items = [serialize_form_item(r) for r in cursor.fetchall()]
    return jsonify({'items': items})

@app.route('/forms/folder', methods=['POST'])
def forms_folder():
    payload = request.json or {}
    name = (payload.get('name') or 'New folder').strip()[:120] or 'New folder'
    parent_id = payload.get('parent_id')
    x, y = _vault_coords(payload)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            if parent_id is not None:
                cursor.execute("SELECT 1 FROM public.form_items WHERE id=%s AND agency_id=%s AND kind='folder'", (parent_id, agency_id))
                if not cursor.fetchone():
                    return jsonify({'error': 'invalid folder'}), 400
            cursor.execute(
                "INSERT INTO public.form_items (agency_id, parent_id, kind, name, pos_x, pos_y) "
                "VALUES (%s, %s, 'folder', %s, %s, %s) "
                "RETURNING id, parent_id, kind, name, pos_x, pos_y, 0",
                (agency_id, parent_id, name, x, y),
            )
            item = serialize_form_item(cursor.fetchone())
    return jsonify({'item': item})

@app.route('/forms/save', methods=['POST'])
def forms_save():
    payload = request.json or {}
    name = (payload.get('name') or 'Untitled form').strip()[:200] or 'Untitled form'
    schema = normalize_form_schema(payload.get('schema'))
    item_id = payload.get('id')
    parent_id = payload.get('parent_id')
    x, y = _vault_coords(payload)
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            if item_id:
                cursor.execute(
                    "UPDATE public.form_items SET name=%s, schema=%s, updated_at=now() "
                    "WHERE id=%s AND agency_id=%s AND kind='form' "
                    "RETURNING id, parent_id, kind, name, pos_x, pos_y, COALESCE(jsonb_array_length(schema->'fields'),0)",
                    (name, json.dumps(schema), item_id, agency_id),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({'error': 'not found'}), 404
            else:
                if parent_id is not None:
                    cursor.execute("SELECT 1 FROM public.form_items WHERE id=%s AND agency_id=%s AND kind='folder'", (parent_id, agency_id))
                    if not cursor.fetchone():
                        parent_id = None
                cursor.execute(
                    "INSERT INTO public.form_items (agency_id, parent_id, kind, name, pos_x, pos_y, schema) "
                    "VALUES (%s, %s, 'form', %s, %s, %s, %s) "
                    "RETURNING id, parent_id, kind, name, pos_x, pos_y, COALESCE(jsonb_array_length(schema->'fields'),0)",
                    (agency_id, parent_id, name, x, y, json.dumps(schema)),
                )
                row = cursor.fetchone()
            item = serialize_form_item(row)
    return jsonify({'item': item})

@app.route('/forms/item/<int:item_id>')
def forms_get(item_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            cursor.execute(
                "SELECT id, name, COALESCE(schema, '{}'::jsonb) FROM public.form_items "
                "WHERE id=%s AND agency_id=%s AND kind='form'",
                (item_id, agency_id),
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': 'not found'}), 404
    return jsonify({'id': row[0], 'name': row[1], 'schema': row[2]})

@app.route('/forms/item/<int:item_id>/move', methods=['POST'])
def forms_move(item_id):
    payload = request.json or {}
    x, y = _vault_coords(payload)
    has_parent = 'parent_id' in payload
    parent_id = payload.get('parent_id')
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            cursor.execute('SELECT 1 FROM public.form_items WHERE id=%s AND agency_id=%s', (item_id, agency_id))
            if not cursor.fetchone():
                return jsonify({'error': 'not found'}), 404
            if has_parent:
                if parent_id is not None:
                    if not str(parent_id).isdigit():
                        return jsonify({'error': 'invalid folder'}), 400
                    parent_id = int(parent_id)
                    if parent_id == item_id:
                        return jsonify({'error': 'cannot nest into itself'}), 400
                    cursor.execute("SELECT 1 FROM public.form_items WHERE id=%s AND agency_id=%s AND kind='folder'", (parent_id, agency_id))
                    if not cursor.fetchone():
                        return jsonify({'error': 'invalid folder'}), 400
                    cursor.execute(
                        '''
                        WITH RECURSIVE descendants AS (
                            SELECT id FROM public.form_items WHERE id = %s
                            UNION ALL
                            SELECT f.id FROM public.form_items f JOIN descendants d ON f.parent_id = d.id
                        )
                        SELECT 1 FROM descendants WHERE id = %s
                        ''',
                        (item_id, parent_id),
                    )
                    if cursor.fetchone():
                        return jsonify({'error': 'cannot nest into a subfolder'}), 400
                cursor.execute('UPDATE public.form_items SET parent_id=%s, pos_x=%s, pos_y=%s, updated_at=now() WHERE id=%s AND agency_id=%s', (parent_id, x, y, item_id, agency_id))
            else:
                cursor.execute('UPDATE public.form_items SET pos_x=%s, pos_y=%s, updated_at=now() WHERE id=%s AND agency_id=%s', (x, y, item_id, agency_id))
    return jsonify({'ok': True})

@app.route('/forms/item/<int:item_id>/rename', methods=['POST'])
def forms_rename(item_id):
    name = ((request.json or {}).get('name') or '').strip()[:200]
    if not name:
        return jsonify({'error': 'empty name'}), 400
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            cursor.execute('UPDATE public.form_items SET name=%s, updated_at=now() WHERE id=%s AND agency_id=%s', (name, item_id, agency_id))
            if not cursor.rowcount:
                return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True, 'name': name})

@app.route('/forms/item/<int:item_id>/delete', methods=['POST'])
def forms_delete(item_id):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            agency_id, err = forms_agency_guard(cursor)
            if agency_id is None:
                return err
            cursor.execute('DELETE FROM public.form_items WHERE id=%s AND agency_id=%s', (item_id, agency_id))
            if not cursor.rowcount:
                return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True})

# ---- Forms in chat (send + submit) ----

@app.route('/chat/<int:chat_id>/forms')
def chat_forms_list(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403
            cursor.execute(
                "SELECT id, name, COALESCE(jsonb_array_length(schema->'fields'),0) "
                "FROM public.form_items WHERE agency_id=%s AND kind='form' ORDER BY name ASC",
                (user_id,),
            )
            forms = [{'id': r[0], 'name': r[1], 'fields': r[2]} for r in cursor.fetchall()]
    return jsonify({'forms': forms})

@app.route('/chat/<int:chat_id>/form/send', methods=['POST'])
def chat_form_send(chat_id):
    form_item_id = (request.json or {}).get('form_item_id')
    if not form_item_id:
        return jsonify({'error': 'missing form'}), 400
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403
            if not is_agency_role(get_user_role(cursor, user_id)):
                return jsonify({'error': 'forbidden'}), 403
            cursor.execute("SELECT name FROM public.form_items WHERE id=%s AND agency_id=%s AND kind='form'", (form_item_id, user_id))
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': 'form not found'}), 404
            cursor.execute(
                'INSERT INTO public.messages (sender_id, receiver_id, created_at, body, form_item_id) '
                'VALUES (%s, %s, %s, %s, %s) RETURNING id',
                (user_id, chat_id, datetime.now(timezone.utc), f'Form: {row[0]}', form_item_id),
            )
            message_id = cursor.fetchone()[0]
    return jsonify({'ok': True, 'id': message_id})

@app.route('/chat/<int:chat_id>/form/<int:message_id>/submit', methods=['POST'])
def chat_form_submit(chat_id, message_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            cursor.execute('SELECT sender_id, receiver_id, form_item_id FROM public.messages WHERE id=%s', (message_id,))
            mrow = cursor.fetchone()
            if not mrow or not mrow[2]:
                return jsonify({'error': 'not a form'}), 404
            sender_id, receiver_id, form_item_id = mrow
            if user_id not in (sender_id, receiver_id):
                return jsonify({'error': 'forbidden'}), 403

            cursor.execute('SELECT 1 FROM public.form_submissions WHERE message_id=%s', (message_id,))
            if cursor.fetchone():
                return jsonify({'error': 'already submitted'}), 409

            cursor.execute("SELECT agency_id, name, COALESCE(schema, '{}'::jsonb) FROM public.form_items WHERE id=%s", (form_item_id,))
            frow = cursor.fetchone()
            if not frow:
                return jsonify({'error': 'form unavailable'}), 404
            agency_id, form_name, schema = frow
            fields = schema.get('fields', []) if isinstance(schema, dict) else []
            client_id = receiver_id if receiver_id != agency_id else sender_id

            answers = []
            for field in fields:
                fid, ftype, label = field['id'], field['type'], field['label']
                if ftype == 'file':
                    files = [f for f in request.files.getlist(fid) if f and f.filename]
                    if field.get('required') and not files:
                        return jsonify({'error': f'{label} is required'}), 400
                    folder = field.get('folder') or ''
                    folder_id = ensure_vault_folder(cursor, client_id, folder, user_id) if folder else None
                    saved = []
                    for fs in files:
                        try:
                            stored = upload_vault_object(fs, client_id)
                        except ValueError as exc:
                            return jsonify({'error': str(exc)}), 400
                        cursor.execute(
                            "INSERT INTO public.vault_items (client_id, parent_id, kind, name, pos_x, pos_y, storage_path, mime, size_bytes, uploaded_by) "
                            "VALUES (%s, %s, 'file', %s, 40, 40, %s, %s, %s, %s)",
                            (client_id, folder_id, fs.filename[:200], stored['path'], stored['mime'], stored['size'], user_id),
                        )
                        saved.append(fs.filename)
                    answers.append({'label': label, 'type': 'file', 'files': saved, 'folder': folder})
                else:
                    value = (request.form.get(fid) or '').strip()
                    if field.get('required') and not value:
                        return jsonify({'error': f'{label} is required'}), 400
                    answers.append({'label': label, 'type': ftype, 'value': value})

            cursor.execute(
                'INSERT INTO public.form_submissions (form_item_id, message_id, agency_id, client_id, submitted_by, answers) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                (form_item_id, message_id, agency_id, client_id, user_id, json.dumps(answers)),
            )
    return jsonify({'ok': True})

@app.route('/notifications/poll')
def notifications_poll():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                '''
                SELECT m.id, m.sender_id, COALESCE(u.name, u.email), m.body
                FROM public.messages m
                JOIN public.users u ON u.id = m.sender_id
                WHERE m.receiver_id = %s
                ORDER BY m.created_at DESC
                LIMIT 1
                ''',
                (user_id,),
            )
            row = cursor.fetchone()

    if not row:
        return jsonify({'latest': None})

    body = (row[3] or '').strip()
    if not body:
        snippet = 'Sent you an attachment'
    elif len(body) <= 80:
        snippet = body
    else:
        snippet = body[:77] + '...'

    return jsonify({'latest': {
        'id': row[0],
        'chat_id': row[1],
        'from': row[2],
        'snippet': snippet,
    }})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('sign_in'))
@app.route('/billing/success')
def billing_success():
    user_id = session.get('user_id')
    checkout_session_id = request.args.get('session_id')
    activated = False

    if user_id and checkout_session_id:
        configure_stripe()
        try:
            checkout_session = stripe.checkout.Session.retrieve(checkout_session_id)
            metadata = stripe_field(checkout_session, 'metadata', {})
            client_id = stripe_field(metadata, 'client_id') or stripe_field(checkout_session, 'client_reference_id')
            if str(client_id) == str(user_id):
                activated = fulfill_checkout_session(checkout_session)
        except stripe.error.StripeError as exc:
            print(f'Could not verify checkout session: {exc}')

    return render_template(
        'billing-success.html',
        email=session.get('email'),
        activated=activated,
    )

@app.route('/billing/cancel', methods=['GET', 'POST'])
def billing_cancel():
    if request.method == 'GET':
        return render_template('billing-cancel.html')

    if not session.get('user_id'):
        return jsonify({'error': 'unauthorized'}), 401

    configure_stripe()
    if not stripe.api_key:
        return jsonify({'error': 'billing not configured'}), 500

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not get_user_pro_status(cursor, session.get('user_id')):
                return jsonify({'error': 'no active subscription'}), 400

            cursor.execute(
                'SELECT stripe_subscription_id FROM public.users WHERE id = %s',
                (session.get('user_id'),),
            )
            row = cursor.fetchone()
            fallback_subscription_id = row[0] if row else None

            customer_id = get_stripe_customer_id(
                cursor,
                session.get('user_id'),
                session.get('email'),
            )

    if not customer_id:
        return jsonify({'error': 'no billing account found'}), 400

    subscription_id = get_active_subscription_id(customer_id, fallback_subscription_id)
    if not subscription_id:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                set_user_pro_status(cursor, session.get('user_id'), False)
        return jsonify({'url': url_for('settings')})

    try:
        stripe.Subscription.cancel(subscription_id)
    except stripe.error.StripeError as exc:
        return jsonify({'error': f'could not cancel subscription: {exc.user_message or str(exc)}'}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            set_user_pro_status(cursor, session.get('user_id'), False)

    return jsonify({'url': url_for('settings')})


@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    if not session.get('user_id'):
        return jsonify({'error': 'unauthorized'}), 401

    configure_stripe()
    price_id = os.getenv('STRIPE_PRICE_ID')
    if not stripe.api_key or not price_id:
        return jsonify({'error': 'billing not configured'}), 500

    user_id = str(session.get('user_id'))
    try:
        checkout = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            success_url=external_url('billing_success') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=external_url('billing_cancel'),
            customer_email=session.get('email'),
            client_reference_id=user_id,
            metadata={'client_id': user_id},
            subscription_data={'metadata': {'client_id': user_id}},
        )
    except stripe.error.StripeError as exc:
        print(f'Checkout session failed: {exc}')
        message = exc.user_message or str(exc)
        if 'No such price' in message:
            message = 'Billing is misconfigured. The price ID does not match your live Stripe account.'
        return jsonify({'error': message}), 400

    return jsonify({'url': checkout.url})

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    configure_stripe()
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, os.getenv('STRIPE_WEBHOOK_SECRET'),
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return '', 400

    event_type = event['type']
    data_object = event['data']['object']

    if event_type == 'checkout.session.completed':
        print(f'PAID: checkout session {data_object.get("id")}')
        fulfill_checkout_session(data_object)

    if event_type in ('customer.subscription.deleted', 'customer.subscription.updated'):
        subscription = data_object
        status = subscription.get('status')
        client_id = subscription.get('metadata', {}).get('client_id')
        subscription_id = subscription.get('id')
        customer_id = subscription.get('customer')

        if event_type == 'customer.subscription.deleted' or status in ('canceled', 'unpaid', 'incomplete_expired'):
            print(f'CANCELLED: deactivate subscription {subscription_id} for client {client_id}')
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    deactivate_user_by_subscription(cursor, subscription_id, client_id, customer_id)

    return '', 200
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
