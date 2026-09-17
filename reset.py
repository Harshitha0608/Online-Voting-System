import os
import sys
import database

def get_target_db_path(args=None):
    """Determine the target database path from CLI args or environment."""
    if args:
        # Filter out flags like --yes or --confirm
        non_flag_args = [a for a in args if not a.startswith('--')]
        if non_flag_args:
            return non_flag_args[0]
    return os.environ.get('DATABASE_NAME', 'voting_system.db')

def reset_database(db_path=None):
    """
    Safely reset the database to a clean baseline:
    - Drops all application tables (votes, candidates, users, server_status)
    - Re-creates full schema via canonical database.init_db()
    - Seeds the default administrator (admin@admin.com / admin123)
    - Seeds the default candidates (Candidate A, B, C active)
    - Sets election status to CLOSED
    - Resets all autoincrement sequences
    """
    if db_path is None:
        db_path = get_target_db_path()

    conn = database.get_db_connection(db_path)
    cursor = conn.cursor()
    # Disable foreign keys temporarily during drop to prevent constraint conflicts
    cursor.execute('PRAGMA foreign_keys = OFF')
    cursor.execute('DROP TABLE IF EXISTS votes')
    cursor.execute('DROP TABLE IF EXISTS candidates')
    cursor.execute('DROP TABLE IF EXISTS users')
    cursor.execute('DROP TABLE IF EXISTS server_status')
    try:
        cursor.execute('DELETE FROM sqlite_sequence')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

    # Recreate canonical schema and seed default data
    database.init_db(db_path)
    database.add_admin_if_not_exists(db_path)
    database.set_voting_status(0, db_path)

    return True

def main():
    db_path = get_target_db_path(sys.argv[1:])
    auto_confirm = '--yes' in sys.argv or '--confirm' in sys.argv

    if not auto_confirm:
        print("=" * 60)
        print("          ONLINE VOTING SYSTEM - DATABASE RESET")
        print("=" * 60)
        print(f"Target Database: {os.path.abspath(db_path)}")
        print("\nWARNING: This destructive operation will permanently erase:")
        print("  - All voter accounts (except the seeded Administrator)")
        print("  - All recorded votes")
        print("  - All custom candidates (re-seeding Candidate A, B, C)")
        print("  - Election status will be set to CLOSED")
        print("\nAre you sure you want to proceed?")
        confirmation = input("Type YES to confirm: ").strip()

        if confirmation != 'YES':
            print("\nReset cancelled. No changes were made.")
            sys.exit(0)

    print(f"\nResetting database: {db_path}...")
    reset_database(db_path)
    print("Database reset successfully.")
    print("  - Administrator: admin@admin.com (password: admin123)")
    print("  - Candidates: Candidate A, Candidate B, Candidate C (active)")
    print("  - Votes: 0")
    print("  - Election Status: CLOSED")

if __name__ == '__main__':
    main()