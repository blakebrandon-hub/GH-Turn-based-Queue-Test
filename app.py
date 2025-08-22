from flask import Flask, render_template, request, session, flash, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms
from flask_login import LoginManager, UserMixin, current_user, login_user, login_required, logout_user
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, exc, exists
from gevent import monkey; monkey.patch_all()
import replicate
import requests
from random import randint
from datetime import datetime
from time import time
import os
import string
import random
from collections import deque


# ===== Global vars =====
messages = {}         # room_id -> list of {username, text} (already exists)
user_rooms = {}       # sid -> room_id (already exists)
room_turn_queues = {}  # room_name -> deque of usernames for turn order
MAIN_ROOM_NAME = "Main Room"
API_KEY = 'AIzaSyC80EUqt8ErvmmJ-Q-5Srq2L72Ur0D3Mmg'

# Flask setup
app = Flask(__name__)
app.config['SECRET_KEY'] = "dsfsdfdsfsdfgdfgsdfgsfghhhhh333"

# Flask SocketIO setup
socketio = SocketIO(
    app,
    async_mode="gevent",
    cors_allowed_origins="*",
    ping_interval=25,
    ping_timeout=60,
)

# Flask CORS setup
CORS(app)

# DB setup
uri = os.getenv("DATABASE_URL", "sqlite:///local.db")
if uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = uri

db = SQLAlchemy(app)

# Login setup
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@app.context_processor
def inject_user():
    return dict(current_user=current_user)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Room clock
room_clock = {}  # room_name -> {'video_id': str, 'base': float, 'started_at': float|None}

def _pos(room):
    s = room_clock.get(room)
    if not s:
        return 0.0
    return s['base'] if s['started_at'] is None else s['base'] + (time() - s['started_at'])

# ===== Models =====
class Room(db.Model):
    __tablename__ = 'rooms'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(24), unique=True, nullable=False, index=True)
    is_private = db.Column(db.Boolean, default=False, nullable=False)
    created_by = db.Column(db.String(150))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class User(UserMixin, db.Model):
    __tablename__ = 'users'  
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

class QueueItem(db.Model):
    __tablename__ = 'queue_items'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    video_url = db.Column(db.String(255), nullable=False)
    video_title = db.Column(db.String(255))
    video_duration = db.Column(db.Integer)
    added_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Add relationships for easier querying
    room = db.relationship('Room', backref='queue_items')
    user = db.relationship('User', backref='queue_items')

