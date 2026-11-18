"""Maintenance commands, run on the PC that hosts the app.

    python manage.py users                    list accounts
    python manage.py reset-password USERNAME  choose a new password (use this if the only Super User forgot theirs)
"""
import getpass
import sys

from ujl import auth, create_app
from ujl import db as D


def reset_password(app, username, pw):
    """Returns None when done, or a plain-words problem."""
    with app.app_context():
        with D.tx() as con:
            u = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if u is None:
                return 'There is no user with that username.'
            err = auth.pw_problem(u['username'], pw)
            if err:
                return err
            con.execute('UPDATE users SET pw_hash=?, must_change=0, updated_at=? WHERE id=?', (auth.hash_pw(pw), D.utcnow(), u['id']))
            auth.clear_failures(con, f"u:{u['username'].lower()}")
            D.audit(con, 'user.reset_password', u['username'], 'Reset from the console (manage.py)', None, username='console')
    return None


def main(argv):
    app = create_app()
    if argv[:1] == ['users']:
        with app.app_context():
            for u in D.get_db().execute('SELECT username, name, role, status FROM users ORDER BY id'):
                print(f"{u['username']:20} {u['name']:28} {u['role'] or '-':16} {u['status']}")
        return 0
    if len(argv) == 2 and argv[0] == 'reset-password':
        pw = getpass.getpass('New password (8 characters or more): ')
        if pw != getpass.getpass('Repeat it: '):
            print('The two passwords do not match.')
            return 1
        err = reset_password(app, argv[1], pw)
        print(err or 'Done. They can sign in with the new password now.')
        return 1 if err else 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
