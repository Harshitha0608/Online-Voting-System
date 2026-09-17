import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import check_password_hash, generate_password_hash

import database
from database import (
    get_db_connection,
    init_db,
    add_admin_if_not_exists,
    get_active_candidates,
    get_all_candidates,
    add_candidate,
    toggle_candidate_status,
    delete_candidate,
    get_all_voters,
    delete_user,
    get_voting_status,
    set_voting_status,
    get_total_votes,
    get_results
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ovs-college-project-secret-key-2026')

# Initialize DB and seed admin / initial candidates safely
init_db()
add_admin_if_not_exists()

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in') or session.get('email') != 'admin@admin.com':
            return render_template('access_denied.html', message="Admin privileges required.")
        return f(*args, **kwargs)
    return decorated_function

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def check_login(email, password):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, email, password FROM users WHERE email = ?', (email,))
    user = cursor.fetchone()
    conn.close()
    if user and check_password_hash(user[3], password):
        return user
    return None

def user_has_voted(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM votes WHERE user_id = ?', (user_id,))
    voted = cursor.fetchone() is not None
    conn.close()
    return voted

def save_vote(user_id, candidate):
    if get_voting_status() != 'started':
        return False
    if user_has_voted(user_id):
        return False
    active_names = [c[1] for c in get_active_candidates()]
    if candidate not in active_names:
        return False
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (user_id, candidate))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


@app.route('/')
def home():
    voting_status = get_voting_status()
    return render_template('index.html', voting_status=voting_status)

@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    user_id = session['user_id']
    has_voted = user_has_voted(user_id)
    voting_status = get_voting_status()
    user_name = session.get('user_name', 'Voter')
    active_candidate_count = len(get_active_candidates())

    return render_template(
        'dashboard.html',
        user_name=user_name,
        has_voted=has_voted,
        voting_status=voting_status,
        active_candidate_count=active_candidate_count
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''

        if not (name and email and password):
            error = "All fields are required!"
        elif '@' not in email or '.' not in email:
            error = "Please enter a valid email address."
        elif email == 'admin@admin.com':
            error = "This email address is reserved."
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM users WHERE email = ?', (email,))
            if cursor.fetchone():
                error = "Email already exists, please login."
                conn.close()
            else:
                hashed_password = generate_password_hash(password)
                cursor.execute(
                    'INSERT INTO users (name, email, password) VALUES (?, ?, ?)',
                    (name, email, hashed_password)
                )
                conn.commit()
                conn.close()
                flash("Registration successful! You can now log in.", "success")
                return redirect(url_for('login'))

    return render_template('register.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        if session.get('admin_logged_in'):
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('dashboard'))

    error = None
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = check_login(email, password)

        if user:
            session['user_id'] = user[0]
            session['user_name'] = user[1] if user[1] else user[2]
            session['email'] = user[2]

            if email == 'admin@admin.com':
                session['admin_logged_in'] = True
                flash("Welcome back, Administrator.", "info")
                return redirect(url_for('admin_dashboard'))
            else:
                session['admin_logged_in'] = False
                flash(f"Welcome back, {session['user_name']}!", "info")
                return redirect(url_for('dashboard'))
        else:
            error = "Invalid email or password."

    return render_template('login.html', error=error)

@app.route('/vote', methods=['GET', 'POST'])
@login_required
def vote():
    if session.get('admin_logged_in'):
        return render_template('access_denied.html', message="Administrators are not permitted to vote.")

    user_id = session['user_id']
    if user_has_voted(user_id):
        return render_template('already_voted.html')

    voting_status = get_voting_status()
    if voting_status != 'started':
        return render_template('access_denied.html', message="Voting is currently closed.")

    if request.method == 'POST':
        candidate = (request.form.get('candidate') or '').strip()
        active_candidates = get_active_candidates()
        active_names = [c[1] for c in active_candidates]

        if not candidate or candidate not in active_names:
            flash("Please select a valid active candidate.", "danger")
            return render_template('vote.html', voting_status=voting_status, candidates=active_candidates)

        if save_vote(user_id, candidate):
            return render_template('thankyou.html', candidate=candidate)
        else:
            if user_has_voted(user_id):
                return render_template('already_voted.html')
            return render_template('access_denied.html', message="Voting is currently closed or candidate is invalid.")

    candidates = get_active_candidates()
    return render_template('vote.html', voting_status=voting_status, candidates=candidates)

@app.route('/admin_dashboard')
@admin_required
def admin_dashboard():
    results = get_results()
    voting_status = get_voting_status()
    candidates = get_all_candidates()
    voters = get_all_voters()
    return render_template(
        'admin_dashboard.html',
        results=results,
        voting_status=voting_status,
        candidates=candidates,
        voters=voters
    )

@app.route('/admin/candidates/add', methods=['POST'])
@admin_required
def admin_add_candidate():
    name = request.form.get('name')
    description = request.form.get('description')
    success, msg = add_candidate(name, description)
    flash(msg, "success" if success else "danger")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/candidates/toggle/<int:candidate_id>', methods=['POST'])
@admin_required
def admin_toggle_candidate(candidate_id):
    success, msg = toggle_candidate_status(candidate_id)
    flash(msg, "success" if success else "danger")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/candidates/delete/<int:candidate_id>', methods=['POST'])
@admin_required
def admin_delete_candidate(candidate_id):
    # Enforce voting status rule independently of UI: only allow deletion when CLOSED ('ended')
    current_status = get_voting_status()
    if current_status != 'ended':
        if current_status == 'started':
            flash("Cannot delete candidates while voting is open. Please close voting first.", "danger")
        else:
            flash("Candidates can only be deleted when the election is closed.", "danger")
        return redirect(url_for('admin_dashboard'))

    success, msg = delete_candidate(candidate_id)
    flash(msg, "success" if success else "danger")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    success, msg = delete_user(user_id)
    flash(msg, "success" if success else "danger")
    return redirect(url_for('admin_dashboard'))

@app.route('/toggle_voting', methods=['POST'])
@admin_required
def toggle_voting():
    action = request.form.get('action')
    if action == 'open':
        set_voting_status(1)
        flash("Voting is now OPEN.", "success")
    elif action == 'close':
        set_voting_status(0)
        flash("Voting is now CLOSED.", "warning")
    return redirect(url_for('admin_dashboard'))

@app.route('/clear_votes', methods=['POST'])
@admin_required
def clear_votes():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM votes')
    conn.commit()
    conn.close()
    flash("All votes have been cleared successfully.", "info")
    return redirect(url_for('admin_dashboard'))

@app.route('/results')
@admin_required
def results():
    results_data = get_results()
    total_votes = get_total_votes()
    return render_template('results.html', results=results_data, total_votes=total_votes)

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out successfully.", "info")
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True)