class RoomTurnOrder(db.Model):
    __tablename__ = 'room_turn_orders'
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    username = db.Column(db.String(150), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    
    room = db.relationship('Room', backref='turn_orders')

# ===== Turn-based helpers =====
def get_turn_queue(room_name):
    """Get the turn queue for a room, creating it if it doesn't exist"""
    if room_name not in room_turn_queues:
        room = get_room_by_name(room_name)
        if room:
            turn_orders = RoomTurnOrder.query.filter_by(room_id=room.id)\
                                             .order_by(RoomTurnOrder.position).all()
            room_turn_queues[room_name] = deque([to.username for to in turn_orders])
        else:
            room_turn_queues[room_name] = deque()
    return room_turn_queues[room_name]

def save_turn_queue(room_name):
    """Persist the queue order to DB"""
    room = get_room_by_name(room_name)
    if not room:
        return
    RoomTurnOrder.query.filter_by(room_id=room.id).delete()
    for position, username in enumerate(get_turn_queue(room_name)):
        db.session.add(RoomTurnOrder(room_id=room.id, username=username, position=position))
    try:
        db.session.commit()
    except:
        db.session.rollback()

def get_current_turn(room_name):
    tq = get_turn_queue(room_name)
    return tq[0] if tq else None

def add_user_to_turns(room_name, username):
    tq = get_turn_queue(room_name)
    if username not in tq:
        tq.append(username)
        save_turn_queue(room_name)
        return True
    return False

def advance_turn(room_name):
    """Rotate head -> tail and return new head"""
    tq = get_turn_queue(room_name)
    if not tq:
        return None
    current_user_name = tq.popleft()
    tq.append(current_user_name)
    save_turn_queue(room_name)
    return tq[0] if tq else None

def remove_user_from_turns(room_name, username):
    tq = get_turn_queue(room_name)
    if username in tq:
        was_head = (tq[0] == username)
        tq.remove(username)
        # If we removed the head, nothing else to do—new head is already correct
        save_turn_queue(room_name)

def broadcast_turn_update(room_name):
    socketio.emit('turn_update', {
        'current_turn': get_current_turn(room_name),
        'turn_queue': list(get_turn_queue(room_name))
    }, room=room_name)

# ===== Existing Helpers =====
def get_display_name():
    if current_user.is_authenticated:
        return current_user.username
    if 'username' not in session or not session['username'].startswith('guest-'):
        session['username'] = f"guest-{randint(1000, 9999)}"
    return session['username']

def ensure_main_room():
    existing = Room.query.filter(func.lower(Room.name) == MAIN_ROOM_NAME.lower()).first()
    if existing: return existing
    try:
        r = Room(name=MAIN_ROOM_NAME, is_private=False, created_by=None)
        db.session.add(r); db.session.commit()
        return r
    except:
        db.session.rollback()
        return Room.query.filter(func.lower(Room.name) == MAIN_ROOM_NAME.lower()).first()

def get_user_room(sid):
    return user_rooms.get(sid) or MAIN_ROOM_NAME

def get_current_video(room_id):
    return QueueItem.query.filter_by(room_id=room_id).order_by(QueueItem.id).first()

def user_can_moderate(user_id, room_name):
    if not current_user.is_authenticated:
        return False
    if current_user.is_admin:
        return True
    room = get_room_by_name(room_name)
    if room and room.created_by == current_user.username:
        return True
    if current_user.username == 'Blake' and room_name == MAIN_ROOM_NAME:
        return True
    return False

def get_room_by_name(room_name):
    return Room.query.filter(func.lower(Room.name) == room_name.lower()).first()

def get_queue_for_room(room_name):
    room = get_room_by_name(room_name)
    if not room:
        return []
    return QueueItem.query.filter_by(room_id=room.id).order_by(QueueItem.id).all()

def get_current_video_for_room(room_name):
    room = get_room_by_name(room_name)
    if not room:
        return None
    return QueueItem.query.filter_by(room_id=room.id).order_by(QueueItem.id).first()

def convert_queue_to_old_format(queue_items):
    video_queue_old_format = {}
    for index, item in enumerate(queue_items):
        video_id = extract_video_id(item.video_url)
        video_queue_old_format[index] = {'video_id': video_id, 'title': item.video_title}
    return video_queue_old_format

def extract_video_id(video_url):
    if 'v=' in video_url:
        return video_url.split('v=')[1].split('&')[0]
    elif 'youtu.be/' in video_url:
        return video_url.split('youtu.be/')[1].split('?')[0]
    else:
        return video_url

def skip_current_video(room_name):
    room = get_room_by_name(room_name)
    if not room:
        return None
    current_video = QueueItem.query.filter_by(room_id=room.id).order_by(QueueItem.id).first()
    if current_video:
        db.session.delete(current_video)
        db.session.commit()
        next_video = QueueItem.query.filter_by(room_id=room.id).order_by(QueueItem.id).first()
        return next_video
    return None

# ===== Auth routes =====
@app.route('/logout')
@login_required
def logout():
    logout_user()
    session.pop('username', None)
    return redirect(url_for('index'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not username or not password:
            return render_template('register.html', error="Username and password required.")
        if len(password) < 8:
            return render_template('register.html', error="Password must be at least 8 characters long.")
        if User.query.filter_by(username=username).first():
            return render_template('register.html', error="Username already exists.")
        user = User(username=username, password=generate_password_hash(password))
        db.session.add(user); db.session.commit()
        login_user(user)
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            session['username'] = user.username
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid credentials.")
    return render_template('login.html')

# ===== Main routes =====
@app.route('/')
@login_required
def index():
    mod = (get_display_name() == 'Blake')
    return render_template('index.html', room_name=MAIN_ROOM_NAME, username=get_display_name(), mod=mod)

@app.route('/rooms')
@login_required
def rooms_page():
    public_rooms = Room.query.filter_by(is_private=False).all()
    rooms_with_counts = []
    for room in public_rooms:
        user_count = sum(1 for sid, room_name in user_rooms.items() if room_name == room.name)
        rooms_with_counts.append({'room': room, 'user_count': user_count})
    return render_template('rooms.html', username=get_display_name(), rooms_with_counts=rooms_with_counts)

@app.route('/create_room', methods=['GET', 'POST'])
@login_required
def create_room():
    if request.method == 'POST':
        name = (request.form.get('room_name') or "").strip()
        is_private = bool(request.form.get('private'))
        if not name:
            flash("Room name is required.", "warning"); return redirect(url_for('create_room'))
        if Room.query.filter_by(name=name).first():
            flash("Room name already exists.", "danger"); return redirect(url_for('create_room'))
        if len(name) > 24:
            flash("Room name must be 24 characters or less.", 'warning'); return redirect(url_for('create_room'))
        room = Room(name=name, is_private=is_private, created_by=get_display_name())
        db.session.add(room); db.session.commit()
        return redirect(url_for('join_room_page', room_name=room.name))
    return render_template('create_room.html')

@app.route('/rooms/<path:room_name>/delete/confirm', endpoint='confirm_delete_room', methods=["GET", "POST"])
@login_required
def confirm_delete_room(room_name):
    room = Room.query.filter(func.lower(Room.name) == room_name.lower()).first_or_404()
    return render_template('confirm_delete.html', room=room)

@app.route('/rooms/<path:room_name>/delete', methods=['POST'])
@login_required
def delete_room(room_name):
    room = Room.query.filter(func.lower(Room.name) == room_name.lower()).first_or_404()
    db.session.delete(room)
    db.session.commit()
    flash('Room deleted.', 'success')
    return redirect(url_for('rooms_page'))

@app.route('/join/<path:room_name>')
@login_required
def join_room_page(room_name):
    room = Room.query.filter(func.lower(Room.name) == room_name.lower()).first_or_404()
    session['current_room'] = room.name
    mod = (current_user.username == room.created_by)
    return render_template('index.html', room_name=room.name, username=current_user.username, private=bool(room.is_private), mod=mod)

@app.route('/remove_video/<int:video_id>', methods=['DELETE'])
@login_required
def remove_video(video_id):
    video = QueueItem.query.get_or_404(video_id)
    room_name = video.room.name
    if video.added_by != current_user.id and not user_can_moderate(current_user.id, room_name):
        return jsonify({'error': 'Permission denied'}), 403
    db.session.delete(video); db.session.commit()
    queue_items = get_queue_for_room(room_name)
    socketio.emit('video_added', convert_queue_to_old_format(queue_items), room=room_name)
    return jsonify({'success': True})

@app.route('/api/queue/<path:room_name>')
def get_queue_api(room_name):
    queue_items = get_queue_for_room(room_name)
    queue_data = [{
        'id': item.id,
        'url': item.video_url,
        'title': item.video_title,
        'duration': item.video_duration,
        'added_by': item.user.username if item.user else 'Unknown',
        'added_at': item.added_at.strftime('%H:%M:%S')
    } for item in queue_items]
    return jsonify({'queue': queue_data})

@app.get("/api/yt/search")
def yt_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"items": []})
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "type": "video", "maxResults": 8, "q": q, "key": API_KEY}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        return jsonify(r.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"YouTube API request failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Search failed: {str(e)}"}), 500

