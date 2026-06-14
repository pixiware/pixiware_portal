from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os
import psycopg
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
import stripe
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
        SELECT id, sender_id, body
        FROM public.messages
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
        ORDER BY created_at ASC
        ''',
        (user_id, chat_id, chat_id, user_id),
    )
    messages = [
        {
            'id': msg_id,
            'sender': 'you' if sender_id == user_id else other_name,
            'body': body,
        }
        for msg_id, sender_id, body in cursor.fetchall()
    ]
    return other_name, messages

def get_preview_user_id(user_id, chat_id):
    if user_id == ADMIN_ACC_ID:
        return chat_id
    return user_id

def get_site_url(cursor, user_id, chat_id):
    preview_user_id = get_preview_user_id(user_id, chat_id)
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

_load_env_file()
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev')
DB_URL = os.environ.get('DB_URL')
ADMIN_ACC_ID = 7


def get_db_connection():
    return psycopg.connect(DB_URL, prepare_threshold=None)


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
                'SELECT id, email, chats FROM public.users WHERE id = %s',
                (user_id,),
            )
            row = cursor.fetchone()

            if not row:
                session.clear()
                return redirect(url_for('sign_in'))

            chat_ids = parse_chat_ids(row[2])
            chats = []
            if chat_ids:
                cursor.execute(
                    'SELECT COALESCE(name, email), id FROM public.users WHERE id = ANY(%s)',
                    (chat_ids,),
                )
                chats = cursor.fetchall()

    return render_template('dashboard.html', user_id=row[0], email=row[1], chats=chats)

@app.route('/settings')
def settings():
    if not session.get('user_id'):
        return redirect(url_for('sign_in'))
    return render_template('settings.html', email=session.get('email'))

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
                'INSERT INTO public.users (email, password, created_at, name, chats) VALUES (%s, %s, %s, %s, %s) RETURNING id',
                (email, hashed_password, datetime.now(timezone.utc), org_name, f'{ADMIN_ACC_ID},'),
            )
            user_id = cursor.fetchone()[0]

            cursor.execute(
                'INSERT INTO public.messages (sender_id, receiver_id, created_at, body) VALUES (%s, %s, %s, %s)',
                (
                    ADMIN_ACC_ID,
                    user_id,
                    datetime.now(timezone.utc),
                    f'Hello {org_name}, just wanted to say hi and welcome to your pixiware portal',
                ),
            )

            session['user_id'] = int(user_id)
            session['email'] = email
            session.modified = True

            cursor.execute('SELECT chats FROM public.users WHERE id = %s', (ADMIN_ACC_ID,))
            admin_chats = cursor.fetchone()[0]
            admin_chat_ids = parse_chat_ids(admin_chats)
            if user_id not in admin_chat_ids:
                admin_chat_ids.append(user_id)
            cursor.execute(
                'UPDATE public.users SET chats = %s WHERE id = %s',
                (format_chat_ids(admin_chat_ids), ADMIN_ACC_ID),
            )

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

            site_url = get_site_url(cursor, user_id, chat_id)

    is_admin = user_id == ADMIN_ACC_ID
    return render_template(
        'chat.html',
        chat_id=chat_id,
        conversation_list=conversation_list,
        other_persons_name=other_persons_name,
        site_url=site_url,
        admin=is_admin,
        user_id=user_id,
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

@app.route('/chat/<int:chat_id>/send', methods=['POST'])
def chat_send(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401

    body = (request.json or {}).get('body', '').strip()
    if not body:
        return jsonify({'error': 'empty message'}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            if not can_message(user_id, chat_id, cursor):
                return jsonify({'error': 'forbidden'}), 403

            cursor.execute('SELECT id FROM public.users WHERE id = %s', (chat_id,))
            if not cursor.fetchone():
                return jsonify({'error': 'not found'}), 404

            cursor.execute(
                'INSERT INTO public.messages (sender_id, receiver_id, created_at, body) VALUES (%s, %s, %s, %s) RETURNING id',
                (user_id, chat_id, datetime.now(timezone.utc), body),
            )
            message_id = cursor.fetchone()[0]

    return jsonify({'ok': True, 'id': message_id})

@app.route('/chat/<int:chat_id>/site-url', methods=['POST'])
def save_site_url(chat_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'unauthorized'}), 401
    if user_id != ADMIN_ACC_ID:
        return jsonify({'error': 'forbidden'}), 403

    site_url = normalize_site_url((request.json or {}).get('site_url', ''))
    if not site_url:
        return jsonify({'error': 'empty url'}), 400

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
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

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('sign_in'))

@app.route('/create-checkout-session',methods=['POST'])
def create_checkout_session():
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    checkout = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": os.getenv("STRIPE_PRICE_ID"), "quantity": 1}],
        success_url="https://portal.pixiware.co.uk/billing/success",
        cancel_url="https://portal.pixiware.co.uk/billing/cancel",
        metadata={"client_id": "test_client_123"},  # hardcoded for now; real client_id later
    )
    print('successfully processed payment request')
    return jsonify({"url": checkout.url})

if __name__ == '__main__':
    app.run()
