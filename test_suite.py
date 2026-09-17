import os
import sys
import sqlite3
import unittest

WORKSPACE_PATH = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_PATH not in sys.path:
    sys.path.insert(0, WORKSPACE_PATH)

TEST_DB = os.path.join(WORKSPACE_PATH, 'test_voting_system.db')
os.environ['DATABASE_NAME'] = TEST_DB

import database
import app as flask_app

class TestOnlineVotingSystem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass
        database.init_db(TEST_DB)
        database.add_admin_if_not_exists(TEST_DB)
        flask_app.app.config['TESTING'] = True
        flask_app.app.config['SECRET_KEY'] = 'test-secret-key'

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except Exception:
                pass

    def setUp(self):
        # Reset DB tables cleanly between tests
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('DELETE FROM votes')
        c.execute('DELETE FROM users WHERE email != "admin@admin.com"')
        c.execute('DELETE FROM candidates')
        # Re-seed initial candidates
        initial = [
            (1, 'Candidate A', 'Candidate representing Candidate A', 1),
            (2, 'Candidate B', 'Candidate representing Candidate B', 1),
            (3, 'Candidate C', 'Candidate representing Candidate C', 1)
        ]
        c.executemany('INSERT INTO candidates (id, name, description, is_active) VALUES (?, ?, ?, ?)', initial)
        c.execute('UPDATE server_status SET is_open = 0 WHERE id = 1')
        conn.commit()
        conn.close()
        self.client = flask_app.app.test_client()

    def login_as_admin(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['user_name'] = 'Administrator'
            sess['email'] = 'admin@admin.com'
            sess['admin_logged_in'] = True

    def login_as_voter(self, user_id=2, email='voter@mail.com', name='Test Voter'):
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('SELECT id FROM users WHERE id = ?', (user_id,))
        if not c.fetchone():
            c.execute('INSERT OR REPLACE INTO users (id, name, email, password) VALUES (?, ?, ?, "hashed_pw")',
                      (user_id, name, email))
            conn.commit()
        conn.close()
        with self.client.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['user_name'] = name
            sess['email'] = email
            sess['admin_logged_in'] = False

    # -------------------------------------------------------------
    # 1. Existing functionality & regression
    # -------------------------------------------------------------
    def test_01_candidates_seeded(self):
        """Verify default Candidate A, B, C are seeded."""
        candidates = database.get_all_candidates(TEST_DB)
        names = [c[1] for c in candidates]
        self.assertIn('Candidate A', names)
        self.assertIn('Candidate B', names)
        self.assertIn('Candidate C', names)

    def test_02_add_candidate_validation(self):
        """Verify adding new candidates, uniqueness, and empty check."""
        success, msg = database.add_candidate('Candidate D', 'Tech leader', TEST_DB)
        self.assertTrue(success)

        dup_success, dup_msg = database.add_candidate('Candidate D', 'Duplicate', TEST_DB)
        self.assertFalse(dup_success)
        self.assertIn('already exists', dup_msg)

        empty_success, empty_msg = database.add_candidate('   ', '', TEST_DB)
        self.assertFalse(empty_success)

    def test_03_toggle_candidate_status(self):
        """Verify active/inactive candidate status toggling."""
        candidates = database.get_all_candidates(TEST_DB)
        cid = candidates[0][0]

        # Deactivate
        success, msg = database.toggle_candidate_status(cid, TEST_DB)
        self.assertTrue(success)
        active = database.get_active_candidates(TEST_DB)
        active_ids = [c[0] for c in active]
        self.assertNotIn(cid, active_ids)

        # Reactivate
        success, msg = database.toggle_candidate_status(cid, TEST_DB)
        self.assertTrue(success)
        active = database.get_active_candidates(TEST_DB)
        active_ids = [c[0] for c in active]
        self.assertIn(cid, active_ids)

    def test_04_duplicate_vote_prevention(self):
        """Verify voter cannot vote more than once via app check and DB index."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (name, email, password) VALUES ("Test Voter", "voter1@mail.com", "hash")')
        user_id = c.lastrowid
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (user_id, 'Candidate A'))
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (user_id, 'Candidate B'))
            conn.commit()
        conn.close()

    # -------------------------------------------------------------
    # 2. Deletion business rules: OPEN vs CLOSED
    # -------------------------------------------------------------
    def test_05_delete_blocked_when_voting_open(self):
        """Candidate deletion is strictly blocked when voting is OPEN ('started')."""
        # Set voting to OPEN
        database.set_voting_status(1, TEST_DB)
        self.assertEqual(database.get_voting_status(TEST_DB), 'started')

        # Add a zero-vote candidate
        database.add_candidate('Candidate Zero', 'No votes', TEST_DB)
        candidates = database.get_all_candidates(TEST_DB)
        zero_cand = [c for c in candidates if c[1] == 'Candidate Zero'][0]

        # 1. Direct database helper level check
        success, msg = database.delete_candidate(zero_cand[0], TEST_DB)
        self.assertFalse(success)
        self.assertIn('open', msg.lower())

        # 2. Web endpoint / HTTP level check
        self.login_as_admin()
        resp = self.client.post(f'/admin/candidates/delete/{zero_cand[0]}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Cannot delete candidates while voting is open', resp.data)

        # Candidate must still exist in DB
        cands_after = [c[1] for c in database.get_all_candidates(TEST_DB)]
        self.assertIn('Candidate Zero', cands_after)

    def test_06_delete_success_when_voting_closed_zero_votes(self):
        """Candidate with zero votes can be permanently deleted when voting is CLOSED ('ended')."""
        # Set voting to CLOSED
        database.set_voting_status(0, TEST_DB)
        self.assertEqual(database.get_voting_status(TEST_DB), 'ended')

        # Add a zero-vote candidate
        database.add_candidate('Candidate Removable', 'Temporary', TEST_DB)
        candidates = database.get_all_candidates(TEST_DB)
        rem_cand = [c for c in candidates if c[1] == 'Candidate Removable'][0]

        # Delete via web endpoint as admin
        self.login_as_admin()
        resp = self.client.post(f'/admin/candidates/delete/{rem_cand[0]}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'permanently deleted', resp.data)

        # Candidate must no longer exist in candidates list
        cands_after = [c[1] for c in database.get_all_candidates(TEST_DB)]
        self.assertNotIn('Candidate Removable', cands_after)

        # Verify candidate does not appear in active candidates list
        active_cands = [c[1] for c in database.get_active_candidates(TEST_DB)]
        self.assertNotIn('Candidate Removable', active_cands)

    # -------------------------------------------------------------
    # 3. Historical vote protection (both full name & legacy single letters)
    # -------------------------------------------------------------
    def test_07_delete_blocked_for_candidate_with_full_name_votes(self):
        """Candidate with full-name recorded votes cannot be deleted even when election is CLOSED."""
        database.set_voting_status(0, TEST_DB)
        self.assertEqual(database.get_voting_status(TEST_DB), 'ended')

        # Insert a vote for Candidate A
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (name, email, password) VALUES ("V1", "v1@mail.com", "h")')
        u1 = c.lastrowid
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (u1, 'Candidate A'))
        conn.commit()
        conn.close()

        cand_a = [c for c in database.get_all_candidates(TEST_DB) if c[1] == 'Candidate A'][0]

        # Attempt delete via helper
        success, msg = database.delete_candidate(cand_a[0], TEST_DB)
        self.assertFalse(success)
        self.assertIn('recorded votes exist', msg)

        # Attempt delete via HTTP endpoint
        self.login_as_admin()
        resp = self.client.post(f'/admin/candidates/delete/{cand_a[0]}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'recorded votes exist', resp.data)

        # Verify historical vote was NOT destroyed
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM votes WHERE candidate = "Candidate A"')
        self.assertEqual(c.fetchone()[0], 1)
        conn.close()

    def test_08_delete_blocked_for_candidate_with_legacy_votes(self):
        """Candidate with legacy single-letter vote ('B') is recognized and protected from deletion."""
        database.set_voting_status(0, TEST_DB)
        self.assertEqual(database.get_voting_status(TEST_DB), 'ended')

        # Insert legacy 'B' vote
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (name, email, password) VALUES ("V2", "v2@mail.com", "h")')
        u2 = c.lastrowid
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (u2, 'B'))
        conn.commit()
        conn.close()

        cand_b = [c for c in database.get_all_candidates(TEST_DB) if c[1] == 'Candidate B'][0]

        # Verify candidate_has_votes recognizes legacy 'B' for 'Candidate B'
        self.assertTrue(database.candidate_has_votes('Candidate B', TEST_DB))
        self.assertTrue(database.candidate_has_votes('B', TEST_DB))

        # Attempt delete
        success, msg = database.delete_candidate(cand_b[0], TEST_DB)
        self.assertFalse(success)
        self.assertIn('recorded votes exist', msg)

        # Historical legacy vote preserved
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM votes WHERE candidate = "B"')
        self.assertEqual(c.fetchone()[0], 1)
        conn.close()

    # -------------------------------------------------------------
    # 4. Results page: total votes & normalization
    # -------------------------------------------------------------
    def test_09_results_normalization_and_total_votes(self):
        """Results displays normalized candidates, 0-vote candidates, and accurate Total Votes."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (name, email, password) VALUES ("U1", "u1@mail.com", "h")')
        u1 = c.lastrowid
        c.execute('INSERT INTO users (name, email, password) VALUES ("U2", "u2@mail.com", "h")')
        u2 = c.lastrowid
        c.execute('INSERT INTO users (name, email, password) VALUES ("U3", "u3@mail.com", "h")')
        u3 = c.lastrowid

        # Insert 1 legacy 'A', 1 full 'Candidate A', and 1 'Candidate C'
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (u1, 'A'))
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (u2, 'Candidate A'))
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, ?)', (u3, 'Candidate C'))
        conn.commit()
        conn.close()

        # Total votes in DB must be 3
        total_votes = database.get_total_votes(TEST_DB)
        self.assertEqual(total_votes, 3)

        results = database.get_results(TEST_DB)
        res_dict = dict(results)

        # Candidate A has 2 votes (legacy 'A' + 'Candidate A' normalized into one row)
        self.assertEqual(res_dict.get('Candidate A'), 2)
        # Candidate C has 1 vote
        self.assertEqual(res_dict.get('Candidate C'), 1)
        # Candidate B has 0 votes, but MUST be present in results
        self.assertIn('Candidate B', res_dict)
        self.assertEqual(res_dict.get('Candidate B'), 0)

        # Sum of candidate votes MUST equal total votes
        displayed_sum = sum(res_dict.values())
        self.assertEqual(displayed_sum, total_votes)

        # Verify through Flask /results page
        self.login_as_admin()
        resp = self.client.get('/results')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Total Votes', resp.data)
        self.assertIn(b'3', resp.data)
        self.assertIn(b'Candidate A', resp.data)
        self.assertIn(b'Candidate B', resp.data)
        self.assertIn(b'Candidate C', resp.data)

    def test_10_non_admin_cannot_delete_candidate(self):
        """Non-admin / voter cannot delete candidates."""
        database.set_voting_status(0, TEST_DB)
        self.login_as_voter()
        resp = self.client.post('/admin/candidates/delete/1')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Admin privileges required', resp.data)

    # -------------------------------------------------------------
    # 5. Security & Voting-Flow Audit Tests
    # -------------------------------------------------------------
    def test_11_closed_election_blocks_vote_submission(self):
        """When voting is closed, direct POST or GET to /vote is blocked and records no vote."""
        database.set_voting_status(0, TEST_DB)
        self.assertEqual(database.get_voting_status(TEST_DB), 'ended')
        self.login_as_voter()

        # GET /vote
        resp = self.client.get('/vote')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Voting is currently closed', resp.data)

        # POST /vote
        resp = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Voting is currently closed', resp.data)

        # Confirm no vote recorded
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_12_unauthenticated_user_cannot_vote(self):
        """Logged-out user cannot access /vote via GET or POST."""
        database.set_voting_status(1, TEST_DB)

        # GET /vote
        resp = self.client.get('/vote', follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

        # POST /vote
        resp = self.client.post('/vote', data={'candidate': 'Candidate A'}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

        # Confirm no vote recorded
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_13_normal_voter_can_vote_while_open(self):
        """Normal authenticated voter can successfully cast ballot when election is OPEN."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        resp = self.client.post('/vote', data={'candidate': 'Candidate B'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Vote Recorded', resp.data)
        self.assertIn(b'Candidate B', resp.data)

        # Total votes is now 1
        self.assertEqual(database.get_total_votes(TEST_DB), 1)

    def test_14_duplicate_vote_blocked_via_post_and_repeat(self):
        """Repeated submission by the same voter is blocked and does not create duplicate votes."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # First vote
        resp1 = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(resp1.status_code, 200)
        self.assertIn(b'Vote Recorded', resp1.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 1)

        # Second vote attempt (e.g. page refresh or direct POST)
        resp2 = self.client.post('/vote', data={'candidate': 'Candidate B'})
        self.assertEqual(resp2.status_code, 200)
        self.assertIn(b'Already Voted', resp2.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 1)

    def test_15_invalid_and_empty_candidate_blocked(self):
        """Manipulated, non-existent, or empty candidate submissions are rejected."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # Empty candidate
        resp = self.client.post('/vote', data={'candidate': '   '})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Please select a valid active candidate', resp.data)

        # Non-existent candidate
        resp = self.client.post('/vote', data={'candidate': 'Hacker Candidate'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Please select a valid active candidate', resp.data)

        # Candidate ID instead of name
        resp = self.client.post('/vote', data={'candidate': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Please select a valid active candidate', resp.data)

        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_16_inactive_candidate_blocked(self):
        """Submitting a vote for an inactive candidate is rejected."""
        database.set_voting_status(1, TEST_DB)
        # Deactivate Candidate A (id=1)
        database.toggle_candidate_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        resp = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Please select a valid active candidate', resp.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_17_candidate_becoming_inactive_race_condition(self):
        """If candidate is active at GET time but deactivated before POST, vote is rejected."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # 1. Voter loads form when Candidate C is active
        get_resp = self.client.get('/vote')
        self.assertEqual(get_resp.status_code, 200)
        self.assertIn(b'Candidate C', get_resp.data)

        # 2. Admin deactivates Candidate C (id=3)
        database.toggle_candidate_status(3, TEST_DB)

        # 3. Voter posts vote for Candidate C
        post_resp = self.client.post('/vote', data={'candidate': 'Candidate C'})
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn(b'Please select a valid active candidate', post_resp.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_18_voting_closes_after_page_load_race_condition(self):
        """If election is open at GET time but closed before POST, vote is rejected."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # 1. Voter loads form while open
        get_resp = self.client.get('/vote')
        self.assertEqual(get_resp.status_code, 200)
        self.assertIn(b'Voting is open', get_resp.data)

        # 2. Admin closes voting
        database.set_voting_status(0, TEST_DB)

        # 3. Voter submits form
        post_resp = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn(b'Voting is currently closed', post_resp.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_19_admin_blocked_from_voting(self):
        """Admin accounts are strictly blocked from voting via GET and direct POST."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_admin()

        get_resp = self.client.get('/vote')
        self.assertEqual(get_resp.status_code, 200)
        self.assertIn(b'Administrators are not permitted to vote', get_resp.data)

        post_resp = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(post_resp.status_code, 200)
        self.assertIn(b'Administrators are not permitted to vote', post_resp.data)

        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_20_non_admin_blocked_from_all_admin_routes(self):
        """Voters and unauthenticated users are blocked from all admin endpoints."""
        database.set_voting_status(1, TEST_DB)
        admin_endpoints = [
            ('/admin_dashboard', 'GET', None),
            ('/toggle_voting', 'POST', {'action': 'close'}),
            ('/admin/candidates/add', 'POST', {'name': 'Illegal Candidate'}),
            ('/admin/candidates/toggle/1', 'POST', None),
            ('/admin/candidates/delete/1', 'POST', None),
            ('/clear_votes', 'POST', None),
            ('/results', 'GET', None),
        ]

        # 1. Test as normal voter
        self.login_as_voter()
        for endpoint, method, data in admin_endpoints:
            if method == 'GET':
                resp = self.client.get(endpoint)
            else:
                resp = self.client.post(endpoint, data=data)
            self.assertEqual(resp.status_code, 200, f"Failed for {endpoint}")
            self.assertIn(b'Admin privileges required', resp.data, f"Endpoint {endpoint} was not blocked for voter")

        # 2. Test as unauthenticated user
        with self.client.session_transaction() as sess:
            sess.clear()
        for endpoint, method, data in admin_endpoints:
            if method == 'GET':
                resp = self.client.get(endpoint)
            else:
                resp = self.client.post(endpoint, data=data)
            self.assertEqual(resp.status_code, 200, f"Failed for {endpoint}")
            self.assertIn(b'Admin privileges required', resp.data, f"Endpoint {endpoint} was not blocked for logged out")

    def test_21_results_route_readonly(self):
        """Results route cannot be modified via POST (read-only)."""
        self.login_as_admin()
        resp = self.client.post('/results')
        self.assertEqual(resp.status_code, 405)  # Method Not Allowed

    # -------------------------------------------------------------
    # 6. Dashboard UX Wording Tests
    # -------------------------------------------------------------
    def test_22_dashboard_ux_unvoted_voter(self):
        """Dashboard displays 'Not Yet Voted' and 'You may cast your vote.' for unvoted voter."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        resp = self.client.get('/dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Not Yet Voted', resp.data)
        self.assertNotIn(b'Ballot Pending', resp.data)
        self.assertIn(b'The polls are open. You may cast your vote.', resp.data)
        self.assertIn(b'Cast My Vote', resp.data)

    def test_23_dashboard_ux_already_voted_voter(self):
        """Dashboard displays 'Ballot Submitted', 'Vote Recorded', and accurate polls open text for voted voter."""
        database.set_voting_status(1, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # Vote first
        self.client.post('/vote', data={'candidate': 'Candidate A'})

        # Now view dashboard
        resp = self.client.get('/dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Ballot Submitted', resp.data)
        self.assertIn(b'Vote Recorded', resp.data)
        self.assertIn(b'The polls are open, but you have already submitted your ballot.', resp.data)
    # -------------------------------------------------------------
    # 7. End-to-End User-Flow Audit Tests
    # -------------------------------------------------------------
    def test_24_end_to_end_voter_journey(self):
        """Complete voter journey: register -> login -> dashboard -> vote -> thankyou -> return -> block dup vote."""
        database.set_voting_status(1, TEST_DB)

        # 1. Register fresh voter
        reg_resp = self.client.post('/register', data={
            'name': 'Audit User',
            'email': 'audit_user@example.com',
            'password': 'auditPassword123'
        }, follow_redirects=True)
        self.assertEqual(reg_resp.status_code, 200)
        self.assertIn(b"Registration successful! You can now log in.", reg_resp.data)

        # 2. Login
        login_resp = self.client.post('/login', data={
            'email': 'audit_user@example.com',
            'password': 'auditPassword123'
        }, follow_redirects=True)
        self.assertEqual(login_resp.status_code, 200)
        self.assertIn(b"Welcome back, Audit User!", login_resp.data)
        self.assertIn(b"Welcome, Audit User", login_resp.data)
        self.assertIn(b"Not Yet Voted", login_resp.data)
        self.assertIn(b"Cast My Vote", login_resp.data)

        # 3. Navigate to vote page
        vote_page = self.client.get('/vote')
        self.assertEqual(vote_page.status_code, 200)
        self.assertIn(b"Candidate A", vote_page.data)
        self.assertIn(b"Candidate B", vote_page.data)
        self.assertIn(b"Candidate C", vote_page.data)

        # 4. Submit ballot
        vote_resp = self.client.post('/vote', data={'candidate': 'Candidate B'}, follow_redirects=True)
        self.assertEqual(vote_resp.status_code, 200)
        self.assertIn(b"Vote Recorded", vote_resp.data)
        self.assertIn(b"Candidate B", vote_resp.data)
        self.assertIn(b"Your ballot has been submitted successfully", vote_resp.data)

        # 5. Verify vote in DB
        self.assertEqual(database.get_total_votes(TEST_DB), 1)

        # 6. Return to dashboard
        dash_resp = self.client.get('/dashboard')
        self.assertEqual(dash_resp.status_code, 200)
        self.assertIn(b"Ballot Submitted", dash_resp.data)
        self.assertIn(b"Vote Recorded", dash_resp.data)
        self.assertIn(b"The polls are open, but you have already submitted your ballot.", dash_resp.data)
        self.assertNotIn(b"Cast My Vote", dash_resp.data)

        # 7. Re-submission attempt blocked
        dup_resp = self.client.post('/vote', data={'candidate': 'Candidate A'}, follow_redirects=True)
        self.assertEqual(dup_resp.status_code, 200)
        self.assertIn(b"You Have Already Voted", dup_resp.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 1)

    def test_25_closed_election_voter_experience(self):
        """When closed, unvoted voter sees polls closed without 'Already Voted' error, and vote submission is blocked."""
        database.set_voting_status(0, TEST_DB)
        self.login_as_voter(user_id=2, email='voter2@mail.com', name='Voter Two')

        # Dashboard view
        dash_resp = self.client.get('/dashboard')
        self.assertEqual(dash_resp.status_code, 200)
        self.assertIn(b"Closed", dash_resp.data)
        self.assertIn(b"The election session has ended.", dash_resp.data)
        self.assertIn(b"Not Yet Voted", dash_resp.data)
        self.assertIn(b"Voting is not currently available", dash_resp.data)
        self.assertNotIn(b"Already Voted", dash_resp.data)

        # GET /vote access
        vote_get = self.client.get('/vote')
        self.assertEqual(vote_get.status_code, 200)
        self.assertIn(b"Voting is currently closed.", vote_get.data)

        # POST /vote access
        vote_post = self.client.post('/vote', data={'candidate': 'Candidate A'})
        self.assertEqual(vote_post.status_code, 200)
        self.assertIn(b"Voting is currently closed.", vote_post.data)
        self.assertEqual(database.get_total_votes(TEST_DB), 0)

    def test_26_end_to_end_admin_workflow(self):
        """Admin journey: login -> dashboard -> toggle status -> add candidate -> toggle candidate -> delete rules."""
        self.login_as_admin()

        # Admin dashboard
        dash = self.client.get('/admin_dashboard')
        self.assertEqual(dash.status_code, 200)
        self.assertIn(b"Admin Dashboard", dash.data)

        # Open voting
        self.client.post('/toggle_voting', data={'action': 'open'}, follow_redirects=True)
        self.assertEqual(database.get_voting_status(TEST_DB), 'started')

        # Add candidate
        add_resp = self.client.post('/admin/candidates/add', data={'name': 'Candidate Audit', 'description': 'Auditor'}, follow_redirects=True)
        self.assertIn(b"Candidate added successfully.", add_resp.data)
        all_cands = database.get_all_candidates(TEST_DB)
        cand_audit = [c for c in all_cands if c[1] == 'Candidate Audit'][0]
        audit_id = cand_audit[0]

        # Deactivate / activate
        self.client.post(f'/admin/candidates/toggle/{audit_id}', follow_redirects=True)
        self.assertEqual(database.get_all_candidates(TEST_DB)[3][3], 0)
        self.client.post(f'/admin/candidates/toggle/{audit_id}', follow_redirects=True)
        self.assertEqual(database.get_all_candidates(TEST_DB)[3][3], 1)

        # Deletion while OPEN blocked
        del_open = self.client.post(f'/admin/candidates/delete/{audit_id}', follow_redirects=True)
        self.assertIn(b"Cannot delete candidates while voting is open", del_open.data)

        # Close voting
        self.client.post('/toggle_voting', data={'action': 'close'}, follow_redirects=True)
        self.assertEqual(database.get_voting_status(TEST_DB), 'ended')

        # Add mock user and vote for Candidate A
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (99, "mock_voter", "mock@m.com", "hash")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (99, "Candidate A")')
        conn.commit()
        conn.close()

        # Deletion of candidate with votes blocked
        del_voted = self.client.post('/admin/candidates/delete/1', follow_redirects=True)
        self.assertIn(b"because recorded votes exist", del_voted.data)

        # Deletion of zero-vote candidate while CLOSED succeeds
        del_zero = self.client.post(f'/admin/candidates/delete/{audit_id}', follow_redirects=True)
        self.assertIn(b"was permanently deleted", del_zero.data)

    def test_27_results_consistency_and_zero_votes(self):
        """Results page displays correct total votes, candidate counts, zero-vote candidates, and sums equal total."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (10, "u1", "u1@m.com", "p")')
        c.execute('INSERT INTO users (id, name, email, password) VALUES (11, "u2", "u2@m.com", "p")')
        c.execute('INSERT INTO users (id, name, email, password) VALUES (12, "u3", "u3@m.com", "p")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (10, "A")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (11, "Candidate A")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (12, "Candidate B")')
        conn.commit()
        conn.close()

        self.login_as_admin()
        results_resp = self.client.get('/results')
        self.assertEqual(results_resp.status_code, 200)

        total_votes = database.get_total_votes(TEST_DB)
        self.assertEqual(total_votes, 3)

        results_data = database.get_results(TEST_DB)
        results_dict = dict(results_data)
        self.assertEqual(results_dict['Candidate A'], 2)
        self.assertEqual(results_dict['Candidate B'], 1)
        self.assertEqual(results_dict['Candidate C'], 0)
        self.assertEqual(sum(results_dict.values()), total_votes)

    def test_28_access_control_and_session_clearing(self):
        """Unauthorized access to protected voter and admin pages is rejected; logout clears session completely."""
        # 1. Logged out accessing voter dashboard -> redirects to login
        resp = self.client.get('/dashboard')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

        # 2. Logged out accessing admin dashboard -> access denied
        resp = self.client.get('/admin_dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Admin privileges required", resp.data)

        # 3. Voter logged in accessing admin dashboard -> access denied
        self.login_as_voter(user_id=2, email='voter2@mail.com')
        resp = self.client.get('/admin_dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Admin privileges required", resp.data)

        # 4. Logout clears session
        self.client.get('/logout')
        dash_after_logout = self.client.get('/dashboard')
        self.assertEqual(dash_after_logout.status_code, 302)
        self.assertIn('/login', dash_after_logout.headers['Location'])

    # -------------------------------------------------------------
    # 8. Voter / User Management Tests
    # -------------------------------------------------------------
    def test_29_admin_delete_unvoted_voter(self):
        """Admin can permanently delete a voter account that has not cast a ballot."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (20, "Deletable Voter", "del@mail.com", "hash")')
        conn.commit()
        conn.close()

        self.login_as_admin()
        resp = self.client.post('/admin/users/delete/20', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"was permanently deleted", resp.data)

        # Verify user is deleted from DB
        conn = database.get_db_connection(TEST_DB)
        row = conn.cursor().execute('SELECT id FROM users WHERE id = 20').fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_30_admin_cannot_delete_administrator_account(self):
        """Admin account is strictly protected from deletion."""
        conn = database.get_db_connection(TEST_DB)
        admin_id = conn.cursor().execute('SELECT id FROM users WHERE email = "admin@admin.com"').fetchone()[0]
        conn.close()

        self.login_as_admin()
        resp = self.client.post(f'/admin/users/delete/{admin_id}', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"The administrator account cannot be deleted.", resp.data)

        # Verify admin still exists
        conn = database.get_db_connection(TEST_DB)
        admin_row = conn.cursor().execute('SELECT id FROM users WHERE email = "admin@admin.com"').fetchone()
        conn.close()
        self.assertIsNotNone(admin_row)

    def test_31_admin_cannot_delete_voter_with_recorded_votes(self):
        """Admin cannot delete a voter who has cast a ballot; voter and vote are preserved."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (21, "Voted User", "voted@mail.com", "hash")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (21, "Candidate A")')
        conn.commit()
        conn.close()

        self.login_as_admin()
        resp = self.client.post('/admin/users/delete/21', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"because a ballot has already been cast", resp.data)

        # Verify user still exists in users
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        user_row = c.execute('SELECT id FROM users WHERE id = 21').fetchone()
        self.assertIsNotNone(user_row)

        # Verify vote still exists in votes
        vote_row = c.execute('SELECT id FROM votes WHERE user_id = 21').fetchone()
        self.assertIsNotNone(vote_row)
        conn.close()

    def test_32_unauthorized_users_blocked_from_delete_user_endpoint(self):
        """Unauthenticated visitors and normal voters cannot delete user accounts."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (22, "Target Voter", "target@mail.com", "hash")')
        conn.commit()
        conn.close()

        # 1. Unauthenticated visitor
        with self.client.session_transaction() as sess:
            sess.clear()
        unauth_resp = self.client.post('/admin/users/delete/22')
        self.assertEqual(unauth_resp.status_code, 200)
        self.assertIn(b"Admin privileges required", unauth_resp.data)

        # 2. Normal voter
        self.login_as_voter(user_id=2, email='voter2@mail.com')
        voter_resp = self.client.post('/admin/users/delete/22')
        self.assertEqual(voter_resp.status_code, 200)
        self.assertIn(b"Admin privileges required", voter_resp.data)

        # Target user still exists
        conn = database.get_db_connection(TEST_DB)
        target_row = conn.cursor().execute('SELECT id FROM users WHERE id = 22').fetchone()
        conn.close()
        self.assertIsNotNone(target_row)

    def test_33_delete_nonexistent_user_fails_gracefully(self):
        """Attempting to delete a nonexistent user ID fails gracefully without 500 error."""
        self.login_as_admin()
        resp = self.client.post('/admin/users/delete/99999', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"User not found.", resp.data)

    def test_34_get_all_voters_helper(self):
        """database.get_all_voters returns accurate has_voted status and excludes administrator."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (30, "Voter Unvoted", "u@m.com", "h")')
        c.execute('INSERT INTO users (id, name, email, password) VALUES (31, "Voter Voted", "v@m.com", "h")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (31, "Candidate B")')
        conn.commit()
        conn.close()

        voters = database.get_all_voters(TEST_DB)
        emails = [v[2] for v in voters]
        self.assertNotIn('admin@admin.com', emails)
        self.assertIn('u@m.com', emails)
        self.assertIn('v@m.com', emails)

        voter_dict = {v[2]: v[3] for v in voters}
        self.assertEqual(voter_dict['u@m.com'], 0)  # unvoted
        self.assertEqual(voter_dict['v@m.com'], 1)  # voted

    def test_35_admin_dashboard_displays_voters_and_delete_actions(self):
        """Admin dashboard displays Voter Accounts table with Delete button for unvoted and Cannot Delete for voted."""
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO users (id, name, email, password) VALUES (40, "Alice Unvoted", "alice@m.com", "h")')
        c.execute('INSERT INTO users (id, name, email, password) VALUES (41, "Bob Voted", "bob@m.com", "h")')
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (41, "Candidate C")')
        conn.commit()
        conn.close()

        self.login_as_admin()
        resp = self.client.get('/admin_dashboard')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Voter Accounts", resp.data)
        self.assertIn(b"Alice Unvoted", resp.data)
        self.assertIn(b"Bob Voted", resp.data)
        self.assertIn(b"Cannot Delete", resp.data)
        self.assertIn(b"/admin/users/delete/40", resp.data)
        self.assertNotIn(b"/admin/users/delete/41", resp.data)

    # -------------------------------------------------------------
    # 9. Database Reset Utility Tests
    # -------------------------------------------------------------
    def test_36_database_reset_utility(self):
        """reset.reset_database cleans all tables, restores default candidates, resets votes, and seeds admin."""
        import reset
        # 1. Add noisy data to TEST_DB
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        c.execute('INSERT INTO candidates (name, description, is_active) VALUES ("Custom Cand", "Desc", 1)')
        c.execute('INSERT INTO users (name, email, password) VALUES ("Temp Voter", "temp@m.com", "h")')
        uid = c.lastrowid
        c.execute('INSERT INTO votes (user_id, candidate) VALUES (?, "Custom Cand")', (uid,))
        c.execute('UPDATE server_status SET is_open = 1 WHERE id = 1')
        conn.commit()
        conn.close()

        # 2. Run reset
        reset.reset_database(TEST_DB)

        # 3. Verify clean state
        conn = database.get_db_connection(TEST_DB)
        c = conn.cursor()
        users = c.execute('SELECT id, name, email FROM users').fetchall()
        candidates = c.execute('SELECT id, name, is_active FROM candidates ORDER BY id').fetchall()
        votes_count = c.execute('SELECT COUNT(*) FROM votes').fetchone()[0]
        status = c.execute('SELECT is_open FROM server_status WHERE id = 1').fetchone()[0]
        conn.close()

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0][2], 'admin@admin.com')
        self.assertEqual(len(candidates), 3)
        self.assertEqual([c[1] for c in candidates], ['Candidate A', 'Candidate B', 'Candidate C'])
        self.assertEqual(votes_count, 0)
        self.assertEqual(status, 0)

if __name__ == '__main__':
    unittest.main()