# ===== Socket.IO handlers =====
@socketio.on('connect')
def on_connect():
    room_name = session.get('current_room') or MAIN_ROOM_NAME
    join_room(room_name)
    user_rooms[request.sid] = room_name

    if room_name not in messages:
        messages[room_name] = []

    username = get_display_name()
    added = add_user_to_turns(room_name, username)

    room = get_room_by_name(room_name)
    is_private = room.is_private if room else False

    emit('room_joined', {
        'room': room_name,
        'username': username,
        'is_private': is_private,
        'current_turn': get_current_turn(room_name),
        'turn_queue': list(get_turn_queue(room_name))
    }, to=request.sid)

    if added:
        broadcast_turn_update(room_name)

@socketio.on('join_room')
def handle_join(data):
    room_name = (data or {}).get('room') or MAIN_ROOM_NAME
    username = (data or {}).get('username') or get_display_name()

    private = False
    room = get_room_by_name(room_name)
    if room and room.is_private:
        private = True
    
    join_room(room_name)
    session['current_room'] = room_name
    user_rooms[request.sid] = room_name

    if not room:
        ensure_main_room()
        room = get_room_by_name(room_name)

    added = add_user_to_turns(room_name, username)

    if room_name not in messages:
        messages[room_name] = []

    emit('chat_history', messages[room_name], to=request.sid)

    queue_items = get_queue_for_room(room_name)
    current_video_id = extract_video_id(queue_items[0].video_url) if queue_items else ''

    emit('sync_video', {
        'video_queue': convert_queue_to_old_format(queue_items),
        'time': 0,
        'current_video': current_video_id
    }, to=request.sid)

    emit('room_joined', {
        'room': room_name,
        'username': username,
        'is_private': private,
        'current_turn': get_current_turn(room_name),
        'turn_queue': list(get_turn_queue(room_name))
    }, to=request.sid)

    join_msg = {'username': '', 'text': f'{username} joined the room'}
    messages[room_name].append(join_msg)
    emit('new_message', join_msg, room=room_name)

    emit('user_joined', {
        'username': username,
        'current_turn': get_current_turn(room_name),
        'turn_queue': list(get_turn_queue(room_name))
    }, room=room_name)

    if added:
        broadcast_turn_update(room_name)

@socketio.on('chat_message')
def handle_chat_message(data):
    room = get_user_room(request.sid)
    text = (data or {}).get('text')
    if not text:
        return
    username = (data or {}).get('username') or get_display_name()
    msg = {'username': username, 'text': text}
    messages[room].append(msg)
    emit('new_message', msg, room=room)

