import sqlite3
import os
import collections
from werkzeug.security import generate_password_hash

DB_NAME = os.environ.get('DATABASE_NAME', 'voting_system.db')

def get_db_connection(db_path=None):
    if db_path is None:
        db_path = DB_NAME
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def get_voting_status(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT is_open FROM server_status WHERE id = 1')
    row = cursor.fetchone()
    conn.close()
    if row:
        return 'started' if row[0] == 1 else 'ended'
    return 'not_started'

def set_voting_status(status, db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('UPDATE server_status SET is_open = ? WHERE id = 1', (status,))
    conn.commit()
    conn.close()

def init_db(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    # Users table
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        email TEXT NOT NULL UNIQUE,
                        password TEXT NOT NULL)''')

    # Votes table (preserves existing votes and schema)
    cursor.execute('''CREATE TABLE IF NOT EXISTS votes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        candidate TEXT NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(id))''')

    # Enforce unique user voting at database level
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_votes_user_id ON votes (user_id)')

    # Server status table
    cursor.execute('''CREATE TABLE IF NOT EXISTS server_status (
                    id INTEGER PRIMARY KEY,
                    is_open INTEGER DEFAULT 0)''')
    cursor.execute('SELECT COUNT(*) FROM server_status')
    if cursor.fetchone()[0] == 0:
        cursor.execute('INSERT OR IGNORE INTO server_status (id, is_open) VALUES (1, 0)')

    # Candidates table for dynamic management
    cursor.execute('''CREATE TABLE IF NOT EXISTS candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        description TEXT,
                        is_active INTEGER DEFAULT 1)''')

    # Seed default candidates if table is empty (preserves original candidates)
    cursor.execute('SELECT COUNT(*) FROM candidates')
    if cursor.fetchone()[0] == 0:
        initial_candidates = [
            ('Candidate A', 'Candidate representing Candidate A'),
            ('Candidate B', 'Candidate representing Candidate B'),
            ('Candidate C', 'Candidate representing Candidate C')
        ]
        cursor.executemany(
            'INSERT INTO candidates (name, description, is_active) VALUES (?, ?, 1)',
            initial_candidates
        )

    conn.commit()
    conn.close()

def add_admin_if_not_exists(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE email=?', ('admin@admin.com',))
    if cursor.fetchone() is None:
        hashed_password = generate_password_hash("admin123")
        cursor.execute('INSERT INTO users (name, email, password) VALUES (?, ?, ?)',
                       ("Administrator", 'admin@admin.com', hashed_password))
        conn.commit()
    conn.close()

def get_active_candidates(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, description FROM candidates WHERE is_active = 1 ORDER BY id')
    candidates = cursor.fetchall()
    conn.close()
    return candidates

def get_all_candidates(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, description, is_active FROM candidates ORDER BY id')
    candidates = cursor.fetchall()
    conn.close()
    return candidates

def normalize_candidate_name(name):
    if not name:
        return ""
    clean = str(name).strip()
    if clean.upper() == 'A' or clean.lower() == 'candidate a':
        return 'Candidate A'
    if clean.upper() == 'B' or clean.lower() == 'candidate b':
        return 'Candidate B'
    if clean.upper() == 'C' or clean.lower() == 'candidate c':
        return 'Candidate C'
    return clean

def get_candidate_aliases(name):
    norm = normalize_candidate_name(name)
    aliases = {str(name).strip(), norm}
    if norm == 'Candidate A':
        aliases.add('A')
    elif norm == 'Candidate B':
        aliases.add('B')
    elif norm == 'Candidate C':
        aliases.add('C')
    return list(aliases)

def candidate_has_votes(candidate_name, db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    aliases = get_candidate_aliases(candidate_name)
    placeholders = ', '.join(['?'] * len(aliases))
    cursor.execute(
        f'SELECT COUNT(*) FROM votes WHERE candidate IN ({placeholders})',
        aliases
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0

def add_candidate(name, description, db_path=None):
    clean_name = (name or '').strip()
    clean_desc = (description or '').strip()
    if not clean_name:
        return False, "Candidate name cannot be empty."

    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM candidates WHERE LOWER(name) = LOWER(?)', (clean_name,))
    if cursor.fetchone():
        conn.close()
        return False, f"A candidate with the name '{clean_name}' already exists."

    cursor.execute('INSERT INTO candidates (name, description, is_active) VALUES (?, ?, 1)',
                   (clean_name, clean_desc))
    conn.commit()
    conn.close()
    return True, "Candidate added successfully."

def toggle_candidate_status(candidate_id, db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, is_active FROM candidates WHERE id = ?', (candidate_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Candidate not found."

    new_status = 0 if row[1] == 1 else 1
    cursor.execute('UPDATE candidates SET is_active = ? WHERE id = ?', (new_status, candidate_id))
    conn.commit()
    conn.close()
    status_text = "activated" if new_status == 1 else "deactivated"
    return True, f"Candidate {status_text} successfully."

def delete_candidate(candidate_id, db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM candidates WHERE id = ?', (candidate_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Candidate not found."

    cand_id, candidate_name = row

    # 1. Enforce voting status rule: deletion is ONLY permitted when the election is CLOSED ('ended')
    status = get_voting_status(db_path)
    if status != 'ended':
        conn.close()
        if status == 'started':
            return False, "Cannot delete candidates while voting is open. Please close voting first."
        else:
            return False, "Candidates can only be deleted when the election is closed."

    # 2. Enforce historical vote integrity: check both full candidate name and legacy aliases
    if candidate_has_votes(candidate_name, db_path):
        conn.close()
        return False, f"Cannot delete candidate '{candidate_name}' because recorded votes exist. You may deactivate the candidate instead to preserve historical records."

    # 3. Candidate has zero votes and election is closed: delete permanently
    cursor.execute('DELETE FROM candidates WHERE id = ?', (cand_id,))
    conn.commit()
    conn.close()
    return True, f"Candidate '{candidate_name}' was permanently deleted."

def get_total_votes(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM votes')
    total = cursor.fetchone()[0]
    conn.close()
    return total

def get_results(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    # 1. Fetch all candidate names from candidates table
    cursor.execute('SELECT name FROM candidates ORDER BY id')
    candidate_rows = cursor.fetchall()

    # 2. Fetch all recorded votes
    cursor.execute('SELECT candidate FROM votes')
    vote_rows = cursor.fetchall()
    conn.close()

    # 3. Count votes by normalized candidate name
    vote_counts = collections.Counter(normalize_candidate_name(r[0]) for r in vote_rows)

    # 4. Build results dict ensuring all registered candidates appear (even with 0 votes)
    results_dict = {}
    for r in candidate_rows:
        norm = normalize_candidate_name(r[0])
        results_dict[norm] = vote_counts.get(norm, 0)

    # 5. Include any historical candidates that exist in votes but might not be in candidates table
    for norm_name, count in vote_counts.items():
        if norm_name not in results_dict:
            results_dict[norm_name] = count

    # 6. Order by votes descending, then candidate name ascending
    sorted_results = sorted(results_dict.items(), key=lambda x: (-x[1], x[0]))
    return sorted_results

def get_all_voters(db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.id, u.name, u.email,
               CASE WHEN EXISTS(SELECT 1 FROM votes v WHERE v.user_id = u.id) THEN 1 ELSE 0 END AS has_voted
        FROM users u
        WHERE LOWER(u.email) != 'admin@admin.com'
        ORDER BY u.id
    ''')
    voters = cursor.fetchall()
    conn.close()
    return voters

def delete_user(user_id, db_path=None):
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, email FROM users WHERE id = ?', (user_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "User not found."

    uid, user_name, user_email = row

    # 1. Protect administrator account from deletion
    if (user_email or '').strip().lower() == 'admin@admin.com':
        conn.close()
        return False, "The administrator account cannot be deleted."

    # 2. Enforce historical vote integrity: if the voter has cast a ballot, deletion is blocked
    cursor.execute('SELECT id FROM votes WHERE user_id = ?', (uid,))
    if cursor.fetchone() is not None:
        conn.close()
        return False, f"Cannot delete voter '{user_name}' because a ballot has already been cast. Historical voting records cannot be deleted."

    # 3. Voter has not voted: delete permanently
    cursor.execute('DELETE FROM users WHERE id = ?', (uid,))
    conn.commit()
    conn.close()
    return True, f"Voter '{user_name}' was permanently deleted."

if __name__ == '__main__':
    init_db()
    add_admin_if_not_exists()
    print("Database initialized successfully.")