@socketio.on('clear_queue')
@login_required
def handle_clear_queue():
    room_name = get_user_room(request.sid)
    if not user_can_moderate(current_user.id, room_name):
        return
    room = get_room_by_name(room_name)
    if room:
        QueueItem.query.filter_by(room_id=room.id).delete()
        db.session.commit()
    emit('clear_queue', room=room_name)

@socketio.on('skip_song')
@login_required
def handle_skip_song():
    room_name = get_user_room(request.sid)
    if not user_can_moderate(current_user.id, room_name):
        return

    skip_current_video(room_name)

    queue_items = get_queue_for_room(room_name)
    if queue_items:
        room_clock[room_name] = {'video_id': extract_video_id(queue_items[0].video_url), 'base': 0.0, 'started_at': time()}
    else:
        room_clock[room_name] = {'video_id': '', 'base': 0.0, 'started_at': None}

    emit('video_added', convert_queue_to_old_format(queue_items), room=room_name)
    emit('skip_video', room=room_name)

@socketio.on('update_time')
def handle_update_time(data):
    pass

@socketio.on('add_video')
@login_required
def handle_add_video_socket(data):
    title = data.get('title')
    video_id = data.get('video_id')
    if not title or not video_id:
        return

    room_name = session.get('current_room') or MAIN_ROOM_NAME
    room = get_room_by_name(room_name)
    if not room:
        return

    username = get_display_name()
    is_mod = user_can_moderate(current_user.id, room_name)
    current_turn = get_current_turn(room_name)

    # Enforce turn unless moderator
    if not is_mod and (current_turn is not None) and (username != current_turn):
        emit('turn_error', {'message': f"Not your turn. It's {current_turn}'s turn."}, to=request.sid)
        return

    video_url = f'https://www.youtube.com/watch?v={video_id}'
    qi = QueueItem(room_id=room.id, video_url=video_url, video_title=title, video_duration=0, added_by=current_user.id)

    try:
        db.session.add(qi)
        db.session.commit()

        # Start clock if first item
        if QueueItem.query.filter_by(room_id=room.id).count() == 1:
            room_clock[room_name] = {'video_id': video_id, 'base': 0.0, 'started_at': time()}

        socketio.emit('video_added', convert_queue_to_old_format(get_queue_for_room(room_name)), room=room_name)

        # ALWAYS advance turn after a counted add (even if moderator), so no one gets stuck at head
        advance_turn(room_name)
        broadcast_turn_update(room_name)

    except Exception as e:
        db.session.rollback()
        print(f"Error adding video: {e}")

@socketio.on('video_ended')
def handle_video_ended():
    room_name = get_user_room(request.sid)
    skip_current_video(room_name)

    queue_items = get_queue_for_room(room_name)
    current_video_id = extract_video_id(queue_items[0].video_url) if queue_items else ''

    if queue_items:
        room_clock[room_name] = {'video_id': current_video_id, 'base': 0.0, 'started_at': time()}
    else:
        room_clock[room_name] = {'video_id': '', 'base': 0.0, 'started_at': None}

    emit('video_ended', room=room_name)
    emit('sync_video', {
        'video_queue': convert_queue_to_old_format(queue_items),
        'time': 0,
        'current_video': current_video_id
    }, room=room_name)

@socketio.on('request_sync')
def handle_request_sync():
    room_name = get_user_room(request.sid)
    queue_items = get_queue_for_room(room_name)
    head_id = extract_video_id(queue_items[0].video_url) if queue_items else ''
    s = room_clock.get(room_name)

    if head_id and (not s or s.get('video_id') != head_id):
        room_clock[room_name] = {'video_id': head_id, 'base': 0.0, 'started_at': time()}
    elif not head_id:
        room_clock[room_name] = {'video_id': '', 'base': 0.0, 'started_at': None}

    emit('sync', {'time': round(_pos(room_name), 2), 'current_video': head_id, 'server_ts': time()}, room=request.sid)

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in user_rooms:
        room_name = user_rooms[request.sid]
        username = session.get('username') or get_display_name()

        remove_user_from_turns(room_name, username)

        if room_name not in messages:
            messages[room_name] = []

        leave_msg = {'username': '', 'text': f'{username} left the room'}
        messages[room_name].append(leave_msg)
        emit('new_message', leave_msg, room=room_name)

        broadcast_turn_update(room_name)
        del user_rooms[request.sid]


# ===== Bootstrap DB =====
with app.app_context():
    db.create_all()
    ensure_main_room()
    print("Database tables created.")

# ===== Run =====
if __name__ == '__main__':
    print("127.0.0.1:5000")
    socketio.run(app, debug=True)
